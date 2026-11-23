#!/usr/bin/env python3
"""The outbound worker's life: what status says of it, what ends it, and what brings it back.

The cases use a temporary state directory and, where a worker is needed, a real
detached one. No session and no network is involved: a worker with no
registrations still owns its lock and publishes its state.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import bus
from bus_service import consumer_process_identity

STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        # Resolved, because the worker receives the resolved state path in its arguments.
        self.temp = Path(tempfile.mkdtemp(prefix="bus-worker-liveness-")).resolve()
        home = self.temp / "home"
        home.mkdir()
        self.root = self.temp / "state/bus"
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
            return json.loads((self.root / "worker.json").read_text())
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

    def launch_worker(self):
        """Start a detached worker the way a command does; the start reports its own failure."""
        code = "import sys; sys.path.insert(0, %r); import bus; bus.start_worker()" % str(ROOT / "lib")
        return subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=30)

    def start_worker(self):
        started = self.launch_worker()
        self.assertEqual(started.returncode, 0, started.stderr)
        info = self.wait_for(lambda: self.worker_file() if self.worker_file().get("state") == "running" else None,
                             "the worker publishes running")
        return info["pid"]

    def kill_fixture_worker(self, pid, how=signal.SIGKILL):
        # The pid comes from this fixture's own state file; signal it only while
        # its command line still names this fixture's state directory.
        argv = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=5).stdout
        self.assertIn("__worker", argv)
        self.assertIn(self.env["COMM_STATE"], argv)
        os.kill(pid, how)

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False
        self.wait_for(gone, "the signalled worker has left the process table")

    def hold_lock(self, name):
        """Hold one of the bus state locks from this test process, as a worker or a starting command would."""
        self.root.mkdir(parents=True, exist_ok=True)
        stream = open(self.root / (name + ".lock"), "a+")
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held.append(stream)
        return stream

    def write_worker_file(self, state="running", pid=None, **more):
        """A worker's status file as a worker left it. The pid defaults to this test process: alive, and no worker."""
        self.root.mkdir(parents=True, exist_ok=True)
        info = {"pid": os.getpid() if pid is None else pid, "state": state, "heartbeat_at": time.time(),
                "adapters": 0, **more}
        (self.root / "worker.json").write_text(json.dumps(info))
        return info

    def write_registrations(self, *records):
        """Client state as a registration leaves it, for a hub address where nothing listens."""
        self.root.mkdir(parents=True, exist_ok=True)
        url = "https://localhost:1"
        (self.root / "client.json").write_text(json.dumps(
            {"connections": {url: {"url": url, "token": "fixture-token"}}, "default": url}))
        (self.root / "registrations.json").write_text(json.dumps(
            {url + "|" + record["session_key"]: {**record, "url": url} for record in records}))

    def bus(self, *args):
        proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", *args], env=self.env,
                              capture_output=True, text=True, timeout=45)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return proc

    def status(self, no_start=True):
        return json.loads(self.bus("status", "--json", *(("--no-start",) if no_start else ())).stdout)


class WorkerStatusTests(WorkerFixture):
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
        self.bus("stop")
        self.wait_for(lambda: self.worker_file().get("state") == "stopped", "the worker publishes stopped")
        status = self.status()
        self.assertIs(status.get("worker_alive"), False)
        self.assertEqual(status["worker"]["state"], "stopped")
        self.assertNotIn("recorded_state", status["worker"])

    def test_status_with_no_worker_ever_started_starts_none(self):
        status = self.status()
        self.assertIs(status.get("worker_alive"), False)
        self.assertEqual(status["worker"], {})
        self.assertFalse((self.root / "worker.json").exists())

    def test_status_trusts_whoever_holds_the_worker_lock(self):
        self.write_worker_file()
        self.hold_lock("worker")
        status = self.status()
        self.assertIs(status.get("worker_alive"), True)
        self.assertEqual(status["worker"]["state"], "running")
        self.assertIs(status["worker_recent"], True)

    def test_status_reports_dead_when_nothing_holds_the_worker_lock(self):
        # The recorded pid is this test process: it exists, and it is not a worker.
        for recorded in ("starting", "running"):
            with self.subTest(recorded=recorded):
                self.write_worker_file(state=recorded)
                status = self.status()
                self.assertEqual(status["worker"]["state"], "dead")
                self.assertEqual(status["worker"]["recorded_state"], recorded)
                self.assertIs(status.get("worker_alive"), False)
                self.assertIs(status["worker_recent"], False)

    def test_status_does_not_try_the_worker_lock_while_a_start_holds_the_start_lock(self):
        # Nothing holds the worker's lock here, so trying it would answer "dead".
        self.write_worker_file()
        self.hold_lock("worker-start")
        status = self.status()
        self.assertIs(status.get("worker_alive"), True)
        self.assertEqual(status["worker"]["state"], "running")

    def test_plain_status_reports_a_killed_worker_as_dead_and_leaves_it_so(self):
        self.bus("list")  # a temporary local hub, which plain status needs
        pid = self.start_worker()
        self.kill_fixture_worker(pid)
        status = self.status(no_start=False)
        self.assertEqual(status["worker"]["state"], "dead")
        self.assertIs(status.get("worker_alive"), False)
        self.assertEqual(self.status()["worker"]["state"], "dead")


