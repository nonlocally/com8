#!/usr/bin/env python3
"""Standalone shutdown fixtures: no Homebrew, COM8, providers or managers."""
import errno
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("homebrew_qualification", Path(__file__).with_name("qualify-homebrew.py"))
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


class ShutdownFixture:
    def __init__(self, root, *, socket_at=0.1, pidfile_at=0.2, process_at=0.35,
                 process="live", returncode=0, command_error=None):
        self.root = root
        self.socket = root / "state/com8/com8.sock"
        self.pidfile = root / "state/com8/daemon.pid"
        self.socket.parent.mkdir(parents=True)
        self.socket.touch()
        self.pidfile.write_text("71234\n")
        self.now = 0.0
        self.socket_at, self.pidfile_at, self.process_at = socket_at, pidfile_at, process_at
        self.process, self.returncode, self.command_error = process, returncode, command_error
        self.commands, self.probes = [], []
        self.daemon = qualification.OwnedDaemon(
            root, root / "data/bin/com8", {"COMM_STATE": str(root / "state")},
            command=self.command, probe=self.probe, clock=lambda: self.now,
            sleep=self.sleep, timeout=0.6)

    def command(self, argv, **kwargs):
        self.commands.append((argv, kwargs))
        if self.command_error:
            raise self.command_error
        # A duplicate request can fail after the first stop was accepted.
        return subprocess.CompletedProcess(argv, self.returncode if len(self.commands) == 1 else 2,
                                           "stopped\n", "" if len(self.commands) == 1 else "no listener\n")

    def probe(self, pid):
        self.probes.append(pid)
        return "gone" if self.process_at is not None and self.now >= self.process_at else self.process

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 9)
        if self.socket_at is not None and self.now >= self.socket_at:
            self.socket.unlink(missing_ok=True)
        if self.pidfile_at is not None and self.now >= self.pidfile_at:
            self.pidfile.unlink(missing_ok=True)

    def started(self):
        self.daemon.begin_start()
        self.daemon.capture_pid()
        return self.daemon


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="com8-stop-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "owned"

    def test_delayed_ack_requires_paths_and_remembered_process_to_disappear(self):
        fixture = ShutdownFixture(self.root)
        daemon = fixture.started()
        self.assertTrue(daemon.stop())
        self.assertGreaterEqual(fixture.now, 0.35)
        self.assertFalse(fixture.socket.exists())
        self.assertFalse(fixture.pidfile.exists())
        self.assertEqual(set(fixture.probes), {71234})
        self.assertEqual(daemon.current["stop_command"]["stdout"], "stopped\n")
        self.assertEqual(daemon.current["shutdown_checks"][-1]["final_state"]["process"], "gone")
        self.assertTrue(daemon.stop(required=False))  # finally needs no second request
        self.assertEqual(len(fixture.commands), 1)
        argv, kwargs = fixture.commands[0]
        self.assertEqual(argv, [str(self.root / "data/bin/com8"), "stop"])
        self.assertEqual(kwargs["env"]["COMM_STATE"], str(self.root / "state"))

    def test_nonzero_stop_is_accepted_only_after_independent_exit(self):
        fixture = ShutdownFixture(self.root, returncode=2)
        daemon = fixture.started()
        self.assertTrue(daemon.stop())
        self.assertEqual(daemon.current["stop_command"]["returncode"], 2)
        self.assertGreaterEqual(fixture.now, 0.35)

    def test_command_timeout_preserves_bounded_evidence_and_can_observe_exit(self):
        failure = subprocess.TimeoutExpired("owned stop", 0.6, output=b"x" * 5000, stderr=b"diagnostic")
        fixture = ShutdownFixture(self.root, command_error=failure)
        daemon = fixture.started()
        self.assertTrue(daemon.stop())
        self.assertEqual(daemon.current["stop_command"]["error"], "timeout")
        self.assertEqual(len(daemon.current["stop_command"]["stdout"]), 4096)
        self.assertEqual(daemon.current["stop_command"]["stderr"], "diagnostic")
        self.assertEqual(len(fixture.commands), 1)

    def test_live_or_unknown_process_cannot_be_replaced_by_absent_files(self):
        for process in ("live", "unknown"):
            with self.subTest(process=process):
                root = self.root / process
                fixture = ShutdownFixture(root, process_at=None, process=process)
                daemon = fixture.started()
                with self.assertRaisesRegex(RuntimeError, "shutdown unconfirmed"):
                    daemon.stop()
                self.assertFalse(daemon.stop(required=False))
                self.assertEqual(len(fixture.commands), 1)
                report = {"ok": True, "error": "earlier qualification failure"}
                qualification.finish_fixture_cleanup(report, root, ["isolated daemon shutdown unconfirmed"])
                self.assertTrue(root.exists())
                self.assertTrue(report["fixture_retained"])
                self.assertFalse(report["ok"])
                self.assertEqual(report["error"], "earlier qualification failure")

    def test_surviving_socket_or_pidfile_is_not_success_even_after_esrch(self):
        for remaining in ("socket", "pidfile"):
            with self.subTest(remaining=remaining):
                fixture = ShutdownFixture(self.root / remaining,
                                          socket_at=None if remaining == "socket" else 0.1,
                                          pidfile_at=None if remaining == "pidfile" else 0.1)
                daemon = fixture.started()
                self.assertFalse(daemon.stop(required=False))
                self.assertEqual(daemon.current["shutdown_checks"][-1]["final_state"]["process"], "gone")

    def test_changed_pid_refuses_stop_and_retains_original_identity(self):
        fixture = ShutdownFixture(self.root, socket_at=None, pidfile_at=None, process_at=None)
        daemon = fixture.started()
        fixture.pidfile.write_text("71235\n")
        self.assertFalse(daemon.stop(required=False))
        self.assertEqual(daemon.current["pid"], 71234)
        self.assertEqual(fixture.commands, [])
        fixture.socket.unlink()
        fixture.pidfile.unlink()
        fixture.process_at = 0
        self.assertFalse(daemon.stop(required=False))
        self.assertEqual(daemon.current["shutdown_checks"][-1]["final_state"]["pid_error"], "owned pidfile changed")

    def test_failed_start_can_capture_owned_pid_during_cleanup(self):
        fixture = ShutdownFixture(self.root)
        fixture.daemon.begin_start()
        self.assertTrue(fixture.daemon.stop(required=False))
        self.assertEqual(fixture.daemon.current["pid"], 71234)
        self.assertEqual(len(fixture.commands), 1)

    def test_attempted_start_without_pid_remains_unconfirmed(self):
        fixture = ShutdownFixture(self.root)
        fixture.socket.unlink()
        fixture.pidfile.unlink()
        fixture.daemon.begin_start()
        self.assertFalse(fixture.daemon.stop(required=False))
        self.assertEqual(fixture.commands, [])

    def test_unrecorded_endpoint_is_not_mistaken_for_never_started(self):
        fixture = ShutdownFixture(self.root)
        fixture.pidfile.write_text("invalid\n")
        self.assertFalse(fixture.daemon.stop(required=False))
        self.assertEqual(fixture.commands, [])

    def test_missing_unstarted_fixture_is_inert(self):
        daemon = qualification.OwnedDaemon(self.root, self.root / "missing", {},
                                          command=lambda *a, **k: self.fail("unexpected stop"),
                                          probe=lambda pid: self.fail("unexpected PID probe"))
        self.assertTrue(daemon.stop(required=False))
        self.assertFalse(self.root.exists())

    def test_each_new_lifecycle_has_its_own_recorded_pid_and_single_stop(self):
        fixture = ShutdownFixture(self.root)
        daemon = fixture.started()
        self.assertTrue(daemon.stop())
        fixture.now = 0.0
        fixture.socket.touch()
        fixture.pidfile.write_text("71236\n")
        daemon.begin_start()
        daemon.capture_pid()
        self.assertTrue(daemon.stop())
        self.assertEqual([record["pid"] for record in daemon.records], [71234, 71236])
        self.assertEqual(len(fixture.commands), 2)

    def test_success_removes_only_owned_fixture(self):
        self.root.mkdir()
        other = self.root.parent / "not-owned"
        other.write_text("preserve")
        report = {"ok": True}
        qualification.finish_fixture_cleanup(report, self.root, [])
        self.assertFalse(self.root.exists())
        self.assertEqual(other.read_text(), "preserve")
        self.assertFalse(report["fixture_retained"])

    def test_process_probe_requires_esrch_and_never_sends_a_signal(self):
        for error, expected in ((None, "live"), (ProcessLookupError(errno.ESRCH, "gone"), "gone"),
                                (PermissionError(errno.EPERM, "denied"), "unknown"),
                                (OSError(errno.EIO, "unknown"), "unknown")):
            with self.subTest(expected=expected), patch.object(qualification.os, "kill", side_effect=error) as kill:
                self.assertEqual(qualification.process_state(71234), expected)
                kill.assert_called_once_with(71234, 0)


if __name__ == "__main__":
    unittest.main()
