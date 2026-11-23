#!/usr/bin/env python3
"""What `bus status` says about the outbound worker comes from the process, not from the worker's own file.

Every case starts a real detached worker in a temporary state directory. No hub,
no session and no network is involved: a worker with no registrations still owns
its lock and publishes its state.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkerLivenessTests(unittest.TestCase):
    def setUp(self):
        # Resolved, because the worker receives the resolved state path in its arguments.
        self.temp = Path(tempfile.mkdtemp(prefix="bus-worker-liveness-")).resolve()
        home = self.temp / "home"
        home.mkdir()
        self.env = dict(os.environ, HOME=str(home), COMM_STATE=str(self.temp / "state"),
                        CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
                        COMM_BUS_PORT="0", PYTHONDONTWRITEBYTECODE="1")
        for key in ("CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "COMM_CODEX_INDEX",
                    "XDG_STATE_HOME"):
            self.env.pop(key, None)
        self.held = []

    def tearDown(self):
        for stream in self.held:
            stream.close()
        subprocess.run([str(ROOT / "bin/communicate"), "bus", "stop"], env=self.env, capture_output=True, timeout=15)
        shutil.rmtree(self.temp)

    def worker_file(self):
        try:
            return json.loads((self.temp / "state/bus/worker.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, probe, what, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = probe()
            if value:
                return value
            time.sleep(0.05)
        self.fail("timed out waiting until " + what)

    def start_worker(self):
        code = "import sys; sys.path.insert(0, %r); import bus; bus.start_worker()" % str(ROOT / "lib")
        subprocess.run([sys.executable, "-c", code], env=self.env, check=True, capture_output=True, timeout=30)
        info = self.wait_for(lambda: self.worker_file() if self.worker_file().get("state") == "running" else None,
                             "the worker publishes running")
        return info["pid"]

    def kill_fixture_worker(self, pid):
        # The pid comes from this fixture's own state file; signal it only while
        # its command line still names this fixture's state directory.
        argv = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=5).stdout
        self.assertIn("__worker", argv)
        self.assertIn(self.env["COMM_STATE"], argv)
        os.kill(pid, signal.SIGKILL)

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        self.wait_for(gone, "the killed worker has left the process table")

    def hold_lock(self, name):
        """Hold one of the bus state locks from this test process, as a worker or a starting command would."""
        root = self.temp / "state/bus"
        root.mkdir(parents=True, exist_ok=True)
        stream = open(root / (name + ".lock"), "a+")
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held.append(stream)

    def write_worker_file_without_kernel_token(self):
        """What a worker leaves when it could not read its kernel start token, or predates publishing one."""
        root = self.temp / "state/bus"
        root.mkdir(parents=True, exist_ok=True)
        (root / "worker.json").write_text(json.dumps(
            {"pid": os.getpid(), "state": "running", "heartbeat_at": time.time(), "adapters": 0}))

    def status(self):
        proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", "status", "--no-start", "--json"],
                              env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return json.loads(proc.stdout)

    def test_status_reports_a_killed_worker_as_dead(self):
        pid = self.start_worker()
        self.kill_fixture_worker(pid)
        # A killed process cannot update its file: the file still claims a running worker.
        self.assertEqual(self.worker_file().get("state"), "running")
        status = self.status()
        self.assertEqual(status["worker"]["state"], "dead")
        self.assertEqual(status["worker"]["recorded_state"], "running")
        self.assertIs(status.get("worker_alive"), False)
        self.assertIs(status["worker_recent"], False)
        self.assertEqual(status["worker"]["pid"], pid)

    def test_status_reports_a_running_worker_as_alive(self):
        pid = self.start_worker()
        status = self.status()
        self.assertIs(status.get("worker_alive"), True)
        self.assertEqual(status["worker"]["state"], "running")
        self.assertNotIn("recorded_state", status["worker"])
        self.assertIs(status["worker_recent"], True)
        self.assertEqual(status["worker"]["pid"], pid)

    def test_status_reports_a_stopped_worker_as_stopped_not_dead(self):
        self.start_worker()
        stopped = subprocess.run([str(ROOT / "bin/communicate"), "bus", "stop"], env=self.env,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.wait_for(lambda: self.worker_file().get("state") == "stopped", "the worker publishes stopped")
        status = self.status()
        self.assertIs(status.get("worker_alive"), False)
        self.assertEqual(status["worker"]["state"], "stopped")
        self.assertNotIn("recorded_state", status["worker"])

    def test_status_with_no_worker_ever_started_starts_none(self):
        status = self.status()
        self.assertIs(status.get("worker_alive"), False)
        self.assertEqual(status["worker"], {})
        self.assertFalse((self.temp / "state/bus/worker.json").exists())

    def test_status_without_a_kernel_token_trusts_the_held_worker_lock(self):
        self.write_worker_file_without_kernel_token()
        self.hold_lock("worker")
        status = self.status()
        self.assertIs(status.get("worker_alive"), True)
        self.assertEqual(status["worker"]["state"], "running")
        self.assertIs(status["worker_recent"], True)

    def test_status_without_a_kernel_token_reports_dead_when_nothing_holds_the_worker_lock(self):
        self.write_worker_file_without_kernel_token()
        status = self.status()
        self.assertEqual(status["worker"]["state"], "dead")
        self.assertIs(status.get("worker_alive"), False)
        self.assertIs(status["worker_recent"], False)

    def test_status_leaves_the_worker_lock_alone_while_a_start_is_in_progress(self):
        self.write_worker_file_without_kernel_token()
        self.hold_lock("worker-start")
        status = self.status()
        self.assertIs(status.get("worker_alive"), True)
        self.assertEqual(status["worker"]["state"], "running")


if __name__ == "__main__":
    unittest.main()