class WorkerLockTests(WorkerFixture):
    """`worker_is_alive` itself, called in this process against the temporary state directory."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"COMM_STATE": self.env["COMM_STATE"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_live_process_that_does_not_hold_the_worker_lock_is_not_a_running_worker(self):
        # What an ended worker that is still in the process table leaves: its pid and
        # its kernel start token both still match a process.
        info = self.write_worker_file(process_identity=consumer_process_identity(os.getpid()))
        self.assertIs(bus.worker_is_alive(info), False)

    def test_another_inspection_holding_the_start_lock_for_a_moment_does_not_hide_a_dead_worker(self):
        info = self.write_worker_file()
        stream = self.hold_lock("worker-start")
        release = threading.Timer(0.1, stream.close)
        with mock.patch.object(bus, "START_LOCK_WAIT", 5.0):
            release.start()
            try:
                alive = bus.worker_is_alive(info)
            finally:
                release.join()
        self.assertIs(alive, False)

    def test_while_a_start_holds_the_start_lock_a_recorded_process_that_is_gone_is_dead(self):
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        info = self.write_worker_file(pid=ended.pid)
        self.hold_lock("worker-start")
        with mock.patch.object(bus, "START_LOCK_WAIT", 0.05):
            self.assertIs(bus.worker_is_alive(info), False)

    def test_while_a_start_holds_the_start_lock_a_recorded_process_that_exists_counts_as_alive(self):
        info = self.write_worker_file()
        self.hold_lock("worker-start")
        with mock.patch.object(bus, "START_LOCK_WAIT", 0.05):
            self.assertIs(bus.worker_is_alive(info), True)


class WorkerEndTests(WorkerFixture):
    def test_a_registration_whose_status_cannot_be_measured_does_not_end_the_worker(self):
        # A Claude registration whose socket was saved as null: asking for its status raises.
        self.write_registrations({"id": "agent-unreadable", "session_key": "claude:unreadable", "kind": "claude",
                                  "name": "unreadable", "socket": None, "buses": ["general"]})
        self.assertEqual(self.launch_worker().returncode, 0)
        time.sleep(5)  # more than two turns of the worker's two-second loop
        status = self.status()
        self.assertEqual(status["worker"]["state"], "running")
        self.assertIs(status.get("worker_alive"), True)
        # Said once in the log, not on every turn.
        lines = [line for line in (self.root / "worker.log").read_text().splitlines() if "agent-unreadable" in line]
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^%s worker \d+ registration agent-unreadable: status cannot be measured "
                                   r"\(TypeError: .*\); reported offline$" % STAMP)

    def test_saved_registrations_that_cannot_be_read_do_not_end_the_worker(self):
        self.write_registrations(
            {"id": "agent-window", "session_key": "codex:window", "kind": "codex", "buses": [], "reply_until": None},
            {"session_key": "codex:nameless", "kind": "codex", "buses": ["general"]},
            {"id": "agent-good", "session_key": "codex:good", "kind": "codex", "binary": "/usr/bin/true",
             "buses": ["general"]})
        saved = json.loads((self.root / "registrations.json").read_text())
        del saved["https://localhost:1|codex:nameless"]["url"]  # no id above, and now no hub either
        (self.root / "registrations.json").write_text(json.dumps(saved))
        self.assertEqual(self.launch_worker().returncode, 0)
        time.sleep(5)  # more than two turns of the worker's two-second loop
        status = self.status()
        self.assertEqual(status["worker"]["state"], "running")
        self.assertIs(status.get("worker_alive"), True)

    def test_a_worker_that_ends_on_an_error_leaves_a_dated_reason_in_its_log(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "receipts.sqlite").write_bytes(b"this is not a database\n" * 64)
        self.launch_worker()
        log = self.root / "worker.log"
        text = self.wait_for(lambda: log.exists() and "DatabaseError" in log.read_text() and log.read_text(),
                             "the worker's log names the error")
        self.assertRegex(text, r"(?m)^%s worker \d+ stopped by DatabaseError: " % STAMP)

    def test_a_worker_asked_to_stop_leaves_dated_lines_in_its_log(self):
        pid = self.start_worker()
        self.bus("stop")
        self.wait_for(lambda: self.worker_file().get("state") == "stopped", "the worker publishes stopped")
        log = self.root / "worker.log"
        text = self.wait_for(lambda: "stopped" in log.read_text() and log.read_text(), "the worker's log says it stopped")
        self.assertRegex(text, r"(?m)^%s worker %d starting$" % (STAMP, pid))
        self.assertRegex(text, r"(?m)^%s worker %d stopped without an error$" % (STAMP, pid))

    def test_a_second_worker_gives_way_and_says_so_without_claiming_an_error(self):
        self.hold_lock("worker")
        second = subprocess.run([sys.executable, str(ROOT / "lib/bus.py"), "__worker"], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertRegex(second.stderr, r"(?m)^%s worker \d+ starting$" % STAMP)
        self.assertRegex(second.stderr, r"(?m)^%s worker \d+ stopped without an error$" % STAMP)
        self.assertFalse((self.root / "worker.json").exists())


class WorkerRevivalTests(WorkerFixture):
    """A command that lists agents brings back a worker that is gone without having been asked to stop."""

    def register_fixture_agent(self):
        """A local hub, and one saved registration on it that is active (it belongs to a bus)."""
        self.bus("list")  # starts the temporary local hub and writes its connection
        config = json.loads((self.root / "client.json").read_text())
        url = config["default"]
        (self.root / "registrations.json").write_text(json.dumps({url + "|codex:fixture": {
            "id": "agent-fixture", "session_key": "codex:fixture", "kind": "codex", "name": "fixture",
            "binary": "/usr/bin/true", "thread": "00000000-0000-4000-8000-000000000000",
            "buses": ["general"], "url": url}}))

    def test_listing_restarts_a_worker_found_dead(self):
        self.register_fixture_agent()
        for command in ("agents", "list"):
            with self.subTest(command=command):
                before = self.start_worker()
                self.kill_fixture_worker(before)
                self.assertIs(self.status().get("worker_alive"), False)
                listed = self.bus(command, "--json")
                json.loads(listed.stdout)  # the listing itself is unchanged and still the only thing on stdout
                self.assertIn("worker was not running", listed.stderr)
                status = self.status()
                self.assertIs(status.get("worker_alive"), True)
                self.assertEqual(status["worker"]["state"], "running")
                self.assertNotEqual(status["worker"]["pid"], before)

    def test_listing_restarts_a_worker_that_a_signal_ended(self):
        # A worker told to end by the system, at logout or shutdown, writes "stopped": nobody asked with `bus stop`.
        self.register_fixture_agent()
        before = self.start_worker()
        self.kill_fixture_worker(before, signal.SIGTERM)
        self.wait_for(lambda: self.worker_file().get("state") == "stopped", "the worker publishes stopped")
        self.bus("agents", "--json")
        self.assertIs(self.status().get("worker_alive"), True)

    def test_listing_leaves_a_worker_stopped_on_request_stopped(self):
        self.register_fixture_agent()
        self.start_worker()
        self.bus("stop")
        self.wait_for(lambda: self.worker_file().get("state") == "stopped", "the worker publishes stopped")
        listed = self.bus("agents", "--json")
        self.assertNotIn("worker", listed.stderr)
        status = self.status()
        self.assertIs(status.get("worker_alive"), False)
        self.assertEqual(status["worker"]["state"], "stopped")

    def test_listing_restarts_the_worker_past_a_saved_registration_it_cannot_read(self):
        self.register_fixture_agent()
        saved = json.loads((self.root / "registrations.json").read_text())
        url = next(iter(saved.values()))["url"]
        # Read first: its reply window is not a number.
        unreadable = {url + "|codex:unreadable": {"id": "agent-unreadable", "session_key": "codex:unreadable",
                                                  "kind": "codex", "buses": [], "reply_until": None, "url": url}}
        (self.root / "registrations.json").write_text(json.dumps({**unreadable, **saved}))
        self.kill_fixture_worker(self.start_worker())
        listed = self.bus("agents", "--json")
        self.assertIn("worker was not running", listed.stderr)
        self.assertIs(self.status().get("worker_alive"), True)

    def test_listing_with_no_registration_starts_no_worker(self):
        listed = self.bus("agents", "--json")
        self.assertNotIn("worker", listed.stderr)
        self.assertFalse((self.root / "worker.json").exists())

    def test_a_listing_is_still_given_when_the_worker_cannot_be_started(self):
        self.register_fixture_agent()
        self.kill_fixture_worker(self.start_worker())
        # The worker's log is opened before the worker is started: make that fail.
        (self.root / "worker.log").unlink()
        (self.root / "worker.log").mkdir()
        listed = self.bus("agents", "--json")
        self.assertTrue(json.loads(listed.stdout)["ok"])
        self.assertIn("could not be started", listed.stderr)
        self.assertIs(self.status().get("worker_alive"), False)

    def test_a_listing_is_still_given_when_the_saved_registrations_cannot_be_read(self):
        self.register_fixture_agent()
        (self.root / "registrations.json").write_text("[]")
        listed = self.bus("agents", "--json")
        self.assertTrue(json.loads(listed.stdout)["ok"])
        self.assertIn("could not be started", listed.stderr)


class ServedRegistrationTests(unittest.TestCase):
    def test_a_registration_in_a_bus_or_inside_a_reply_window_is_served(self):
        base = {"id": "agent", "url": "https://hub.example"}
        self.assertIs(bus.active_adapter({**base, "buses": ["general"]}), True)
        self.assertIs(bus.active_adapter({**base, "buses": [], "reply_until": time.time() + 60}), True)
        self.assertIs(bus.active_adapter({**base, "buses": [], "reply_until": time.time() - 60}), False)
        self.assertIs(bus.active_adapter({**base, "buses": ["general"], "retired": True}), False)

    def test_a_saved_registration_that_cannot_be_read_is_not_served(self):
        for record in ("not a record",
                       None,
                       {"buses": ["general"]},
                       {"id": "agent", "buses": ["general"]},
                       {"url": "https://hub.example", "buses": ["general"]},
                       {"id": 7, "url": "https://hub.example", "buses": ["general"]},
                       {"id": "agent", "url": "https://hub.example", "buses": [], "reply_until": None},
                       {"id": "agent", "url": "https://hub.example", "buses": [], "reply_until": "soon"}):
            with self.subTest(record=record):
                self.assertIs(bus.active_adapter(record), False)


class RegistrationStatusTests(unittest.TestCase):
    def test_a_registration_of_a_kind_this_client_does_not_know_is_offline(self):
        # It carries a socket that answers, so reading it as a Claude registration would call it live.
        with tempfile.TemporaryDirectory(prefix="bus-kind-") as temp, socket.socket(socket.AF_UNIX) as listener:
            path = str(Path(temp) / "s")
            listener.bind(path)
            listener.listen(16)  # each status asked below leaves one connection waiting
            self.assertEqual(bus.current_status({"id": "agent-claude", "kind": "claude", "socket": path}), "live")
            with mock.patch.object(bus, "worker_note") as note:
                self.assertEqual(bus.current_status({"id": "agent-new", "kind": "a-later-kind", "socket": path}),
                                 "offline")
            note.assert_not_called()  # expected of a newer client's record; nothing to report

    def test_two_threads_asking_at_once_say_it_once(self):
        # The worker asks from its heartbeat thread and its delivery loop. This set holds
        # each asker's answer to "said already?" until a second asker has its own, or a moment passes.
        together = threading.Barrier(2)

        class Meeting(set):
            def __contains__(self, key):
                said = set.__contains__(self, key)
                try:
                    together.wait(timeout=0.5)
                except threading.BrokenBarrierError:
                    pass
                return said

        record = {"id": "agent-twice", "kind": "claude", "socket": None}
        with mock.patch.object(bus, "UNMEASURED", Meeting()), mock.patch.object(bus, "worker_note") as note:
            askers = [threading.Thread(target=bus.current_status, args=(record,)) for _ in range(2)]
            for asker in askers:
                asker.start()
            for asker in askers:
                asker.join()
        self.assertEqual(note.call_count, 1)

    def test_a_record_whose_status_cannot_be_measured_is_offline(self):
        for record in ({"kind": "claude", "socket": None},
                       {"kind": "claude"},
                       {"kind": "claude", "socket": "an\x00embedded null"},
                       {"kind": "codex"},
                       {"kind": "codex", "binary": None},
                       {"kind": "codex", "binary": "an\x00embedded null"},
                       {},
                       "not a record"):
            with self.subTest(record=record), mock.patch.object(bus, "worker_note"):
                self.assertEqual(bus.current_status(record), "offline")


if __name__ == "__main__":
    unittest.main()
