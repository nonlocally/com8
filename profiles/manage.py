#!/usr/bin/env python3
"""Optional COM8 workstation profiles. Preview by default; install starts no services.

Only explicit install/uninstall mutate files. Managed blocks preserve adjacent
user text; exact hashes protect generated files. Original backups survive repeat
installs. Payloads are immutable snapshots, independent of source checkouts.
Uninstall delegates removal of a separately installed, owned snapshot schedule.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types

VERSION = "0.3.0"
HERE = Path(__file__).resolve().parent
BEGIN = "# >>> COM8 profile"
END = "# <<< COM8 profile"


class Conflict(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".com8-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def quote(value):
    import shlex
    return shlex.quote(str(value))


def snapshot(path):
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    if not path.is_file():
        raise Conflict(f"not a regular file: {path}")
    data = path.read_bytes()
    return {"kind": "file", "hash": digest(data), "mode": stat.S_IMODE(path.stat().st_mode)}


def managed(text, body, prior=None, remove=False):
    """Only replace our exact previously installed block, never adjacent text."""
    starts = [m.start() for m in re.finditer(re.escape(BEGIN), text)]
    ends = [m.start() for m in re.finditer(re.escape(END), text)]
    if not starts and not ends:
        if remove:
            raise Conflict("managed block is missing")
        if prior is not None:
            raise Conflict("managed block was removed by the user")
        sep = "" if not text or text.endswith("\n") else "\n"
        return text + sep + body
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise Conflict("duplicate or incomplete COM8 block")
    start, end = starts[0], ends[0] + len(END)
    if end < len(text) and text[end] == "\n":
        end += 1
    existing = text[start:end]
    if prior is None or existing != prior:
        raise Conflict("COM8 block has no matching ownership record or was edited")
    return text[:start] + ("" if remove else body) + text[end:]


class Profile:
    def __init__(self, home):
        self.home = Path(home).expanduser().absolute()
        if any(c in str(self.home) for c in "\n\r\0"):
            raise Conflict("home path contains a control character")
        self.config = self.home / ".config/com8/profiles"
        self.state = self.home / ".local/state/com8/profiles"
        self.payloads = self.home / ".local/share/com8/profiles"
        self.ledger = self.state / "ownership.json"
        self.record = json.loads(self.ledger.read_text()) if self.ledger.exists() else {"entries": {}}
        if self.record.get("schema", 1) != 1:
            raise Conflict("unsupported profile ownership schema")

    def check_parent(self, path):
        for p in (path, *path.parents):
            if p == self.home:
                break
            if p.is_symlink():
                raise Conflict(f"symlink requires separately reviewed migration: {p}")

    def payload_id(self):
        h = hashlib.sha256()
        for p in sorted([*(HERE / "runtime").rglob("*"), HERE / "manage.py", HERE / "NOTICE.md"]):
            if p.is_file():
                h.update(str(p.relative_to(HERE)).encode() + b"\0" + p.read_bytes())
        return VERSION + "-" + h.hexdigest()[:16]

    def snapshot_schedule(self, runtime=None):
        # Execute installed source directly: importing an immutable artifact
        # must not add __pycache__ files to its verified inventory.
        path = HERE / "runtime/modules/snapshots/schedule.py"
        module = types.ModuleType("com8_snapshot_schedule")
        module.__file__ = str(path)
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        manage = types.SimpleNamespace(**globals())
        return module.Schedule(manage, self.home, "darwin" if sys.platform == "darwin" else "linux", runtime)

    def targets(self, modules):
        runtime = self.payloads / self.payload_id() / "runtime"
        active = self.config / "active.sh"
        # This script can be loaded by a login shell and tools independently.
        active_text = (f"# COM8 profile {VERSION}; generated, put overrides in local.sh\n"
                       f"export COM8_PROFILE_RUNTIME={quote(runtime)}\n"
                       f"export COM8_PROFILE_CONFIG={quote(self.config)}\n"
                       f"export COM8_PROFILE_MODULES={quote(' '.join(modules))}\n"
                       '. "$COM8_PROFILE_RUNTIME/init.sh"\n')
        out = [(active, active_text, "file", 0o600)]
        # env bash works with the normal user PATH; a guarded re-exec handles macOS.
        bash_guard = ('#!/usr/bin/env bash\n'
                   'if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then\n'
                   '  for b in /opt/homebrew/bin/bash /usr/local/bin/bash; do\n'
                   '    [ ! -x "$b" ] || exec "$b" "$0" "$@"\n'
                   '  done\n'
                   '  echo "COM8 workstation needs Bash 4+" >&2; exit 1\n'
                   'fi\n'
                   'case ":$PATH:" in *":${BASH%/*}:"*) ;; *) export PATH="$PATH:${BASH%/*}" ;; esac\n')
        wrapper = bash_guard + f'. {quote(active)}\n'
        for name, command in [("com8-workstation", '"$COM8_PROFILE_RUNTIME/command.sh" "$@"'),
                              ("com8-agent", '"$COM8_PROFILE_RUNTIME/agent.sh" "$@"'),
                              ("com8-mesh", '"$COM8_PROFILE_RUNTIME/command.sh" shell mesh "$@"'),
                              ("mesh", '"$COM8_PROFILE_RUNTIME/command.sh" shell mesh "$@"'),
                              ("com8-account", '"$COM8_PROFILE_RUNTIME/accounts/account" "$@"'),
                              ("com8-account-pane", '"$COM8_PROFILE_RUNTIME/accounts/pane" "$@"'),
                              ("com8-account-secrets", '"$COM8_PROFILE_RUNTIME/accounts/secrets-client" "$@"'),
                              ("com8-box", 'python3 "$COM8_PROFILE_RUNTIME/box/box.py" "$@"')]:
            if name == "com8-agent" and "terminal" not in modules:
                continue
            if name in ["com8-mesh", "mesh"] and "mesh" not in modules:
                continue
            if name == "com8-box" and "box" not in modules:
                continue
            if name.startswith("com8-account") and "accounts" not in modules:
                continue
            out.append((self.home / ".local/bin" / name, wrapper + ('exec ' if name == 'com8-box' else '. ') + command + '\n', "file", 0o755))
        if "terminal" in modules:
            for alias in ["cx", "cxx", "cxc", "cdx", "cdxx", "cdxxs"]:
                out.append((self.home / ".local/bin" / alias,
                            wrapper + '. "$COM8_PROFILE_RUNTIME/agent.sh" ' + alias + ' "$@"\n', "file", 0o755))
            for helper in ["t", "tn", "tk", "tl", "tp", "tj", "tw", "twp", "to", "tws", "twg", "al", "alw"]:
                out.append((self.home / ".local/bin" / helper,
                            wrapper + '. "$COM8_PROFILE_RUNTIME/command.sh" shell ' + helper + ' "$@"\n', "file", 0o755))
        if "snapshots" in modules:
            out.append((self.home / ".local/bin/com8-snapshot",
                        self.snapshot_schedule(runtime).wrapper_text(), "file", 0o755))
        shell_block = f'{BEGIN}\n[[ $- != *i* ]] || . {quote(active)}\n{END}\n'
        out.extend((self.home / p, shell_block, "block", 0o600) for p in [".bashrc", ".bash_profile"])
        zsh_block = (f'{BEGIN}\n'
                     'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n'
                     f'{END}\n')
        out.append((self.home / ".zshrc", zsh_block, "block", 0o600))
        if "terminal" in modules:
            # The option expands at command execution, after tmux parses its
            # configuration. Shell quoting therefore survives spaces in HOME.
            rendered = (HERE / "runtime/tmux/tmux.conf").read_text().replace(
                "@COM8_COMMAND@", "#{@com8_profile_command}").replace(
                "@COM8_TMUX_CONFIG@", json.dumps(str(self.config / "tmux.conf")))
            rendered = 'set -g @com8_profile_command ' + json.dumps(quote(self.home / '.local/bin/com8-workstation')) + '\n' + rendered
            if "mesh" in modules:
                rendered += '\nbind m display-popup -E -w 80% -h 60% "#{@com8_profile_command} shell mesh"\n'
            rendered += '\nsource-file -q ' + json.dumps(str(self.config / 'local.tmux.conf')) + '\n'
            out.append((self.config / "tmux.conf", rendered, "file", 0o600))
            tmux_target = self.home / ".tmux.conf"
            if not tmux_target.exists() and not tmux_target.is_symlink():
                tmux_target = self.home / ".config/tmux/tmux.conf"
            out.append((tmux_target, f'{BEGIN}\nsource-file {json.dumps(str(self.config / "tmux.conf"))}\n{END}\n', "block", 0o600))
            ghostty = (HERE / "runtime/ghostty/config").read_text()
            if sys.platform == "darwin":
                ghostty += (HERE / "runtime/ghostty/macos.conf").read_text()
            ghostty += 'config-file = ?' + str(self.config / "local.ghostty.conf") + '\n'
            out.append((self.config / "ghostty.conf", ghostty, "file", 0o600))
            out.append((self.home / ".config/ghostty/config", f'{BEGIN}\nconfig-file = {self.config / "ghostty.conf"}\n{END}\n', "block", 0o600))
        return out

    def plan(self, modules):
        old = self.record.get("entries", {})
        result = []
        for path, content, kind, mode in self.targets(modules):
            item = {"path": str(path), "kind": kind, "mode": mode, "content": content}
            try:
                self.check_parent(path)
                before = snapshot(path)
                prev = old.get(str(path))
                if kind == "block":
                    text = path.read_text() if path.exists() else ""
                    item["next"] = managed(text, content, prev.get("block") if prev else None).encode()
                else:
                    if before["kind"] != "absent" and (not prev or before.get("hash") != prev.get("installed_hash")):
                        raise Conflict("existing generated target is unowned or edited")
                    item["next"] = content.encode()
                item["before"] = before
                item["action"] = "unchanged" if before.get("hash") == digest(item["next"]) else "update" if prev else "install"
            except (Conflict, UnicodeError) as e:
                item["action"], item["reason"] = "conflict", str(e)
            result.append(item)
        return result

    @contextlib.contextmanager
    def lock(self):
        self.check_parent(self.state / "install.lock")
        self.check_parent(self.payloads)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        lock = self.state / "install.lock"
        try:
            lock.mkdir()
        except FileExistsError:
            raise Conflict(f"another profile operation owns {lock}; inspect before recovering a stale lock")
        try:
            yield
        finally:
            lock.rmdir()

    def install(self, modules):
        with self.lock():
            # Reload inside lock: concurrent invocations cannot overwrite ownership.
            self.record = json.loads(self.ledger.read_text()) if self.ledger.exists() else {"entries": {}}
            # Selection is additive. Another command may have added a module
            # since this command read its initial ledger before taking the lock.
            modules = sorted(set(self.record.get("modules", [])) | set(modules))
            plan = self.plan(modules)
            conflicts = [p for p in plan if p["action"] == "conflict"]
            if conflicts:
                raise Conflict("; ".join(p["path"] + ": " + p["reason"] for p in conflicts))
            self.config.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.config.chmod(0o700)
            dest = self.payloads / self.payload_id()
            self.check_parent(dest)
            if not dest.exists():
                self.payloads.mkdir(parents=True, exist_ok=True)
                tmp = Path(tempfile.mkdtemp(prefix=".payload-", dir=self.payloads))
                try:
                    shutil.copytree(HERE / "runtime", tmp / "runtime")
                    shutil.copy2(HERE / "NOTICE.md", tmp / "NOTICE.md")
                    # The optional scheduler shares our ownership machinery.
                    # Keep it self-contained after the source/release moves.
                    shutil.copy2(HERE / "manage.py", tmp / "manage.py")
                    os.replace(tmp, dest)
                finally:
                    if tmp.exists():
                        shutil.rmtree(tmp)
            else:
                for source in [*(HERE / "runtime").rglob("*"), HERE / "manage.py", HERE / "NOTICE.md"]:
                    if source.is_file():
                        installed = dest / source.relative_to(HERE)
                        self.check_parent(installed)
                        if not installed.is_file() or installed.read_bytes() != source.read_bytes():
                            raise Conflict(f"immutable profile payload was modified: {installed}")
            record = dict(self.record)
            record["entries"] = dict(self.record.get("entries", {}))
            rollback = []
            try:
                for p in plan:
                    path = Path(p["path"])
                    if snapshot(path) != p["before"]:
                        raise Conflict(f"changed since preview: {path}")
                    prior = record["entries"].get(str(path))
                    data = path.read_bytes() if path.exists() else None
                    if not prior:
                        original = dict(p["before"])
                        if data is not None:
                            backup = self.state / "backups" / (digest(str(path).encode() + b'\0' + data) + ".original")
                            if backup.exists() and backup.read_bytes() != data:
                                raise Conflict(f"original backup already exists with different content: {backup}")
                            atomic(backup, data, original.get("mode", 0o600))
                            original["backup"] = str(backup)
                    else:
                        original = prior["original"]
                    rollback.append((path, data, p["before"].get("mode", p["mode"])))
                    if p["action"] != "unchanged":
                        atomic(path, p["next"], p["before"].get("mode", p["mode"]))
                    record["entries"][str(path)] = {"kind": p["kind"], "original": original,
                        "installed_hash": digest(p["next"]), "block": p["content"] if p["kind"] == "block" else None}
                record.update(schema=1, version=VERSION, modules=modules, payload=str(dest), updated_at=time.time())
                atomic(self.ledger, (json.dumps(record, indent=2) + "\n").encode())
            except Exception:
                for path, data, mode in reversed(rollback):
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, data, mode)
                raise
        return {"ok": True, "modules": modules, "payload": str(dest), "paths": len(plan),
                "activation": "Open a fresh Bash shell; load the tmux profile deliberately. No running sessions or services were changed."}

    def uninstall_plan(self, record):
        actions, conflicts = [], []
        for name, entry in record["entries"].items():
            path = Path(name)
            try:
                self.check_parent(path)
                now = snapshot(path)
                if entry["kind"] == "block":
                    data = managed(path.read_text(), "", entry["block"], remove=True).encode()
                    orig = entry["original"]
                    original_data = Path(orig["backup"]).read_bytes() if orig["kind"] == "file" else b''
                    # Repeat installs must not turn later user additions
                    # into installer-owned content and erase them here.
                    if data == original_data or (not original_data.endswith(b'\n') and data == original_data + b'\n'):
                        data = original_data if orig["kind"] == "file" else None
                else:
                    if now.get("hash") != entry["installed_hash"]:
                        raise Conflict("target was replaced or edited; left untouched")
                    orig = entry["original"]
                    data = Path(orig["backup"]).read_bytes() if orig["kind"] == "file" else None
                actions.append((path, data, now))
            except (Conflict, OSError, UnicodeError) as e:
                conflicts.append({"path": name, "reason": str(e)})
        return actions, conflicts

    def uninstall(self):
        # The schedule shares this ledger/lock. Never invoke it while holding
        # our lock; it must verify and stop its owned job before unlinking units.
        record = json.loads(self.ledger.read_text()) if self.ledger.exists() else {"entries": {}}
        if any(e.get("owner") == "snapshots-schedule" for e in record["entries"].values()):
            with self.lock():
                record = json.loads(self.ledger.read_text())
                _, conflicts = self.uninstall_plan(record)
                if conflicts:
                    return {"ok": False, "conflicts": conflicts, "changed": 0}
            try:
                result = self.snapshot_schedule().uninstall()
            except Exception as error:
                return {"ok": False, "changed": 0, "error": "snapshot schedule uninstall failed: " + str(error)}
            if not result.get("ok") or result.get("unloaded") is not True:
                return {"ok": False, "changed": 0, "error": "snapshot schedule could not confirm its job unloaded",
                        "schedule": result}
            record = json.loads(self.ledger.read_text())
            if any(e.get("owner") == "snapshots-schedule" for e in record["entries"].values()):
                return {"ok": False, "changed": 0, "error": "snapshot schedule still owns files after uninstall"}
        with self.lock():
            record = json.loads(self.ledger.read_text()) if self.ledger.exists() else {"entries": {}}
            actions, conflicts = self.uninstall_plan(record)
            if conflicts:
                return {"ok": False, "conflicts": conflicts, "changed": 0}
            if any(e.get("owner") == "snapshots-schedule" for e in record["entries"].values()):
                return {"ok": False, "changed": 0, "error": "snapshot ownership changed; retry uninstall"}
            rollback = []
            try:
                for path, data, before in actions:
                    if snapshot(path) != before:
                        raise Conflict(f"target changed during uninstall: {path}")
                    rollback.append((path, path.read_bytes(), before.get("mode", 0o600)))
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, data, before.get("mode", 0o600))
                record["entries"] = {}
                record["modules"] = []
                atomic(self.ledger, (json.dumps(record, indent=2) + "\n").encode())
            except Exception:
                for path, data, mode in reversed(rollback):
                    atomic(path, data, mode)
                raise
            return {"ok": True, "removed": len(actions), "preserved": "private configuration, runtime state, original backups, and immutable payloads"}

    def migration(self):
        paths = [".local/share/anu", ".local/bin/anu", ".local/bin/pane", ".local/bin/communicate", ".local/bin/com8",
                 ".local/share/com8/current", ".local/share/communicate/current", ".local/share/communicate/repo-path",
                 ".config/ghostty/config", ".config/tmux/tmux.conf", ".tmux.conf", ".bashrc", ".bash_profile",
                 ".config/nvim", ".claude/settings.json", ".codex/config.toml", ".codex/AGENTS.md",
                 "Library/LaunchAgents/com.anu.snapshot.plist"]
        rows = []
        for rel in paths:
            p = self.home / rel
            if not p.exists() and not p.is_symlink():
                continue
            row = {"path": str(p), "kind": "symlink" if p.is_symlink() else "directory" if p.is_dir() else "file"}
            if p.is_symlink():
                row["target"] = os.readlink(p)
            elif p.is_file():
                # Report only markers and hashes; never emit settings, credentials or shell contents.
                data = p.read_bytes()
                row["sha256"] = digest(data)
                row["legacy_markers"] = [s for s in [".local/share/anu", "communicate@communicate", "anu@anu", "repo-path", "anu account", "anu-snapshot"] if s.encode() in data]
            rows.append(row)
        for root in [self.home / ".local/share/communicate", self.home / ".config/systemd/user", self.home / ".agents/skills"]:
            if root.exists():
                rows.append({"path": str(root), "kind": "review", "reason": "inspect active pointers/registrations/services separately; not modified by profiles"})
        return {"read_only": True, "paths": rows,
                "blockers": ["Do not run legacy anu unlink after replacement: it does not verify symlink ownership.",
                             "Inventory running shells, tmux hooks/watchers and registered plugins separately; this report does not query or stop them.",
                             "Account launcher, secrets, box credentials, snapshots and provider homes need a private migration; public profiles do not copy credentials.",
                             "No legacy repo, registration, service or command is disabled by this report."],
                "preserve": ["source checkouts", "provider histories", "identity/mail state", "private configuration", "snapshots", "original backups"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["preview", "install", "status", "uninstall", "migrate-preview"], nargs="?", default="preview")
    ap.add_argument("--terminal", action="store_true")
    ap.add_argument("--mesh", action="store_true")
    ap.add_argument("--snapshots", action="store_true", help="optional snapshot commands; scheduling remains a separate explicit operation")
    ap.add_argument("--box", action="store_true", help="optional local container adapter; no runtime is started")
    ap.add_argument("--accounts", action="store_true", help="optional configured account launch and observer tools; no watcher or service is started")
    ap.add_argument("--home", default=str(Path.home()), help="installation home; use an isolated home for qualification")
    ap.add_argument("--json", action="store_true", help="output is always JSON")
    args = ap.parse_args(argv)
    try:
        profile = Profile(args.home)
        selected = [m for m in ["terminal", "mesh", "accounts", "box", "snapshots"] if getattr(args, m)]
        modules = sorted(set(profile.record.get("modules", [])) | set(selected or ["terminal"]))
        if args.command == "install":
            result = profile.install(modules)
        elif args.command == "uninstall":
            result = profile.uninstall()
        elif args.command == "migrate-preview":
            result = profile.migration()
        elif args.command == "status":
            result = {"version": VERSION, "record": profile.record,
                      "dependencies": {b: shutil.which(b) for b in ["bash", "tmux", "fzf", "jq", "ssh", "tailscale", "ghostty", "claude", "codex"]}}
        else:
            result = {"read_only": True, "modules": modules, "actions": [{k: v for k, v in p.items() if k not in ["next", "content"]} for p in profile.plan(modules)],
                      "no_automatic_actions": ["package installation", "network", "system login shell change", "service start", "tmux reload", "legacy uninstall"]}
            if "terminal" in modules:
                result["terminal_commands"] = {"directory": str(profile.home / ".local/bin"),
                    "zsh_path_include": str(profile.home / ".zshrc"),
                    "system_login_shell_changed": False, "interactive_shell_changed": False}
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok", True) else 1
    except (Conflict, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
