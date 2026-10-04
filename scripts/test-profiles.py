#!/usr/bin/env python3
"""Profile qualification uses disposable homes and a dedicated tmux socket only."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profiles", ROOT / "profiles/manage.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
BASH = next((p for p in ["/opt/homebrew/bin/bash", "/usr/local/bin/bash", shutil.which("bash")] if p and Path(p).exists()), "bash")


class Profiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-profile-")
        self.home = Path(self.temp.name) / "home with spaces"
        self.home.mkdir()
        self.profile = mod.Profile(self.home)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.home / ".local/bin") + ":" + os.environ["PATH"])
        for key in ["TMUX", "TMUX_PANE", "COM8_PROFILE_CONFIG", "COM8_PROFILE_STATE", "COM8_PROFILE_RUNTIME", "COM8_PROFILE_MODULES", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "BASH_ENV", "ZDOTDIR", "COM8_ACCOUNT_LAUNCHER", "COM8_BOX_LAUNCHER", "COM8_PANE_WATCHER", "COM8_PROFILE_WATCH"]:
            self.env.pop(key, None)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, modules=None):
        self.profile.install(modules or ["terminal", "mesh"])
        self.profile = mod.Profile(self.home)

    def run_tool(self, *args, check=True, env=None):
        result = subprocess.run(args, env=env or self.env, text=True, capture_output=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, f"{args!r}\n{result.stdout}\n{result.stderr}")
        return result

    def test_preview_and_migration_are_read_only(self):
        self.assertTrue(self.profile.plan(["terminal"]))
        self.assertTrue(self.profile.migration()["read_only"])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_install_repeat_upgrade_uninstall_preserve_original_and_user_edits(self):
        rc = self.home / ".bashrc"
        rc.write_text("# my original, no trailing newline")
        self.install()
        original_backup = self.profile.record["entries"][str(rc)]["original"]["backup"]
        rc.write_text(rc.read_text() + "# added after install\n")
        self.install()
        self.assertEqual(self.profile.record["entries"][str(rc)]["original"]["backup"], original_backup)
        self.assertEqual(rc.read_text().count(mod.BEGIN), 1)
        private = self.profile.config / "local.sh"
        private.write_text("# private overlay\n")
        data = self.home / ".local/state/com8/workstation/sessions/keep"
        data.mkdir(parents=True)
        self.assertTrue(self.profile.uninstall()["ok"])
        self.assertIn("# my original", rc.read_text())
        self.assertIn("# added after install", rc.read_text())
        self.assertNotIn(mod.BEGIN, rc.read_text())
        self.assertTrue(private.exists())
        self.assertTrue(data.exists())

    def test_uninstall_untouched_file_restores_exact_bytes(self):
        rc = self.home / ".bashrc"
        rc.write_bytes(b"# no newline")
        self.install()
        self.assertTrue(self.profile.uninstall()["ok"])
        self.assertEqual(rc.read_bytes(), b"# no newline")

    def test_additive_selection_survives_a_stale_installer_snapshot(self):
        # Two commands may read the ledger before either acquires its lock.
        # The second must retain the first command's newly installed module.
        waiting = mod.Profile(self.home)
        self.install(["terminal"])
        waiting.install(["mesh"])
        self.profile = mod.Profile(self.home)
        self.assertEqual(self.profile.record["modules"], ["mesh", "terminal"])
        result = self.run_tool(BASH, "-c", '. "$HOME/.config/com8/profiles/active.sh"; declare -F t; declare -F mesh')
        self.assertEqual(result.stdout.splitlines(), ["t", "mesh"])
        self.assertTrue(self.profile.uninstall()["ok"])

    def test_owned_config_and_backup_directories_are_private(self):
        self.install()
        self.assertEqual(self.profile.config.stat().st_mode & 0o077, 0)
        self.assertEqual(self.profile.state.stat().st_mode & 0o077, 0)
        self.profile.config.chmod(0o755)
        self.profile.state.chmod(0o755)
        self.install()
        self.assertEqual(self.profile.config.stat().st_mode & 0o077, 0)
        self.assertEqual(self.profile.state.stat().st_mode & 0o077, 0)

    def test_legacy_symlink_is_never_followed(self):
        original = Path(self.temp.name) / "legacy-config"
        original.write_text("keep this exactly\n")
        target = self.home / ".bashrc"
        target.symlink_to(original)
        plan = self.profile.plan(["terminal"])
        self.assertTrue(any(p["action"] == "conflict" for p in plan))
        with self.assertRaises(mod.Conflict):
            self.profile.install(["terminal"])
        self.assertTrue(target.is_symlink())
        self.assertEqual(original.read_text(), "keep this exactly\n")

    def test_uninstall_refuses_replacement_and_keeps_entire_profile(self):
        self.install()
        wrapper = self.home / ".local/bin/com8-agent"
        wrapper.write_text("# replacement owned by another installer\n")
        result = self.profile.uninstall()
        self.assertFalse(result["ok"])
        self.assertEqual(result["changed"], 0)
        self.assertTrue((self.home / ".local/bin/com8-workstation").exists())
        self.assertEqual(wrapper.read_text(), "# replacement owned by another installer\n")

    def test_modified_payload_and_modified_block_are_not_overwritten(self):
        self.install()
        payload = Path(self.profile.record["payload"]) / "runtime/init.sh"
        payload.write_text("# changed payload\n")
        with self.assertRaises(mod.Conflict):
            self.install()
        self.assertEqual(payload.read_text(), "# changed payload\n")
        rc = self.home / ".bashrc"
        rc.write_text(rc.read_text().replace("[[ $- != *i* ]]", "# edited\n[[ $- != *i* ]]"))
        self.assertFalse(self.profile.uninstall()["ok"])
        self.assertTrue((self.home / ".local/bin/com8-workstation").exists())

    def test_failed_install_restores_earlier_targets(self):
        from unittest.mock import patch
        rc = self.home / ".bashrc"
        rc.write_text("original\n")
        actual = mod.atomic
        def fail_one(path, data, mode=0o600):
            if str(path).endswith("/.bash_profile"):
                raise OSError("simulated write failure")
            return actual(path, data, mode)
        with patch.object(mod, "atomic", fail_one):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(rc.read_text(), "original\n")
        self.assertFalse((self.home / ".local/bin/com8-workstation").exists())
        self.assertFalse(self.profile.ledger.exists())

    def test_installed_helpers_work_without_source_checkout(self):
        self.install()
        result = self.run_tool(str(self.home / ".local/bin/com8-mesh"), "help")
        self.assertIn("COM8 mesh", result.stdout)
        active = self.profile.config / "active.sh"
        expected = ["t", "_t_switch", "al", "alw", "mesh", "cx", "cxx", "cxc", "cdx", "cdxx", "cdxxs"]
        result = self.run_tool(BASH, "--noprofile", "--norc", "-c",
                               '. "$1"; declare -F ' + " ".join(expected)
                               + '; declare -F tss tsr tsl tslm tml taa tra tap tscale anu_landing chat browser || true',
                               "test", str(active))
        self.assertEqual(result.stdout.splitlines(), expected)
        absent = self.run_tool(str(self.home / ".local/bin/com8-workstation"), "shell", "tss", "proof", check=False)
        self.assertNotEqual(absent.returncode, 0)
        self.assertIn("not enabled", absent.stderr)
        self.assertNotIn(".local/share/anu", active.read_text())

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_fresh_zsh_executes_shortcuts_without_changing_shell(self):
        zsh = self.home / ".zshrc"
        zsh.write_text("# existing zsh settings\n")
        self.install(["terminal"])
        commands = self.home / ".local/bin"
        for name in ["claude", "codex"]:
            stub = commands / name
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            stub.chmod(0o755)
        tmux = commands / "tmux"
        tmux.write_text('#!/bin/sh\n[ "$1" = list-sessions ] || exit 2\nprintf "fixture: 1 windows\\n"\n')
        tmux.chmod(0o755)
        # Fail before agent.sh if an implementation regression shadows a stub;
        # this fixture must never execute a real installed provider.
        (self.profile.config / "local.sh").write_text(
            '[[ $(command -v claude) == ' + mod.quote(commands / "claude")
            + ' && $(command -v codex) == ' + mod.quote(commands / "codex")
            + ' ]] || exit 73\n')
        # A fresh GUI shell can omit Homebrew entirely. Only the managed zsh
        # PATH block loads here; each executable uses Bash internally.
        shell = shutil.which("zsh")
        env = dict(self.env, SHELL=shell, ZDOTDIR=str(self.home), PATH="/usr/bin:/bin")
        argument = "literal 'quotes' $value\nsecond line"
        result = self.run_tool(shell, "-i", "-c",
                               '[[ -n $ZSH_VERSION ]] || exit 1; '
                               'command -v cx cxx cdx cdxx al alw t; '
                               'cxx "$1"; cdxx "$1"; tl; printf "shell=%s\\n" "$SHELL"',
                               "fixture", argument, env=env)
        expected = "".join(str(commands / name) + "\n" for name in ["cx", "cxx", "cdx", "cdxx", "al", "alw", "t"])
        self.assertEqual(result.stdout, expected + "--dangerously-skip-permissions\n" + argument
                         + "\n--yolo\n" + argument + "\nfixture: 1 windows\nshell=" + shell + "\n")
        self.assertNotIn("COM8_PROFILE_RUNTIME", zsh.read_text())
        self.assertIn("# existing zsh settings", zsh.read_text())
        ghostty = (self.profile.config / "ghostty.conf").read_text()
        self.assertNotIn("command =", ghostty)
        tmux_config = (self.profile.config / "tmux.conf").read_text()
        self.assertNotIn("default-shell", tmux_config)
        self.assertFalse((commands / "com8-shell").exists())
        self.assertTrue(self.profile.uninstall()["ok"])
        self.assertFalse((commands / "cxx").exists())
        self.assertEqual(zsh.read_text(), "# existing zsh settings\n")
        self.assertTrue((commands / "claude").exists())

    def test_shortcut_collision_refuses_install_without_overwrite(self):
        shortcut = self.home / ".local/bin/cxx"
        shortcut.parent.mkdir(parents=True)
        shortcut.write_text("# my existing shortcut\n")
        plan = self.profile.plan(["terminal"])
        self.assertTrue(any(row["path"] == str(shortcut) and row["action"] == "conflict" for row in plan))
        with self.assertRaises(mod.Conflict):
            self.install(["terminal"])
        self.assertEqual(shortcut.read_text(), "# my existing shortcut\n")
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertFalse((self.home / ".local/bin/cdxx").exists())

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_fresh_zsh_mesh_help_is_owned_and_offline(self):
        self.install(["terminal"])
        commands = self.home / ".local/bin"
        mesh = commands / "mesh"
        self.assertFalse(mesh.exists())
        mesh.write_text("# existing unowned mesh\n")
        with self.assertRaises(mod.Conflict):
            self.install(["mesh"])
        self.assertEqual(mesh.read_text(), "# existing unowned mesh\n")
        mesh.unlink()  # Only the fixture just created above.
        self.install(["mesh"])
        network = self.home / "unexpected-network-call"
        for name in ["ssh", "tailscale", "curl"]:
            stub = commands / name
            stub.write_text("#!/bin/sh\nprintf called > " + mod.quote(network) + "\nexit 99\n")
            stub.chmod(0o755)
        shell = shutil.which("zsh")
        env = dict(self.env, SHELL=shell, ZDOTDIR=str(self.home), PATH="/usr/bin:/bin")
        result = self.run_tool(shell, "-i", "-c",
                               '[[ -n $ZSH_VERSION && ${+functions[mesh]} = 0 ]] || exit 1; '
                               'command -v mesh; mesh help', env=env)
        self.assertEqual(result.stdout.splitlines()[0], str(mesh))
        self.assertIn("COM8 mesh", result.stdout)
        self.assertFalse(network.exists())
        self.assertTrue(self.profile.uninstall()["ok"])
        self.assertFalse(mesh.exists())

    def test_failed_uninstall_restores_earlier_targets_and_ownership(self):
        from unittest.mock import patch
        self.install()
        before = {name: Path(name).read_bytes() for name in self.profile.record["entries"]}
        ledger = self.profile.ledger.read_bytes()
        actual = mod.atomic
        failed = False
        def fail_once(path, data, mode=0o600):
            nonlocal failed
            if path == self.profile.ledger and not failed:
                failed = True
                raise OSError("simulated ownership write failure")
            return actual(path, data, mode)
        with patch.object(mod, "atomic", fail_once):
            with self.assertRaises(OSError):
                self.profile.uninstall()
        self.assertEqual(self.profile.ledger.read_bytes(), ledger)
        self.assertEqual({name: Path(name).read_bytes() for name in before}, before)

    def test_native_launch_and_optional_account_adapter_preserve_argv(self):
        self.install()
        stub = self.home / ".local/bin/claude"
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        result = self.run_tool(str(self.home / ".local/bin/com8-agent"), "cxx", "literal $value\nnext")
        self.assertEqual(result.stdout, "--dangerously-skip-permissions\nliteral $value\nnext\n")
        result = self.run_tool(str(self.home / ".local/bin/com8-agent"), "cxc", check=False)
        self.assertNotEqual(result.returncode, 0)
        adapter = self.home / "account adapter"
        adapter.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        adapter.chmod(0o755)
        (self.profile.config / "local.sh").write_text("export COM8_ACCOUNT_LAUNCHER=" + mod.quote(adapter) + "\n")
        result = self.run_tool(str(self.home / ".local/bin/com8-agent"), "cdxx", "message")
        self.assertEqual(result.stdout, "launch\n--provider\ncodex\n--\n--yolo\nmessage\n")

    def test_snapshot_selection_installs_shared_wrapper_but_no_job(self):
        self.install(["snapshots"])
        runtime = Path(self.profile.record["payload"]) / "runtime"
        wrapper = self.home / ".local/bin/com8-snapshot"
        self.assertEqual(wrapper.read_text(), self.profile.snapshot_schedule(runtime).wrapper_text())
        self.assertIn("com8-snapshot", self.run_tool(str(wrapper), "help").stdout)
        functions = self.run_tool(BASH, "--noprofile", "--norc", "-c",
                                 '. "$1"; declare -F tss tsr; declare -F t tsl tslm tscale || true',
                                 "test", str(self.profile.config / "active.sh"))
        self.assertEqual(functions.stdout.splitlines(), ["tss", "tsr"])
        self.assertEqual((runtime.parent / "manage.py").read_bytes(), (ROOT / "profiles/manage.py").read_bytes())
        env = dict(self.env, COMMUNICATE_DATA=str(self.home / "no-core-install"))
        preview = self.run_tool(str(wrapper), "schedule", "preview", env=env)
        self.assertTrue(json.loads(preview.stdout)["read_only"])
        self.assertFalse((self.home / "Library/LaunchAgents").exists())
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertTrue(self.profile.uninstall()["ok"])

    def add_schedule_fixture(self):
        unit = self.home / ".config/systemd/user/fixture-snapshots.timer"
        unit.parent.mkdir(parents=True)
        unit.write_text("fixture unit, never loaded\n")
        record = json.loads(self.profile.ledger.read_text())
        record["entries"][str(unit)] = {"kind": "file", "original": {"kind": "absent"},
            "installed_hash": mod.digest(unit.read_bytes()), "owner": "snapshots-schedule"}
        self.profile.ledger.write_text(json.dumps(record))
        return unit

    def test_profile_uninstall_preserves_files_when_schedule_cannot_confirm_unload(self):
        from unittest.mock import Mock, patch
        self.install(["snapshots"])
        unit = self.add_schedule_fixture()
        before = self.profile.ledger.read_bytes()
        schedule = Mock()
        for answer in [{"ok": False, "unloaded": False}, {"ok": True, "unloaded": None}]:
            schedule.uninstall.return_value = answer
            with patch.object(self.profile, "snapshot_schedule", return_value=schedule):
                self.assertFalse(self.profile.uninstall()["ok"])
            self.assertEqual(self.profile.ledger.read_bytes(), before)
            self.assertTrue(unit.exists())
            self.assertTrue((self.home / ".local/bin/com8-snapshot").exists())
        # An edited startup block is caught before asking the manager to unload.
        rc = self.home / ".bashrc"
        rc.write_text(rc.read_text().replace("[[ $-", "# changed\n[[ $-"))
        schedule.reset_mock()
        with patch.object(self.profile, "snapshot_schedule", return_value=schedule):
            self.assertFalse(self.profile.uninstall()["ok"])
        schedule.uninstall.assert_not_called()

    def test_profile_uninstall_delegates_job_then_reloads_shared_ownership(self):
        from unittest.mock import Mock, patch
        self.install(["snapshots"])
        unit = self.add_schedule_fixture()
        def unload_owned():
            # No nested ownership lock, and wrappers are still available to the
            # service manager until its owned job has been stopped.
            self.assertFalse((self.profile.state / "install.lock").exists())
            self.assertTrue((self.home / ".local/bin/com8-snapshot").exists())
            record = json.loads(self.profile.ledger.read_text())
            unit.unlink()
            del record["entries"][str(unit)]
            self.profile.ledger.write_text(json.dumps(record))
            return {"ok": True, "unloaded": True}
        schedule = Mock()
        schedule.uninstall.side_effect = unload_owned
        with patch.object(self.profile, "snapshot_schedule", return_value=schedule):
            self.assertTrue(self.profile.uninstall()["ok"])
        schedule.uninstall.assert_called_once()
        self.assertFalse((self.home / ".local/bin/com8-snapshot").exists())
        self.assertEqual(json.loads(self.profile.ledger.read_text())["entries"], {})

    @unittest.skipUnless(shutil.which("jq"), "jq unavailable")
    def test_manual_mesh_hosts_and_ssh_export_preserve_ssh_config(self):
        self.install(["mesh"])
        mesh = str(self.home / ".local/bin/com8-mesh")
        self.run_tool(mesh, "host", "add", "lab", "scientist@lab.example", "2222")
        self.assertEqual((self.profile.config / "mesh/hosts.json").stat().st_mode & 0o077, 0)
        result = self.run_tool(BASH, "--noprofile", "--norc", "-c",
                               '. "$1"; umask 022; mesh help >/dev/null; umask',
                               "test", str(self.profile.config / "active.sh"))
        self.assertEqual(result.stdout.strip(), "0022")
        self.assertIn("lab", self.run_tool(mesh, "host", "list").stdout)
        sshdir = self.home / ".ssh"
        sshdir.mkdir()
        config = sshdir / "config"
        config.write_text("Host mine\n  HostName untouched\n")
        self.run_tool(mesh, "sshconfig")
        self.assertEqual(config.read_text(), "Host mine\n  HostName untouched\n")
        generated = self.profile.config / "mesh/ssh.conf"
        self.assertIn("lab.example", generated.read_text())
        self.assertIn("2222", generated.read_text())
        # Manual routes never need a Tailscale lookup or real SSH connection.
        stub = self.home / ".local/bin/ssh"
        stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        tail = self.home / ".local/bin/tailscale"
        tail.write_text('#!/usr/bin/env bash\necho UNEXPECTED_TAILSCALE >&2; exit 99\n')
        tail.chmod(0o755)
        result = self.run_tool(mesh, "ssh", "lab")
        self.assertIn("scientist@lab.example", result.stdout)
        self.assertNotIn("UNEXPECTED", result.stderr)
        result = self.run_tool(mesh, "run", "lab", "printf '%s' '$literal'")
        self.assertIn("printf '%s' '$literal'", result.stdout)
        self.assertNotIn("UNEXPECTED", result.stderr)
        # The terminal helper produces shell syntax only for deliberate commands;
        # execution here reaches our SSH stub and preserves the exact argument.
        result = self.run_tool(BASH, "--noprofile", "--norc", "-c",
                               '. "$1"; line=$(_mesh_terminal_command lab "$2") || exit; bash -c "$line"',
                               "test", str(self.profile.config / "active.sh"), "printf '%s' '$literal'")
        self.assertEqual(result.stdout, "-t\n-p\n2222\n--\nscientist@lab.example\nprintf '%s' '$literal'\n")
        self.assertNotIn("UNEXPECTED", result.stderr)
        result = self.run_tool(mesh, "host", "add", "bad", "u@$(touch bad)", check=False)
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
    def test_isolated_tmux_profile_navigation_and_snapshots(self):
        self.install(["terminal", "mesh", "snapshots"])
        # Never uses the user's default server. Environment and socket are private.
        socket = str(Path(self.temp.name) / "tmux.sock")
        tm = [shutil.which("tmux"), "-S", socket]
        # Reproduce older tmux's client-dependent `info` on newer hosts too;
        # all real workspace operations still reach this dedicated server.
        shim = self.home / ".local/bin/tmux"
        shim.write_text("#!/bin/sh\n[ \"$1\" != info ] || exit 1\nexec "
                        + shlex.quote(tm[0]) + ' "$@"\n')
        shim.chmod(0o755)
        cfg = str(self.profile.config / "tmux.conf")
        clients = []
        try:
            self.run_tool(*tm, "-f", cfg, "new-session", "-d", "-s", "fixture", "-x", "180", "-y", "60", BASH + " --noprofile --norc")
            self.run_tool(*tm, "source-file", cfg)
            pane = self.run_tool(*tm, "display-message", "-p", "-t", "fixture", "#{pane_id}").stdout.strip()
            env = dict(self.env, TMUX=socket + ",0,0", TMUX_PANE=pane)
            self.assertEqual(self.run_tool(*tm, "list-clients").stdout, "")
            self.assertNotEqual(self.run_tool(str(shim), "info", env=env, check=False).returncode, 0)
            wrapper = str(self.home / ".local/bin/com8-workstation")
            self.run_tool(wrapper, "tile", "new", env=env)
            self.assertEqual(len(self.run_tool(*tm, "list-panes", "-t", "fixture").stdout.splitlines()), 2)
            self.run_tool(wrapper, "shell", "tss", "proof", env=env)
            snapshot = self.home / ".local/state/com8/workstation/sessions/proof"
            self.assertTrue((snapshot / "windows.tsv").exists())
            for path in [snapshot, *snapshot.rglob("*")]:
                self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
            self.run_tool(str(self.home / ".local/bin/com8-snapshot"), "run",
                          "snap-2026-09-24-0300", env=env)
            scheduled = snapshot.parent / "snap-2026-09-24-0300"
            self.assertTrue((scheduled / "meta.tsv").is_file())
            self.assertIn("fixture\t", (scheduled / "windows.tsv").read_text())
            result = self.run_tool(BASH, "--noprofile", "--norc", "-c",
                                   '. "$1"; umask 022; tss umask-check >/dev/null; umask',
                                   "test", str(self.profile.config / "active.sh"), env=env)
            self.assertEqual(result.stdout.strip(), "0022")
            self.assertIn("fixture", self.run_tool(wrapper, "shell", "tsr", "-n", "proof", env=env).stdout)
            result = self.run_tool(wrapper, "shell", "tss", "../escape", check=False, env=env)
            self.assertNotEqual(result.returncode, 0)
            keys = self.run_tool(*tm, "list-keys").stdout
            self.assertNotIn(".local/share/anu", keys)
            self.assertNotIn("browser", keys)
            hooks = self.run_tool(*tm, "show-hooks", "-g").stdout
            self.assertEqual(hooks.count("after-new-window[0]"), 1)
            # Two control clients are enough to exercise tmux's real per-client
            # session/window pointers without opening a terminal or a live agent.
            self.run_tool(*tm, "new-window", "-d", "-t", "fixture", "-n", "second")
            for _ in range(2):
                clients.append(subprocess.Popen(tm + ["-C", "attach-session", "-t", "fixture"],
                                                env=self.env, stdin=subprocess.PIPE,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            deadline = time.monotonic() + 3
            rows = []
            while time.monotonic() < deadline:
                rows = self.run_tool(*tm, "list-clients", "-F", "#{client_name}").stdout.splitlines()
                if len(rows) == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(len(rows), 2)
            for client in rows:
                self.run_tool(BASH, "--noprofile", "--norc", "-c", '. "$1"; _t_switch fixture "$2"',
                              "test", str(self.profile.config / "active.sh"), client, env=env)
            attached = self.run_tool(*tm, "list-clients", "-F", "#{client_session}").stdout.splitlines()
            self.assertEqual(len(set(attached)), 2)
            self.assertTrue(all(s == "fixture" or s.startswith("fixture~") for s in attached))
            self.run_tool(*tm, "select-window", "-t", attached[0] + ":2")
            current = self.run_tool(*tm, "list-clients", "-F", "#{window_index}").stdout.splitlines()
            self.assertEqual(sorted(current), ["1", "2"])
        finally:
            for client in clients:
                client.terminate()
                client.wait(timeout=3)
                client.stdin.close()
            subprocess.run(tm + ["kill-server"], env=self.env, capture_output=True)


if __name__ == "__main__":
    unittest.main()
