#!/usr/bin/env python3
"""A live socket is insufficient when its original Claude session disappears."""
import json
import base64
import io
import contextlib
import http.client
import os
from pathlib import Path
import socket
import sys
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import bus


class EnrollmentInputTests(unittest.TestCase):
    def setUp(self):
        self.secret = "private-fixture-invitation"
        self.code = "commbus1." + base64.urlsafe_b64encode(json.dumps({
            "url": "https://hub.example", "invite": self.secret}).encode()).decode().rstrip("=")

    def test_stdin_invitation_uses_existing_redemption_and_never_returns_token(self):
        args = bus.parser().parse_args(["connect", "--invite-stdin", "--device", "test-device"])
        with mock.patch.object(bus.sys, "stdin", io.StringIO(self.code + "\n")), \
                mock.patch.object(bus, "config", return_value={"connections": {}}), \
                mock.patch.object(bus, "device_metadata", return_value={}), \
                mock.patch.object(bus, "request", return_value={"ok": True, "token": "issued-secret", "principal": "device-1", "buses": ["general"], "user": "owner"}) as request, \
                mock.patch.object(bus, "save_connection") as save:
            result = bus.run(args)
        request.assert_called_once_with({"url": "https://hub.example"}, "redeem", invite=self.secret, device="test-device", device_metadata={})
        self.assertEqual(save.call_args.args[0]["token"], "issued-secret")
        self.assertEqual(result["user"], "owner")
        self.assertNotIn("issued-secret", json.dumps(result))
        self.assertNotIn(self.secret, json.dumps(result))

    def test_stdin_rejects_missing_oversized_or_conflicting_code_before_any_state(self):
        for argv, value in [(["connect"], ""), (["connect", "--invite-stdin"], ""),
                            (["connect", "--invite-stdin"], "x" * 8194),
                            (["connect", self.code, "--invite-stdin"], self.code)]:
            with self.subTest(argv=argv[:2]), mock.patch.object(bus.sys, "stdin", io.StringIO(value)), \
                    mock.patch.object(bus, "config") as config, mock.patch.object(bus, "request") as request:
                with self.assertRaises(bus.BusError) as error:
                    bus.run(bus.parser().parse_args(argv))
                self.assertNotIn(self.secret, str(error.exception))
                self.assertNotIn(self.code, str(error.exception))
                config.assert_not_called()
                request.assert_not_called()

    def test_legacy_invitation_argument_remains_supported_without_reading_stdin(self):
        args = bus.parser().parse_args(["connect", self.code])
        stdin = mock.Mock()
        with mock.patch.object(bus.sys, "stdin", stdin), \
                mock.patch.object(bus, "config", return_value={"connections": {}}), \
                mock.patch.object(bus, "device_metadata", return_value={"hostname": "fixture"}), \
                mock.patch.object(bus, "request", return_value={"ok": True, "token": "issued", "principal": "p", "buses": ["general"]}), \
                mock.patch.object(bus, "save_connection"):
            self.assertTrue(bus.run(args)["ok"])
        stdin.read.assert_not_called()

    def test_local_selection_without_start_preserves_connections_and_registrations(self):
        for has_local in [False, True]:
            with self.subTest(has_local=has_local), tempfile.TemporaryDirectory(prefix="bus-select-", dir="/tmp") as directory:
                root = Path(directory)
                registrations = root / "registrations.json"
                registrations.write_text('{"existing":"adapter survives"}')
                remote = {"url": "https://hub.example", "token": "saved-secret", "principal": "device-1"}
                cfg = {"connections": {remote["url"]: remote}, "default": remote["url"]}
                if has_local:
                    cfg["connections"]["http://127.0.0.1:7777"] = {"url": "http://127.0.0.1:7777", "token": "local-secret", "local": True}
                before = json.loads(json.dumps(cfg["connections"]))
                with mock.patch.object(bus, "state_dir", return_value=root), mock.patch.object(bus, "config", return_value=cfg), \
                        mock.patch.object(bus, "locked", return_value=contextlib.nullcontext()), \
                        mock.patch.object(bus, "local_connection") as local, mock.patch.object(bus, "spawn_daemon") as spawn, \
                        mock.patch.object(bus, "start_worker") as worker, mock.patch.object(bus, "stop_services") as stop, \
                        mock.patch.object(bus, "request") as request:
                    result = bus.run(bus.parser().parse_args(["use", "local", "--no-start"]))
                self.assertFalse(result["started"])
                saved = json.loads((root / "client.json").read_text())
                self.assertEqual(saved["connections"], before)
                self.assertEqual(saved["default"], "http://127.0.0.1:7777" if has_local else None)
                self.assertEqual(registrations.read_text(), '{"existing":"adapter survives"}')
                for call in [local, spawn, worker, stop, request]:
                    call.assert_not_called()

    def test_no_start_does_not_weaken_remote_selection_rules(self):
        with mock.patch.object(bus, "config") as config:
            with self.assertRaisesRegex(bus.BusError, "only.*local"):
                bus.run(bus.parser().parse_args(["use", "https://not-enrolled.example", "--no-start"]))
        config.assert_not_called()


