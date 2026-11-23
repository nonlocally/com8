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
sys.path.insert(0, str(ROOT / "lib"))
import bus


class WorkerFixture(unittest.TestCase):
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

    def write_registrations(self, *records):
        """Client state as a registration leaves it, for a hub address where nothing listens."""
        root = self.temp / "state/bus"
        root.mkdir(parents=True, exist_ok=True)
        url = "https://localhost:1"
        (root / "client.json").write_text(json.dumps(
            {"connections": {url: {"url": url, "token": "fixture-token"}}, "default": url}))
        (root / "registrations.json").write_text(json.dumps(
            {url + "|" + record["session_key"]: {**record, "url": url} for record in records}))

    def status(self):
        proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", "status", "--no-start", "--json"],
                              env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return json.loads(proc.stdout)


class WorkerLivenessTests(WorkerFixture):
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

    def test_a_registration_the_client_cannot_read_does_not_end_the_worker(self):
        # A Claude registration whose socket was saved as null: asking for its status raises.
        self.write_registrations({"id": "agent-unreadable", "session_key": "claude:unreadable", "kind": "claude",
                                  "name": "unreadable", "socket": None, "buses": ["general"]})
        code = "import sys; sys.path.insert(0, %r); import bus; bus.start_worker()" % str(ROOT / "lib")
        subprocess.run([sys.executable, "-c", code], env=self.env, check=True, capture_output=True, timeout=30)
        time.sleep(5)  # more than two turns of the worker's two-second loop
        status = self.status()
        self.assertEqual(status["worker"]["state"], "running")
        self.assertIs(status.get("worker_alive"), True)


    def test_a_worker_that_ends_on_an_error_leaves_a_dated_reason_in_its_log(self):
        root = self.temp / "state/bus"
        root.mkdir(parents=True, exist_ok=True)
        (root / "receipts.sqlite").write_bytes(b"this is not a database\n" * 64)
        code = "import sys; sys.path.insert(0, %r); import bus; bus.start_worker()" % str(ROOT / "lib")
        # The start reports its own failure; what the worker left behind is the subject here.
        subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, timeout=30)
        log = root / "worker.log"
        text = self.wait_for(lambda: log.exists() and "DatabaseError" in log.read_text() and log.read_text(),
                             "the worker's log names the error")
        self.assertRegex(text, r"(?m)^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ worker \d+ stopped by DatabaseError: ")

    def test_a_worker_stopped_on_request_leaves_dated_lines_in_its_log(self):
        pid = self.start_worker()
        stopped = subprocess.run([str(ROOT / "bin/communicate"), "bus", "stop"], env=self.env,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.wait_for(lambda: self.worker_file().get("state") == "stopped", "the worker publishes stopped")
        log = self.temp / "state/bus/worker.log"
        stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
        text = self.wait_for(lambda: "stopped" in log.read_text() and log.read_text(), "the worker's log says it stopped")
        self.assertRegex(text, r"(?m)^%s worker %d starting$" % (stamp, pid))
        self.assertRegex(text, r"(?m)^%s worker %d stopped on request$" % (stamp, pid))


class WorkerRevivalTests(WorkerFixture):
    """A command that lists agents brings back a worker that is gone without having been asked to stop."""

    def bus(self, *args):
        proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", *args], env=self.env,
                              capture_output=True, text=True, timeout=45)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return proc

    def register_fixture_agent(self):
        """A local hub, and one saved registration on it that is active (it belongs to a bus)."""
        self.bus("list")  # starts the temporary local hub and writes its connection
        root = self.temp / "state/bus"
        config = json.loads((root / "client.json").read_text())
        url = config["default"]
        (root / "registrations.json").write_text(json.dumps({url + "|codex:fixture": {
            "id": "agent-fixture", "session_key": "codex:fixture", "kind": "codex", "name": "fixture",
            "binary": "/usr/bin/true", "thread": "00000000-0000-4000-8000-000000000000",
            "buses": ["general"], "url": url}}))

    def test_listing_agents_restarts_a_worker_found_dead(self):
        self.register_fixture_agent()
        first = self.start_worker()
        self.kill_fixture_worker(first)
        self.assertIs(self.status().get("worker_alive"), False)
        listed = self.bus("agents", "--json")
        json.loads(listed.stdout)  # the listing itself is unchanged and still the only thing on stdout
        self.assertIn("worker was not running", listed.stderr)
        status = self.status()
        self.assertIs(status.get("worker_alive"), True)
        self.assertEqual(status["worker"]["state"], "running")
        self.assertNotEqual(status["worker"]["pid"], first)

    def test_listing_agents_leaves_a_stopped_worker_stopped(self):
        self.register_fixture_agent()
        self.start_worker()
        self.bus("stop")
        self.wait_for(lambda: self.worker_file().get("state") == "stopped", "the worker publishes stopped")
        listed = self.bus("agents", "--json")
        self.assertNotIn("worker", listed.stderr)
        status = self.status()
        self.assertIs(status.get("worker_alive"), False)
        self.assertEqual(status["worker"]["state"], "stopped")

    def test_listing_agents_with_no_registration_starts_no_worker(self):
        listed = self.bus("agents", "--json")
        self.assertNotIn("worker", listed.stderr)
        self.assertFalse((self.temp / "state/bus/worker.json").exists())


class RegistrationStatusTests(unittest.TestCase):
    def test_a_record_that_cannot_be_read_is_offline(self):
        for record in ({"kind": "claude", "socket": None},
                       {"kind": "claude"},
                       {"kind": "codex"},
                       {"kind": "codex", "binary": None},
                       {},
                       "not a record"):
            with self.subTest(record=record):
                self.assertEqual(bus.current_status(record), "offline")


if __name__ == "__main__":
    unittest.main()
