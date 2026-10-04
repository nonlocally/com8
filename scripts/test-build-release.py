#!/usr/bin/env python3
"""Release isolation tests; --real-builds also performs two locked npm builds."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("com8_release_builder", ROOT / "scripts/build-release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
TRAILERS = ("Co-Authored-By: Claude <noreply@anthropic.com>\n"
            "Co-authored-by: Codex <codex@openai.com>\n"
            "Co-authored-by: Com8 <322615700+Com8@users.noreply.github.com>")


class SourceIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-build-test-")
        self.root = Path(self.temp.name).resolve() / "repo"
        self.root.mkdir()
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
        self.git("init", "--quiet", "--template=")
        (self.root / "tracked").write_text("committed\n")
        (self.root / "removed").write_text("remove me\n")
        (self.root / ".gitignore").write_text("node_modules/\nvendor/\n")
        self.git("add", ".")
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "--quiet",
                 "-m", "Disposable release fixture", "-m", TRAILERS)
        self.revision = self.git("rev-parse", "HEAD").strip()
        self.worktrees = self.git("worktree", "list", "--porcelain")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, env=self.env, text=True)

    def test_committed_snapshot_ignores_later_edits_and_disables_checkout_hooks(self):
        hookdir = Path(self.temp.name) / "hooks"; hookdir.mkdir()
        hook = hookdir / "post-checkout"
        sentinel = Path(self.temp.name) / "hook-ran"
        hook.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\n'); hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hookdir))
        with builder.isolated_source(self.revision, root=self.root) as source:
            (self.root / "tracked").write_text("concurrent development\n")
            self.assertEqual((source / "tracked").read_text(), "committed\n")
            self.assertEqual(builder.git("rev-parse", "HEAD", cwd=source).strip(), self.revision)
            self.assertFalse(sentinel.exists(), "packaging executed a checkout hook")
            (source / "vendor").mkdir()
            (source / "vendor/generated").write_text("private build output")
        self.assertFalse(source.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.worktrees)
        self.assertEqual((self.root / "tracked").read_text(), "concurrent development\n")
        self.assertFalse((self.root / "vendor").exists())

    def test_failed_build_cleans_only_owned_worktree(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with builder.isolated_source(self.revision, root=self.root) as source:
                raise RuntimeError("fixture failure")
        self.assertFalse(source.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.worktrees)
        self.assertTrue((self.root / "tracked").is_file())

    def test_dirty_snapshot_freezes_modified_deleted_untracked_and_symlink_files(self):
        (self.root / "tracked").write_text("captured development\n")
        self.git("rm", "--quiet", "removed")
        (self.root / "new").write_text("untracked development\n")
        (self.root / "link").symlink_to("tracked")
        for name in ["vendor", "node_modules"]:
            (self.root / name).mkdir()
            (self.root / name / "sentinel").write_text("never copy me")
        snapshot, digest = builder.worktree_snapshot(self.revision, self.root)
        self.assertRegex(digest, r"^[a-f0-9]{64}$")
        self.assertNotIn("node_modules/sentinel", snapshot)
        self.assertNotIn("vendor/sentinel", snapshot)
        (self.root / "tracked").write_text("later uncaptured edit\n")
        with builder.isolated_source(self.revision, snapshot, self.root) as source:
            self.assertEqual((source / "tracked").read_text(), "captured development\n")
            self.assertEqual((source / "new").read_text(), "untracked development\n")
            self.assertFalse((source / "removed").exists())
            self.assertEqual(os.readlink(source / "link"), "tracked")
            self.assertFalse((source / "node_modules").exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.worktrees)

    def test_dirty_capture_rejects_concurrent_edits(self):
        original = Path.read_bytes
        changed = False

        def read(path):
            nonlocal changed
            contents = original(path)
            if path == self.root / "tracked" and not changed:
                changed = True
                path.write_text("changed during capture\n")
            return contents

        with mock.patch.object(Path, "read_bytes", read):
            with self.assertRaisesRegex(RuntimeError, "changed while capturing"):
                builder.worktree_snapshot(self.revision, self.root)

    def test_untracked_only_development_archive_is_marked_dirty_inside_and_out(self):
        package = self.root / "packages/communicate"; package.mkdir(parents=True)
        (package / "src").mkdir(); (package / "src/cli.mjs").write_text("// fixture\n")
        for file, content in [("package.json", '{"version":"0.0.0-fixture"}'),
                              ("package-lock.json", '{"packages":{}}'), ("README.md", "fixture")]:
            (package / file).write_text(content)
        (self.root / "VERSION").write_text("0.0.0-fixture\n")
        (self.root / "LICENSE").write_text("fixture license\n")

        def fake_run(*args, cwd):
            if args == ("npm", "run", "vendor"):
                vendor = package / "vendor"; vendor.mkdir()
                (vendor / "release.json").write_text(json.dumps({"source": {"commit": self.revision, "dirty": False}}))
                (vendor / "VERSION").write_text("fixture-clean\n")
            else:
                self.assertEqual(args, ("npm", "ci", "--omit=dev", "--ignore-scripts"))

        with mock.patch.object(builder, "run", fake_run):
            receipt = builder.build(self.root, Path(self.temp.name) / "out", self.revision, 1, True, "a" * 64)
        with tarfile.open(receipt["archive"]) as archive:
            outer = json.load(archive.extractfile("com8-0.0.0-fixture/release.json"))
            inner = json.load(archive.extractfile("com8-0.0.0-fixture/vendor/release.json"))
            self.assertTrue(outer["dirty"])
            self.assertTrue(inner["source"]["dirty"])
            self.assertEqual(outer["sourceSnapshot"], inner["source"]["snapshot"])
            self.assertTrue(archive.extractfile("com8-0.0.0-fixture/vendor/VERSION").read().endswith(b".dirty\n"))


def fingerprint(root):
    digest = hashlib.sha256()
    if not root.exists():
        return "absent"
    for item in [root, *sorted(root.rglob("*"))]:
        info = item.lstat()
        digest.update(str(item.relative_to(root)).encode() + b"\0")
        digest.update(f"{info.st_mode}:{info.st_mtime_ns}:{info.st_ino}:{info.st_size}".encode())
        if item.is_symlink():
            digest.update(os.readlink(item).encode())
        elif item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def real_builds():
    assert not builder.git("status", "--porcelain").strip(), "commit the test/builder before qualifying clean builds"
    revision = builder.git("rev-parse", "HEAD").strip()
    inputs = [ROOT / "packages/communicate" / name for name in ["vendor", "node_modules"]]
    before = {str(item): fingerprint(item) for item in inputs}
    # Other agents may commit in their own worktrees while this runs. Compare
    # registrations, not their unrelated moving HEAD revisions.
    registrations = lambda: {line for line in builder.git("worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")}
    worktrees = registrations()
    with tempfile.TemporaryDirectory(prefix="com8-reproducibility-") as temp:
        temp = Path(temp)
        build_tmp = temp / "owned-temp"; build_tmp.mkdir()
        receipts = []
        for name in ["one", "two"]:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/build-release.py"), "--output", str(temp / name)],
                                    cwd=ROOT, env={**os.environ, "TMPDIR": str(build_tmp)}, capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            receipt = json.loads(result.stdout.splitlines()[-1]); receipts.append(receipt)
            assert receipt["source"] == revision and receipt["dirty"] is False
            with tarfile.open(receipt["archive"]) as archive:
                version = Path(receipt["archive"]).name.removesuffix(".tar.gz")
                outer = json.load(archive.extractfile(version + "/release.json"))
                inner = json.load(archive.extractfile(version + "/vendor/release.json"))
                assert outer["source"] == inner["source"]["commit"] == revision
                assert not outer["dirty"] and not inner["source"]["dirty"]
            assert list(build_tmp.iterdir()) == [], "owned build temporary directories remain"
        assert receipts[0]["sha256"] == receipts[1]["sha256"], "two clean builds differ"
        after = {str(item): fingerprint(item) for item in inputs}
        assert before == after, "source checkout vendor/dependencies changed"
        assert registrations() == worktrees, "worktree registry changed"
        return {"ok": True, "source": revision, "sha256": receipts[0]["sha256"], "clean_builds": 2,
                "input_fingerprints": before, "inputs_unchanged": True,
                "temporary_directories_removed": True, "worktree_registry_unchanged": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-builds", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SourceIsolationTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    if args.real_builds:
        report = real_builds()
        if args.evidence:
            args.evidence.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
