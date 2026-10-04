#!/usr/bin/env python3
"""Core payload and exact-pane adoption contracts; no live sessions/services."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import com8
import com8_adopt
import com8_payload
import com8_seat


class InterfaceTests(unittest.TestCase):
    def test_shared_core_payload_imports_without_apps(self):
        mock.patch.stopall()
        self.assertEqual(com8.KERNEL_FILES, com8_adopt.KERNEL_FILES)
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory)
            for name in com8_payload.KERNEL_FILES:
                (target / name).write_bytes((ROOT / "lib" / name).read_bytes())
            result = subprocess.run([sys.executable, "-c", "import com8, com8_adopt; print('core imports')"],
                                    cwd=target, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "core imports")
            result = subprocess.run([sys.executable, "com8.py", "call", "board"],
                                    cwd=target, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("not included", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "102.json"
        self.identity = {"pid": 102, "sessionId": "target", "name": "old",
                         "messagingSocketPath": "/fixture/target.sock"}
        self.path.write_text(json.dumps(self.identity))
        # Another session already has the requested name. It must not confirm
        # adoption of our target while its own sidecar remains unchanged.
        (self.path.parent / "900.json").write_text(json.dumps({
            "pid": 900, "sessionId": "other", "name": "reviewer",
            "messagingSocketPath": "/fixture/other.sock"}))
        self.driver = mock.Mock()
        self.driver._field.return_value = "100"
        self.driver.state.return_value = "idle"
        self.driver.send.return_value = {"ok": True}
        process = subprocess.CompletedProcess([], 0, "100 1\n101 100\n102 101\n900 1\n", "")
        self.addCleanup(mock.patch.stopall)
        self.factory = mock.patch.object(com8_seat, "SeatDriver", return_value=self.driver).start()
        mock.patch.object(com8_seat.subprocess, "run", return_value=process).start()

    def adopt(self, **kwargs):
        return com8_seat.adopt_pane("reviewer", "%3", self.path.parent,
                                   socket_path="/fixture/tmux.sock", **kwargs)

    def test_adoption_uses_existing_driver_and_exact_sidecar(self):
        def renamed(*args):
            self.path.write_text(json.dumps({**self.identity, "name": "reviewer"}))
            return {"ok": True}
        self.driver.send.side_effect = renamed
        self.assertTrue(self.adopt()["ok"])
        self.factory.assert_called_once_with(socket_path="/fixture/tmux.sock", ambient=True)
        self.driver.send.assert_called_once_with("%3", "/rename reviewer")

    def test_other_same_named_session_is_not_confirmation(self):
        result = self.adopt(timeout=0.01)
        self.assertFalse(result["ok"])
        self.assertTrue(result["sent"])

    def test_shell_without_target_sidecar_receives_nothing(self):
        self.path.unlink()
        with self.assertRaisesRegex(com8_seat.SeatError, "exactly one"):
            self.adopt()
        self.driver.send.assert_not_called()

    def test_busy_agent_receives_nothing(self):
        self.driver.state.return_value = "busy"
        with self.assertRaisesRegex(com8_seat.SeatError, "busy"):
            self.adopt()
        self.driver.send.assert_not_called()

    def test_replaced_session_cannot_confirm_rename(self):
        def replaced(*args):
            self.path.write_text(json.dumps({**self.identity, "name": "reviewer", "sessionId": "replacement"}))
            return {"ok": True}
        self.driver.send.side_effect = replaced
        with self.assertRaisesRegex(com8_seat.SeatError, "identity changed"):
            self.adopt()

    def test_invalid_name_never_types_control(self):
        with self.assertRaisesRegex(com8_seat.SeatError, "invalid"):
            com8_seat.adopt_pane("reviewer\n/exit", "%3", self.path.parent)
        self.driver.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
