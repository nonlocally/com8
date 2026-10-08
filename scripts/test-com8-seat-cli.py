#!/usr/bin/env python3
"""CLI options must never become a spawned program's flags; seats identify servers."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import com8
import com8_seat


class ParserTests(unittest.TestCase):
    def call(self, args, reply=None):
        out, err = io.StringIO(), io.StringIO()
        reply = reply or {"ok": True, "seat": "%2", "tmux_server": {
            "socket_path": "/tmp/tmux-test/owned", "pid": 321}}
        with patch.object(com8, "_call", return_value=reply) as send:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = com8.cli_call(["seat", *args])
        return status, out.getvalue(), err.getvalue(), send

    def test_json_option_before_or_after_command_never_reaches_shell(self):
        for args in (["--json", "bash --norc --noprofile"],
                     ["bash --norc --noprofile", "--json"]):
            with self.subTest(args=args):
                status, out, _, send = self.call(["spawn", *args])
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(out)["seat"], "%2")
                self.assertEqual(send.call_args.args[0]["cmd"], "bash --norc --noprofile")

    def test_separator_preserves_program_flags(self):
        status, out, _, send = self.call(["spawn", "--json", "--name", "worker",
                                         "--cwd", "/tmp/project", "--", "program", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(send.call_args.args[0]["cmd"], "program --json")
        self.assertEqual(send.call_args.args[0]["cwd"], "/tmp/project")
        self.assertEqual(json.loads(out)["tmux_server"]["pid"], 321)

    def test_quoted_command_preserves_its_own_json_flag(self):
        _, _, _, send = self.call(["spawn", "program --json", "--json"])
        self.assertEqual(send.call_args.args[0]["cmd"], "program --json")

    def test_unknown_or_missing_options_fail_before_daemon(self):
        for args in (["--jsno", "bash"], ["bash", "--norc"], ["--cwd"],
                     ["--name", "--json", "bash"], ["--json", "--"]):
            with self.subTest(args=args):
                status, _, _, send = self.call(["spawn", *args])
                self.assertEqual(status, 1)
                send.assert_not_called()

    def test_legacy_plain_output_is_preserved(self):
        status, out, _, _ = self.call(["spawn", "bash --norc --noprofile"])
        self.assertEqual((status, out), (0, "%2\n"))

    def test_list_json_preserves_server_and_failure_status(self):
        for ok in [True, False]:
            reply = {"ok": ok, "seats": [], "tmux_server": {"socket_path": None, "pid": None}}
            status, out, _, _ = self.call(["ls", "--json"], reply)
            self.assertEqual(status, 0 if ok else 1)
            self.assertEqual(json.loads(out), reply)
        _, _, _, send = self.call(["ls", "--wrong"])
        send.assert_not_called()

    def test_unreachable_server_has_no_invented_socket_or_pid(self):
        with patch.dict(os.environ, {"COM8_TMUX_SOCKET": "owned-unavailable"}):
            driver = com8_seat.SeatDriver()
        with patch.object(driver, "_tmux", side_effect=com8_seat.SeatUnavailable("timeout")):
            self.assertEqual(driver.server(), {"argv": ["tmux", "-L", "owned-unavailable"],
                                             "socket_path": None, "pid": None})


class RealServerTests(unittest.TestCase):
    """Two private server lifetimes may contain the same numeric pane ID."""
    def test_json_spawn_stays_alive_and_server_coordinates_disambiguate(self):
        self.assertIsNotNone(shutil.which("tmux"), "UNQUALIFIED: tmux required")
        with tempfile.TemporaryDirectory(prefix="com8-seat-json-") as directory:
            root = Path(directory)
            env = dict(os.environ)
            for key in ("TMUX", "TMUX_PANE", "CLAUDE_CODE_MESSAGING_SOCKET"):
                env.pop(key, None)
            servers = ["com8-json-" + uuid.uuid4().hex[:12] for _ in range(2)]
            env.update(HOME=str(root), COMM_STATE=str(root / "state"),
                       COM8_SOCK_DIR=str(root / "socks"), COM8_SESSIONS_DIR=str(root / "sessions"),
                       COM8_SELF="seat-json-fixture", COM8_TMUX_SOCKET=servers[0],
                       COM8_SEAT_SESSION="owned", SHELL=shutil.which("bash"))
            (root / "sessions").mkdir()
            cli = [sys.executable, str(ROOT / "lib/com8.py")]
            def call(*args):
                return subprocess.run([*cli, "call", *args], env=env, capture_output=True,
                                      text=True, timeout=15)
            daemon = subprocess.Popen([*cli, "daemon"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(60):
                    if call("status", "--json").returncode == 0:
                        break
                    time.sleep(.05)
                else:
                    self.fail("isolated daemon failed to start")
                normal = call("seat", "spawn", "bash --norc --noprofile")
                self.assertEqual(normal.returncode, 0, normal.stderr)
                spawned = call("seat", "spawn", "bash --norc --noprofile", "--json")
                self.assertEqual(spawned.returncode, 0, spawned.stderr)
                result = json.loads(spawned.stdout)
                # The launching CLI has exited; the daemon and both seats remain.
                time.sleep(.25)
                for seat in [normal.stdout.strip(), result["seat"]]:
                    self.assertEqual(call("seat", "state", seat).stdout.strip(), "idle")
                selected = result["tmux_server"]
                self.assertTrue(Path(selected["socket_path"]).is_absolute())
                self.assertGreater(selected["pid"], 0)
                listed = json.loads(call("seat", "ls", "--json").stdout)
                self.assertEqual(listed["tmux_server"], selected)
                self.assertIn(result["seat"], {s["seat"] for s in listed["seats"]})
                # Build a second, unrelated test server with the same pane IDs.
                with patch.dict(os.environ, {**env, "COM8_TMUX_SOCKET": servers[1]}):
                    other = com8_seat.SeatDriver()
                    other.spawn("bash --norc --noprofile")
                    collision = other.spawn("bash --norc --noprofile")
                self.assertEqual(collision["seat"], result["seat"])
                self.assertNotEqual(collision["tmux_server"]["socket_path"], selected["socket_path"])
                self.assertNotEqual(collision["tmux_server"]["pid"], selected["pid"])
                observed = subprocess.check_output(["tmux", "-S", selected["socket_path"],
                    "display-message", "-p", "-t", result["seat"], "#{pid}"], text=True)
                self.assertEqual(int(observed), selected["pid"])
            finally:
                call("stop")
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.terminate(); daemon.wait(timeout=5)
                for server in servers:
                    subprocess.run(["tmux", "-L", server, "kill-server"],
                                   env=env, capture_output=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