# The worker as its own process, with one hub whose heartbeat never answers.
# argv: the lib folder, the state folder, and a file to create once the call has begun.
STALLED_WORKER = """
import sys, time
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import bus
state, entered = Path(sys.argv[2]), Path(sys.argv[3])
url = "https://stall.invalid"
record = {"id": "agent-a", "kind": "codex", "binary": sys.executable, "url": url, "buses": ["general"]}

def request(connection, operation, **payload):
    if operation == "heartbeat":
        entered.touch()
        time.sleep(120)
        return {"ok": True}
    if operation == "poll_device":
        return {"messages": []}
    raise AssertionError(operation)

with mock.patch.object(bus, "state_dir", return_value=state), \\
        mock.patch.object(bus, "config", return_value={"connections": {url: {"url": url}}}), \\
        mock.patch.object(bus, "registrations", return_value={"a": record}), \\
        mock.patch.object(bus, "current_status", return_value="queueable"), \\
        mock.patch.object(bus, "request", side_effect=request), \\
        mock.patch.object(bus, "HEARTBEAT_TICK", 0.05):
    bus.worker()
"""


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bus-life-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)})
        self.env.start()
        self.listeners = []
        self.original_socket = self.listener("original.sock")
        self.record = {"kind": "claude", "session_id": "original-session", "socket": self.original_socket}

    def tearDown(self):
        self.env.stop()
        for listener in self.listeners:
            listener.close()
        self.temp.cleanup()

    def listener(self, filename):
        path = str(self.root / filename)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(8)
        self.listeners.append(listener)
        return path

    def sidecar(self, pid, session_id, path):
        (self.sessions / (str(pid) + ".json")).write_text(json.dumps({
            "sessionId": session_id, "messagingSocketPath": path, "name": "same-name", "pid": pid}))

    def test_removed_sidecar_does_not_follow_still_listening_old_path(self):
        self.assertTrue(bus.probe(self.original_socket))
        self.assertEqual(bus.current_status(self.record), "offline")

    def test_other_session_reusing_path_or_name_is_not_registered_session(self):
        self.sidecar(101, "different-session", self.original_socket)
        self.assertTrue(bus.probe(self.original_socket))
        self.assertEqual(bus.current_status(self.record), "offline")

    def test_ambiguous_original_session_fails_closed(self):
        resumed = self.listener("resumed.sock")
        self.sidecar(101, "original-session", self.original_socket)
        self.sidecar(102, "original-session", resumed)
        self.assertEqual(bus.current_status(self.record), "offline")

    def test_resume_follows_only_exact_original_session(self):
        resumed = self.listener("resumed.sock")
        self.sidecar(101, "original-session", resumed)
        self.sidecar(102, "same-name-other-session", self.original_socket)
        self.assertEqual(bus.current_status(self.record), "live")
        self.assertEqual(self.record["socket"], resumed)

    def test_resumed_self_uses_original_session_id_before_reregistration(self):
        resumed = self.listener("resumed-self.sock")
        self.sidecar(101, "original-session", resumed)
        record = dict(self.record, url="https://hub.invalid", id="agent-1", name="alias", buses=["general"])
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": resumed, "CODEX_THREAD_ID": ""}), \
                mock.patch.object(bus, "registrations", return_value={"one": record}):
            self.assertEqual(bus.local_agent({"url": record["url"]})["id"], "agent-1")

    def test_registered_config_root_does_not_drift_with_worker_environment(self):
        self.sidecar(101, "original-session", self.original_socket)
        record = dict(self.record, claude_config_dir=str(self.root))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "different-config")}):
            self.assertEqual(bus.current_status(record), "live")

    def test_worker_upgrade_timeout_does_not_start_competitor_or_stop_broker(self):
        state = self.root / "blocked-upgrade"
        state.mkdir()
        (state / "worker.json").write_text(json.dumps({"runtime": "older-version"}))
        def lock(name, **_kwargs):
            return contextlib.nullcontext(name != "worker")
        with mock.patch.object(bus, "state_dir", return_value=state), \
                mock.patch.object(bus, "locked", side_effect=lock), \
                mock.patch.object(bus.time, "monotonic", side_effect=[0, 10, 20, 31]), \
                mock.patch.object(bus.time, "sleep"), mock.patch.object(bus, "spawn_daemon") as spawn:
            with self.assertRaisesRegex(bus.BusError, "did not stop within 30 seconds"):
                bus.start_worker()
        spawn.assert_not_called()
        self.assertTrue((state / "worker.stop").exists())
        self.assertFalse((state / "broker.stop").exists())

    def test_device_poll_batches_hidden_adapters_and_journals_other_ack_failures(self):
        state = self.root / "batched-worker"
        state.mkdir()
        url = "https://batch.invalid"
        records = {str(i): {"id": "agent-" + str(i), "kind": "codex", "binary": sys.executable,
                           "url": url, "buses": [], "reply_until": time.time() + 60} for i in range(3)}
        records["2"]["reply_until"] = time.time() - 1
        records["3"] = dict(records["2"], id="agent-3", buses=["general"])
        cfg = {"connections": {url: {"url": url}}}
        polls, delivered, acknowledged, errors = [], [], [], []
        barrier = threading.Barrier(2)

        def request(conn, operation, **payload):
            if operation == "heartbeat":
                self.assertNotIn("agent-2", {entry["id"] for entry in payload["agents"]})
                return {"ok": True}
            if operation == "poll_device":
                polls.append(payload["agents"])
                return {"messages": [{"id": "message-" + str(i), "target": "agent-" + str(i), "lease": "lease"}
                                     for i in range(2)] if len(polls) == 1 else []}
            if operation == "ack":
                acknowledged.append(payload["id"])
                if len(acknowledged) == 2:
                    (state / "worker.stop").touch()
                if payload["id"] == "message-0":
                    raise bus.BusError("simulated lost ACK")
                return {"ok": True}
            raise AssertionError(operation)

        def deliver(record, envelope):
            barrier.wait(timeout=3)
            delivered.append((record["id"], envelope["target"]))
            return "queued", "fixture queued"

        def run():
            try:
                bus.worker()
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(bus, "state_dir", return_value=state), \
                mock.patch.object(bus, "config", return_value=cfg), \
                mock.patch.object(bus, "registrations", return_value=records), \
                mock.patch.object(bus, "request", side_effect=request), \
                mock.patch.object(bus, "deliver", side_effect=deliver), mock.patch.object(bus.signal, "signal"):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(6)
            (state / "worker.stop").touch()
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(polls), 1)
        self.assertEqual(set(polls[0]), {"agent-0", "agent-1", "agent-3"})
        self.assertEqual(set(delivered), {("agent-0", "agent-0"), ("agent-1", "agent-1")})
        self.assertEqual(set(acknowledged), {"message-0", "message-1"})
        with sqlite3.connect(state / "receipts.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM delivered WHERE status='queued'").fetchone()[0], 2)

    def test_worker_is_ready_before_network_and_heartbeats_continue_during_delivery(self):
        state = self.root / "worker-state"
        state.mkdir()
        good = {"id": "good-agent", "kind": "codex", "binary": sys.executable,
                "url": "https://good.invalid", "buses": ["general"]}
        slow = {"id": "slow-agent", "kind": "codex", "binary": sys.executable,
                "url": "https://slow.invalid", "buses": ["general"]}
        records = {"good": good, "slow": slow}
        cfg = {"connections": {record["url"]: {"url": record["url"]} for record in records.values()}}
        slow_started, release_slow = threading.Event(), threading.Event()
        delivering, release_delivery = threading.Event(), threading.Event()
        healthy_once, healthy_twice = threading.Event(), threading.Event()
        counts = {"heartbeats": 0, "polls": 0}
        worker_errors = []

        def fake_request(connection, operation, **payload):
            if operation == "heartbeat":
                if connection["url"] == slow["url"]:
                    slow_started.set()
                    if not release_slow.wait(8):
                        raise bus.BusError("simulated unreachable hub")
                else:
                    counts["heartbeats"] += 1
                    healthy_once.set()
                    if counts["heartbeats"] >= 2:
                        healthy_twice.set()
                return {"ok": True}
            if operation == "poll_device":
                counts["polls"] += 1
                return {"messages": [{"id": "message-1", "lease": "lease", "target": payload["agents"][0]}] if counts["polls"] == 1 else []}
            if operation == "ack":
                return {"ok": True}
            raise AssertionError(operation)

        def fake_deliver(record, envelope):
            delivering.set()
            if not release_delivery.wait(8):
                raise bus.BusError("simulated slow delivery")
            return "queued", "fixture accepted"

        def run_worker():
            try:
                bus.worker()
            except Exception as exc:
                worker_errors.append(exc)

        # This test is about the ticker, not the cadence: with no interval,
        # every tick sends a heartbeat even though no status changes.
        with mock.patch.object(bus, "state_dir", return_value=state), \
                mock.patch.object(bus, "config", return_value=cfg), \
                mock.patch.object(bus, "registrations", return_value=records), \
                mock.patch.object(bus, "request", side_effect=fake_request), \
                mock.patch.object(bus, "deliver", side_effect=fake_deliver), \
                mock.patch.object(bus, "HEARTBEAT_INTERVAL", 0), \
                mock.patch.object(bus.signal, "signal"):
            worker = threading.Thread(target=run_worker, daemon=True)
            worker.start()
            try:
                self.assertTrue(slow_started.wait(2))
                self.assertTrue(delivering.wait(2))
                self.assertTrue(healthy_once.wait(2))
                initial = json.loads((state / "worker.json").read_text())
                self.assertEqual(initial["pid"], os.getpid())
                self.assertFalse(release_slow.is_set())
                first_heartbeat = initial["heartbeat_at"]
                release_slow.set()
                self.assertTrue(healthy_twice.wait(5), "delivery blocked the independent heartbeat ticker")
                self.assertFalse(release_delivery.is_set())
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    latest = json.loads((state / "worker.json").read_text())
                    if latest["heartbeat_at"] > first_heartbeat:
                        break
                    time.sleep(.01)
                self.assertGreater(latest["heartbeat_at"], first_heartbeat)
            finally:
                (state / "worker.stop").touch()
                release_slow.set()
                release_delivery.set()
                worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker_errors, [])
            self.assertEqual(json.loads((state / "worker.json").read_text())["state"], "stopped")

    @contextlib.contextmanager
    def beating_worker(self, name, tick, interval, records=None, urls=("https://beat.invalid",), hook=None):
        """Run the worker against fake hubs, recording each heartbeat request a hub accepts.

        hook(fixture, url, agents) runs inside every heartbeat request before it
        is recorded; it may block, or raise to refuse the request.
        """
        state = self.root / name
        state.mkdir()
        # The registrations file exists, as on any registered machine, so that a
        # test's rewrite of it is a rewrite and not a creation.
        bus.write_json(state / "registrations.json", {})
        if records is None:
            records = {"a": {"id": "agent-a", "kind": "codex", "binary": sys.executable,
                             "url": urls[0], "buses": ["general"]}}
        fixture = {"state": state, "status": {}, "beats": [], "refusals": [], "records": records,
                   "rewrite": lambda: bus.write_json(state / "registrations.json", {})}
        cfg = {"connections": {url: {"url": url} for url in urls}}
        heard = threading.Condition()
        worker_errors = []

        def fake_request(connection, operation, **payload):
            if operation == "heartbeat":
                agents = [(entry["id"], entry["status"], entry.get("epoch")) for entry in payload["agents"]]
                try:
                    if hook is not None:
                        hook(fixture, connection["url"], agents)
                except Exception:
                    with heard:
                        fixture["refusals"].append((time.monotonic(), connection["url"], agents))
                        heard.notify_all()
                    raise
                with heard:
                    fixture["beats"].append((time.monotonic(), connection["url"], agents))
                    heard.notify_all()
                return {"ok": True}
            if operation == "poll_device":
                return {"messages": []}
            raise AssertionError(operation)

        def wait_for(predicate, timeout):
            with heard:
                return heard.wait_for(lambda: predicate(fixture["beats"]), timeout)

        def worker_file():
            return json.loads((state / "worker.json").read_text())

        def until(predicate, timeout):
            """Wait until predicate(worker.json) holds."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    if predicate(worker_file()):
                        return True
                except (OSError, ValueError):
                    pass
                time.sleep(0.01)
            return False

        def run_worker():
            try:
                bus.worker()
            except Exception as exc:
                worker_errors.append(exc)

        fixture.update(wait_for=wait_for, worker=worker_file, until=until)
        with mock.patch.object(bus, "state_dir", return_value=state), \
                mock.patch.object(bus, "config", return_value=cfg), \
                mock.patch.object(bus, "registrations", side_effect=lambda: dict(fixture["records"])), \
                mock.patch.object(bus, "current_status",
                                  side_effect=lambda record: fixture["status"].get(record["id"], "queueable")), \
                mock.patch.object(bus, "request", side_effect=fake_request), \
                mock.patch.object(bus, "HEARTBEAT_TICK", tick), \
                mock.patch.object(bus, "HEARTBEAT_INTERVAL", interval), \
                mock.patch.object(bus.signal, "signal"):
            worker = threading.Thread(target=run_worker, daemon=True)
            worker.start()
            try:
                yield fixture
            finally:
                (state / "worker.stop").touch()
                worker.join(8)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker_errors, [])

    def test_a_hub_hears_unchanged_statuses_once_per_interval_and_any_change_at_once(self):
        interval = 2.0
        clocks, real_due = [], bus.heartbeat_due

        def due(accepted, now_monotonic, now_wall):
            clocks.append((now_monotonic - time.monotonic(), now_wall - time.time()))
            return real_due(accepted, now_monotonic, now_wall)

        with mock.patch.object(bus, "heartbeat_due", side_effect=due), \
                self.beating_worker("beat-cadence", tick=0.02, interval=interval) as fixture:
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 1, 3), "the first heartbeat never came")
            self.assertTrue(fixture["until"](lambda info: info.get("heartbeat_success_at"), 2))
            first = fixture["worker"]()
            time.sleep(0.8)
            self.assertEqual(len(fixture["beats"]), 1, "unchanged statuses were sent again inside the interval")
            quiet = fixture["worker"]()
            # Worker liveness (worker_recent) reads heartbeat_at, so every tick records it.
            self.assertGreater(quiet["heartbeat_at"], first["heartbeat_at"])
            self.assertEqual(quiet["heartbeat_success_at"], first["heartbeat_success_at"])
            self.assertEqual(quiet["adapters"], 1)
            # The due check gets the monotonic clock and the wall clock, in that order.
            self.assertTrue(clocks)
            self.assertTrue(all(abs(mono) < 5 and abs(wall) < 5 for mono, wall in clocks), clocks[:3])

            fixture["status"]["agent-a"] = "live"
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 2, 1), "a status change waited for the interval")
            one, two = fixture["beats"][:2]
            self.assertLess(two[0] - one[0], interval)
            self.assertEqual(two[2], [("agent-a", "live", None)])

            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 3, interval + 2),
                            "unchanged statuses were never sent again")
            three = fixture["beats"][2]
            self.assertGreaterEqual(three[0] - two[0], interval - 0.1)
            self.assertEqual(three[2], [("agent-a", "live", None)])

            fixture["records"] = dict(fixture["records"], b=dict(fixture["records"]["a"], id="agent-b"))
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 4, 1), "a new registration waited for the interval")
            four = fixture["beats"][3]
            self.assertLess(four[0] - three[0], interval)
            self.assertEqual(four[2], [("agent-a", "live", None), ("agent-b", "queueable", None)])

    def test_an_epoch_a_removed_registration_or_a_rewritten_registrations_file_goes_at_once(self):
        url = "https://beat.invalid"
        records = {"a": {"id": "agent-a", "kind": "codex", "binary": sys.executable, "url": url, "buses": ["general"]},
                   "s": {"id": "service-s", "kind": "service", "url": url, "buses": ["general"],
                         "fence_protocol": bus.SERVICE_FENCE_PROTOCOL, "epoch": 1}}
        with self.beating_worker("beat-changes", tick=0.05, interval=30, records=records) as fixture:
            count = lambda n: (lambda beats: len(beats) >= n)
            self.assertTrue(fixture["wait_for"](count(1), 3))
            self.assertEqual(fixture["beats"][0][2], [("agent-a", "queueable", None), ("service-s", "queueable", 1)])

            fixture["records"] = dict(records, s=dict(records["s"], epoch=2))
            self.assertTrue(fixture["wait_for"](count(2), 1), "a new service epoch waited for the interval")
            self.assertEqual(fixture["beats"][1][2], [("agent-a", "queueable", None), ("service-s", "queueable", 2)])

            fixture["records"] = {"s": dict(records["s"], epoch=2)}
            self.assertTrue(fixture["wait_for"](count(3), 1), "a removed registration waited for the interval")
            self.assertEqual(fixture["beats"][2][2], [("service-s", "queueable", 2)])

            # Another command rewrote the registrations file, the same bytes as
            # before, and may have told the hub a status.
            fixture["rewrite"]()
            self.assertTrue(fixture["wait_for"](count(4), 1), "a rewritten registrations file waited for the interval")
            self.assertEqual(fixture["beats"][3][2], [("service-s", "queueable", 2)])

            # A hub that has no adapters for a while is told everything again when one returns.
            kept = fixture["records"]
            fixture["records"] = {}
            self.assertTrue(fixture["until"](lambda info: info.get("adapters") == 0, 3))
            fixture["records"] = kept
            self.assertTrue(fixture["wait_for"](count(5), 1), "a returning hub waited for the interval")
            time.sleep(0.3)
            self.assertEqual(len(fixture["beats"]), 5)

    def test_a_heartbeat_in_flight_when_the_registrations_file_changes_is_followed_by_another(self):
        release, entered = threading.Event(), threading.Event()

        def hold_the_second(fixture, url, agents):
            if len(fixture["beats"]) == 1 and not release.is_set():
                entered.set()
                release.wait(3)

        with self.beating_worker("beat-inflight", tick=0.05, interval=30, hook=hold_the_second) as fixture:
            try:
                self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 1, 3))
                fixture["status"]["agent-a"] = "live"
                self.assertTrue(entered.wait(2), "the status change started no heartbeat")
                fixture["rewrite"]()
                # Ticks see the rewrite while the hub still holds the call.
                time.sleep(0.5)
            finally:
                release.set()
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 3, 1),
                            "a heartbeat that began before the registrations file changed was taken as current")
            self.assertEqual(fixture["beats"][2][2], [("agent-a", "live", None)])

    def test_a_hub_that_refuses_heartbeats_is_shown_failing_and_asked_again_at_each_tick(self):
        refusing = threading.Event()
        refusing.set()

        def refuse_while_set(fixture, url, agents):
            if refusing.is_set():
                raise bus.BusError("simulated hub outage")

        with self.beating_worker("beat-retry", tick=0.1, interval=30, hook=refuse_while_set) as fixture:
            self.assertTrue(fixture["until"](lambda info: info.get("heartbeat_errors"), 3), "a failing hub was not reported")
            failing = fixture["worker"]()
            self.assertEqual(failing["adapters"], 0)
            self.assertEqual(failing["heartbeat_success_at"], 0)
            self.assertEqual(failing["heartbeat_errors"][0]["hub"], "https://beat.invalid")
            self.assertIn("simulated hub outage", failing["heartbeat_errors"][0]["error"])
            # It is asked again at each tick: about one refusal for each tick that worker.json shows.
            ticks, before = set(), len(fixture["refusals"])
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                try:
                    ticks.add(fixture["worker"]()["heartbeat_at"])
                except (OSError, ValueError):
                    pass
                time.sleep(0.005)
            asked = len(fixture["refusals"]) - before
            self.assertGreaterEqual(asked, (len(ticks) - 2) * 0.6, "asked %d times in %d ticks" % (asked, len(ticks)))

            refusing.clear()
            cleared = time.monotonic()
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 1, 3))
            self.assertLess(fixture["beats"][0][0] - cleared, 0.6, "a hub that failed waited more than a few ticks")
            self.assertTrue(fixture["until"](lambda info: info.get("heartbeat_errors") == [] and info.get("adapters") == 1
                                             and info.get("heartbeat_success_at", 0) > 0, 3))
            time.sleep(0.5)
            self.assertEqual(len(fixture["beats"]), 1, "an accepted heartbeat was sent again inside the interval")

    def test_a_hub_that_goes_away_takes_its_error_with_it(self):
        def refuse(fixture, url, agents):
            raise bus.BusError("simulated hub outage")

        with self.beating_worker("beat-gone", tick=0.05, interval=30, hook=refuse) as fixture:
            self.assertTrue(fixture["until"](lambda info: info.get("heartbeat_errors"), 3))
            fixture["records"] = {}
            self.assertTrue(fixture["until"](lambda info: info.get("heartbeat_errors") == [] and info.get("adapters") == 0, 3),
                            "a hub with no adapters kept its error")

    def test_anything_a_hubs_call_raises_is_that_hubs_failure_and_the_ticker_goes_on(self):
        class Abandoned(BaseException):
            pass

        raising = {"error": None}

        def raise_while_set(fixture, url, agents):
            if raising["error"] is not None:
                raise raising["error"]

        with self.beating_worker("beat-unexpected", tick=0.05, interval=30, hook=raise_while_set) as fixture:
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 1, 3))
            # An http.client error, outside the kinds the loop names, on a changed status.
            raising["error"] = http.client.IncompleteRead(b"")
            fixture["status"]["agent-a"] = "live"
            self.assertTrue(fixture["until"](lambda info: "IncompleteRead" in json.dumps(info.get("heartbeat_errors")), 3),
                            "an http.client error was not reported as that hub's failure")
            self.assertEqual(fixture["worker"]()["adapters"], 0)
            # The statuses go back to the last accepted ones; the hub must still hear them again.
            fixture["status"]["agent-a"] = "queueable"
            raising["error"] = None
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 2, 2),
                            "after an http.client error the hub was not told again")
            self.assertEqual(fixture["beats"][1][2], [("agent-a", "queueable", None)])
            # Even an exception that is not an Exception ends only that call.
            raising["error"] = Abandoned()
            fixture["status"]["agent-a"] = "live"
            time.sleep(0.3)
            raising["error"] = None
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 3, 2), "a BaseException from a call ended the ticker")
            self.assertEqual(fixture["beats"][2][2], [("agent-a", "live", None)])

    def test_a_heartbeat_call_that_cannot_start_is_that_hubs_failure(self):
        real_thread, refusing, refused = threading.Thread, threading.Event(), []
        refusing.set()

        class FlakyThread(real_thread):
            def start(self):
                if self.name == "bus-heartbeat-call" and refusing.is_set():
                    refused.append(time.monotonic())
                    raise RuntimeError("can't start new thread")
                return super().start()

        with mock.patch.object(bus.threading, "Thread", FlakyThread), \
                self.beating_worker("beat-nothread", tick=0.05, interval=30) as fixture:
            self.assertTrue(fixture["until"](lambda info: "can't start new thread" in json.dumps(info.get("heartbeat_errors")), 3),
                            "a call that could not start was not reported as that hub's failure")
            failing = fixture["worker"]()
            self.assertEqual(failing["heartbeat_errors"][0]["hub"], "https://beat.invalid")
            self.assertEqual(failing["adapters"], 0)
            refusing.clear()
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 1, 3), "a call that could not start ended the ticker")
            self.assertTrue(refused)

    def test_a_heartbeat_call_is_reported_only_once_it_has_run_for_an_interval(self):
        release, entered = threading.Event(), []

        def stall(fixture, url, agents):
            entered.append(time.monotonic())
            release.wait(6)

        with self.beating_worker("beat-threshold", tick=0.02, interval=1.0, hook=stall) as fixture:
            try:
                self.assertTrue(fixture["until"](lambda info: entered and info.get("adapters") == 1, 3), "no heartbeat call began")
                # Younger than an interval: not reported, and its hub still counted.
                while time.monotonic() < entered[0] + 0.6:
                    info = fixture["worker"]()
                    self.assertEqual(info["heartbeat_errors"], [], "a call was reported before it had run for an interval")
                    self.assertEqual(info["adapters"], 1)
                    time.sleep(0.02)
                # Past the interval: reported under its hub's name, and its hub no longer counted.
                self.assertTrue(fixture["until"](lambda info: info.get("heartbeat_errors"), 2),
                                "a call still running past the interval was not reported")
                info = fixture["worker"]()
                self.assertEqual(info["heartbeat_errors"][0]["hub"], "https://beat.invalid")
                self.assertTrue(info["heartbeat_errors"][0]["error"].startswith("heartbeat call running for "))
                self.assertEqual(info["adapters"], 0)
                self.assertGreaterEqual(time.monotonic() - entered[0], 0.95)
            finally:
                release.set()

    def test_a_failed_heartbeat_is_sent_again_even_when_the_statuses_went_back(self):
        # The hub may have applied the refused request (its answer was lost), so
        # it must hear the statuses again although they equal the last accepted.
        def lose_the_answer_once(fixture, url, agents):
            if agents == [("agent-a", "live", None)] and not fixture["refusals"]:
                fixture["status"]["agent-a"] = "queueable"
                raise bus.BusError("simulated lost answer")

        with self.beating_worker("beat-revert", tick=0.1, interval=30, hook=lose_the_answer_once) as fixture:
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 1, 3))
            fixture["status"]["agent-a"] = "live"
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 2, 2),
                            "the hub was not told again after a failed heartbeat")
            self.assertEqual(fixture["beats"][1][2], [("agent-a", "queueable", None)])

    def test_more_than_128_agents_go_in_parts_and_a_failed_part_sends_them_all_again(self):
        url = "https://beat.invalid"
        records = {str(i): {"id": "agent-%03d" % i, "kind": "codex", "binary": sys.executable, "url": url,
                            "buses": ["general"]} for i in range(130)}

        def refuse_the_changed_part_once(fixture, url, agents):
            if len(agents) == 2 and agents[-1][1] == "live" and not fixture["refusals"]:
                raise bus.BusError("simulated failure of the second request")

        with self.beating_worker("beat-parts", tick=0.05, interval=30, records=records,
                                 hook=refuse_the_changed_part_once) as fixture:
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 2, 3))
            self.assertEqual([len(beat[2]) for beat in fixture["beats"]], [128, 2])
            fixture["status"]["agent-129"] = "live"
            # The first part of the change is applied, the second refused; the
            # next call sends both parts again.
            self.assertTrue(fixture["wait_for"](lambda beats: len(beats) >= 5, 2),
                            "a change past the first 128 agents, or its refused part, waited for the interval")
            self.assertEqual([len(beat[2]) for beat in fixture["beats"][2:]], [128, 128, 2])
            self.assertEqual(fixture["beats"][4][2][-1], ("agent-129", "live", None))
            time.sleep(0.3)
            self.assertEqual(len(fixture["beats"]), 5)

    def test_a_worker_asked_to_stop_sends_no_further_part(self):
        url = "https://beat.invalid"
        records = {str(i): {"id": "agent-%03d" % i, "kind": "codex", "binary": sys.executable, "url": url,
                            "buses": ["general"]} for i in range(130)}

        def stop_during_the_first_part(fixture, url, agents):
            if len(agents) == 128:
                (fixture["state"] / "worker.stop").touch()

        with self.beating_worker("beat-stop", tick=0.05, interval=30, records=records,
                                 hook=stop_during_the_first_part) as fixture:
            self.assertTrue(fixture["until"](lambda info: info.get("state") == "stopped", 5), "the worker did not stop")
            self.assertEqual([len(beat[2]) for beat in fixture["beats"]], [128])

    def test_hubs_that_do_not_answer_hold_up_neither_the_ticker_nor_another_hub(self):
        # Twenty, more than a pool of eight or ten threads could carry.
        slow = ["https://slow-%d.invalid" % index for index in range(20)]
        quick = "https://quick.invalid"
        records = {url: {"id": "agent-" + url[len("https://"):-len(".invalid")], "kind": "codex",
                         "binary": sys.executable, "url": url, "buses": ["general"]} for url in slow + [quick]}
        release, entered = threading.Event(), []

        def stall_the_slow_hubs(fixture, url, agents):
            if url != quick:
                entered.append(url)
                if not release.wait(6):
                    raise bus.BusError("simulated stalled hub")

        quick_times = lambda beats: [beat[0] for beat in beats if beat[1] == quick]
        with self.beating_worker("beat-stall", tick=0.02, interval=0.3, records=records, urls=tuple(slow) + (quick,),
                                 hook=stall_the_slow_hubs) as fixture:
            try:
                self.assertTrue(fixture["wait_for"](lambda beats: len(quick_times(beats)) >= 4, 3),
                                "hubs that do not answer held up another hub's heartbeats")
                times = quick_times(fixture["beats"])
                self.assertLess(max(later - earlier for earlier, later in zip(times, times[1:])), 1)
                # Each stalled hub keeps its one call however many ticks pass, and
                # shows as an error once that call has gone unanswered for an interval.
                self.assertTrue(fixture["until"](lambda info: len(info.get("heartbeat_errors", [])) == 20, 3),
                                "hubs whose calls went unanswered were not reported")
                info = fixture["worker"]()
                self.assertTrue(all(error["error"].startswith("heartbeat call running for ") for error in info["heartbeat_errors"]))
                self.assertEqual(sorted(error["hub"] for error in info["heartbeat_errors"]), sorted(slow))
                self.assertEqual(info["adapters"], 1)
                self.assertEqual(sorted(entered), sorted(slow))
            finally:
                release.set()

    def test_the_worker_stops_without_waiting_for_a_heartbeat_call_that_has_not_answered(self):
        release, entered = threading.Event(), threading.Event()

        def stall(fixture, url, agents):
            entered.set()
            release.wait(6)

        with self.beating_worker("beat-stopping", tick=0.05, interval=30, hook=stall) as fixture:
            try:
                self.assertTrue(entered.wait(3), "no heartbeat call began")
                (fixture["state"] / "worker.stop").touch()
                self.assertTrue(fixture["until"](lambda info: info.get("state") == "stopped", 1.5),
                                "the worker waited for a heartbeat call that had not answered")
            finally:
                release.set()

    def test_a_worker_process_exits_after_a_stop_without_waiting_for_a_stalled_heartbeat(self):
        # Only a process of its own shows whether a call thread that has not
        # answered keeps the interpreter from exiting.
        state, entered = self.root / "beat-process", self.root / "beat-process-entered"
        state.mkdir()
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", STALLED_WORKER, lib, str(state), str(entered)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not entered.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(entered.exists(), "the worker process began no heartbeat call")
            (state / "worker.stop").touch()
            # A stalled request ends within its 12-second timeout, so a stop may
            # take that and a margin; this call would take two minutes.
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.fail("the worker process was still running 15 s after the stop")
            self.assertEqual(child.returncode, 0, child.stderr.read())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stderr.close()

    def test_a_heartbeat_is_due_at_the_last_tick_before_the_interval_after_sleep_or_a_clock_set_back(self):
        with mock.patch.object(bus, "HEARTBEAT_TICK", 2), mock.patch.object(bus, "HEARTBEAT_INTERVAL", 15):
            accepted = (1000.0, 5000.0, [])
            self.assertFalse(bus.heartbeat_due(accepted, 1012.9, 5012.9))
            # The last tick before 15 seconds, so heartbeats are about 14 seconds apart.
            self.assertTrue(bus.heartbeat_due(accepted, 1013.0, 5013.0))
            # Asleep: the monotonic clock stood still while the wall clock ran on.
            self.assertTrue(bus.heartbeat_due(accepted, 1001.0, 5600.0))
            # The wall clock was set back an hour; the monotonic clock still counts.
            self.assertTrue(bus.heartbeat_due(accepted, 1014.0, 1400.0))
            self.assertFalse(bus.heartbeat_due(accepted, 1001.0, 1400.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
