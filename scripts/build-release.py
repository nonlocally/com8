#!/usr/bin/env python3
"""Build an installable, deterministic COM8 runtime archive from the lockfile.

The archive includes production Node dependencies. Installing it requires Node,
Python and Bash, but never a Git checkout, npm credentials, or npm registry access.
"""
import argparse
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def git(*args, cwd=ROOT, text=True):
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, text=text)


def run(*args, cwd=ROOT):
    # Node/npm can retain a compile cache in os.tmpdir(). Keep tool-created
    # scratch data inside an owned directory too, not the caller's TMPDIR.
    with tempfile.TemporaryDirectory(prefix="com8-release-tools-", dir=Path(cwd).parent) as temp:
        env = {**os.environ, "TMPDIR": temp, "TMP": temp, "TEMP": temp,
               "NODE_COMPILE_CACHE": str(Path(temp) / "node-compile-cache")}
        subprocess.run(args, cwd=cwd, env=env, check=True)


def worktree_snapshot(revision, root=ROOT):
    """Freeze tracked and nonignored untracked files; never copy deps/vendor.

    Read twice to reject concurrent edits during capture, then build from the
    captured bytes. Deleted tracked files remain deletions in the snapshot.
    """
    committed = set(git("ls-tree", "-r", "--name-only", "-z", revision, cwd=root).split("\0")) - {""}

    def capture():
        names = set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z", cwd=root).split("\0")) - {""}
        result = {}
        for name in sorted(committed | names):
            source = root / name
            try:
                info = source.lstat()
            except FileNotFoundError:
                result[name] = None
                continue
            if stat.S_ISLNK(info.st_mode):
                result[name] = ("symlink", os.readlink(source), 0)
            elif stat.S_ISREG(info.st_mode):
                result[name] = ("file", source.read_bytes(), 0o755 if info.st_mode & 0o111 else 0o644)
            else:
                raise RuntimeError(f"unsupported snapshot source (expected file/symlink): {name}")
        return result

    captured = capture()
    if captured != capture():
        raise RuntimeError("working tree changed while capturing development source; retry when edits settle")
    digest = hashlib.sha256()
    for name, item in captured.items():
        digest.update(name.encode() + b"\0")
        if item is None:
            digest.update(b"deleted\0")
        else:
            kind, content, mode = item
            digest.update(f"{kind}\0{mode}\0".encode())
            digest.update(content.encode() if isinstance(content, str) else content)
            digest.update(b"\0")
    return captured, digest.hexdigest()


@contextlib.contextmanager
def isolated_source(revision, snapshot=None, root=ROOT):
    """Own one detached temporary worktree and remove only that registration."""
    with tempfile.TemporaryDirectory(prefix="com8-release-source-") as temp:
        source = Path(temp) / "source"
        registered = False
        try:
            # Do not run a user's post-checkout hook just to package a release.
            git("worktree", "add", "--detach", "--quiet", str(source), revision, cwd=root)
            registered = True
            if snapshot is not None:
                for name, item in snapshot.items():
                    target = source / name
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    elif target.exists():
                        raise RuntimeError(f"snapshot path conflicts with a directory: {name}")
                    if item is None:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    kind, content, mode = item
                    if kind == "symlink":
                        target.symlink_to(content)
                    else:
                        target.write_bytes(content)
                        target.chmod(mode)
            yield source
        finally:
            # Never prune other worktrees or run checkout/cleanup hooks.
            if registered or (source / ".git").is_file():
                git("worktree", "remove", "--force", str(source), cwd=root)


def launcher(entry):
    return '''#!/usr/bin/env bash
set -euo pipefail
self="${BASH_SOURCE[0]}"
while [ -L "$self" ]; do
  base="$(cd "$(dirname "$self")" && pwd)"
  self="$(readlink "$self")"
  [[ "$self" = /* ]] || self="$base/$self"
done
root="$(cd "$(dirname "$self")/.." && pwd)"
exec node "$root/src/''' + entry + '''" "$@"
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--allow-dirty", action="store_true", help="development candidates only; freeze tracked and nonignored untracked files")
    args = parser.parse_args()
    revision = git("rev-parse", "HEAD").strip()
    dirty = bool(git("status", "--porcelain").strip())
    if dirty and not args.allow_dirty:
        parser.error("release source has uncommitted changes; commit it or use --allow-dirty for a development candidate")
    snapshot, snapshot_hash = worktree_snapshot(revision) if dirty else (None, None)
    args.output = args.output.expanduser().resolve()
    epoch = int(git("show", "-s", "--format=%ct", revision))
    with isolated_source(revision, snapshot) as source:
        result = build(source, args.output, revision, epoch, dirty, snapshot_hash)
    print(json.dumps(result))


def build(source, output, revision, epoch, dirty, snapshot_hash):
    package = source / "packages/communicate"
    if not (package / "package-lock.json").is_file():
        raise RuntimeError("source snapshot requires package-lock.json")
    version = json.loads((package / "package.json").read_text())["version"]
    if (source / "VERSION").read_text().strip() != version:
        raise RuntimeError("root and package release versions differ")
    # Vendoring uses only Node/Python standard libraries. Production dependency
    # installation happens once, in the private stage below, never the checkout.
    run("npm", "run", "vendor", cwd=package)
    if dirty:
        # vendor.mjs deliberately ignores untracked files for source checkouts;
        # a development archive must still disclose an untracked-only snapshot.
        manifest_path = package / "vendor/release.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"].update(dirty=True, snapshot=snapshot_hash)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        (package / "vendor/VERSION").write_text(f"{version}+{revision[:12]}.dirty\n")
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"com8-{version}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="com8-release-") as temp:
        stage = Path(temp) / f"com8-{version}"
        stage.mkdir()
        for name in ("src", "vendor"):
            shutil.copytree(package / name, stage / name, symlinks=True)
        for name in ("package.json", "package-lock.json", "README.md"):
            shutil.copy2(package / name, stage / name)
        shutil.copy2(source / "LICENSE", stage / "LICENSE")
        run("npm", "ci", "--omit=dev", "--ignore-scripts", cwd=stage)
        (stage / "bin").mkdir()
        for name, entry in (("com8", "com8.mjs"), ("communicate", "cli.mjs")):
            path = stage / "bin" / name
            path.write_text(launcher(entry))
            path.chmod(0o755)
        dependencies = {}
        lock = json.loads((stage / "package-lock.json").read_text())
        for name, data in lock.get("packages", {}).items():
            if name:
                dependencies[name] = {k: data[k] for k in ("version", "integrity", "license") if k in data}
        manifest = {"product": "COM8", "version": version, "source": revision,
                    "dirty": dirty, "sourceDateEpoch": epoch, "dependencies": dependencies,
                    "files": {}}
        if snapshot_hash:
            manifest["sourceSnapshot"] = snapshot_hash
        for file in sorted(stage.rglob("*")):
            if file.is_file() and not file.is_symlink():
                manifest["files"][file.relative_to(stage).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
        (stage / "release.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        with target.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for file in [stage, *sorted(stage.rglob("*"))]:
                    info = archive.gettarinfo(str(file), arcname=file.relative_to(stage.parent).as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = epoch
                    info.pax_headers = {}
                    info.mode = 0o755 if file.is_dir() or file.stat().st_mode & 0o111 else 0o644
                    if info.isfile():
                        with file.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(f"{checksum}  {target.name}\n")
    return {"archive": str(target), "sha256": checksum, "source": revision, "dirty": dirty}


if __name__ == "__main__":
    main()
