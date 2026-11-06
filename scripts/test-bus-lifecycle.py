#!/usr/bin/env python3
"""A live socket is insufficient when its original Claude session disappears."""
import json
import base64
import io
import contextlib
import os
from pathlib import Path
import socket
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import bus
import bus_service


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
        # The lost acknowledgment is kept with its time, so it stays visible after
        # later quiet iterations replace `errors`.
        published = json.loads((state / "worker.json").read_text())
        self.assertEqual(published["errors"], published["delivery_errors"])
        self.assertEqual([item["error"]["agent"] for item in published["recent_errors"]], ["agent-0"])
        self.assertLessEqual(published["recent_errors"][0]["at"], published["updated_at"])
        self.assertIn("simulated lost ACK", published["recent_errors"][0]["error"]["error"])

    def service_records(self, state, url):
        token = state / "service.token"
        token.write_text("fixture-service-token-" + "t" * 40)
        token.chmod(0o600)
        return {"svc": {"id": "agent-service", "kind": "service", "url": url, "buses": ["general"],
                        "session_key": "service:fixture", "service_id": "00000000-0000-4000-8000-000000000000",
                        "endpoint": "https://service.invalid/bridge", "token_file": str(token), "contract_version": 2},
                "native": {"id": "agent-native", "kind": "codex", "binary": sys.executable, "url": url, "buses": ["general"]}}

    def run_worker(self, state, cfg, records, request, deliver, timeout=12):
        errors = []
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
            thread.join(timeout)
            (state / "worker.stop").touch()
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_service_backlog_is_leased_in_batches_and_polled_without_the_two_second_wait(self):
        state = self.root / "batched-service-worker"
        state.mkdir()
        url = "https://service-batch.invalid"
        records = self.service_records(state, url)
        cfg = {"connections": {url: {"url": url}}}
        polls, acks, lock = [], [], threading.Lock()

        def request(conn, operation, **payload):
            if operation == "heartbeat":
                return {"ok": True}
            if operation == "poll_device":
                with lock:
                    polls.append({"at": time.monotonic(), "agents": payload["agents"], "batch": payload.get("batch")})
                    count = len(polls)
                size = {1: 32, 2: 5}.get(count, 0)
                if count == 4:
                    (state / "worker.stop").touch()
                return {"messages": [{"id": "message-%d-%d" % (count, i), "target": "agent-service",
                                      "lease": "lease-%d-%d" % (count, i)} for i in range(size)]}
            if operation == "ack":
                time.sleep(.05)
                with lock:
                    acks.append(payload["id"])
                return {"ok": True}
            raise AssertionError(operation)

        self.run_worker(state, cfg, records, request, lambda record, envelope: ("queued", "fixture queued"))
        self.assertEqual(len(polls), 4)
        self.assertEqual(sorted(polls[0]["agents"]), ["agent-native", "agent-service"])
        # Service recipients ask for batch leases; native sessions keep one message at a time.
        self.assertEqual(polls[0]["batch"], {"agent-service": bus.SERVICE_LEASE_BATCH})
        self.assertEqual(len(acks), 37)
        # Thirty-two acknowledgments took well under their sequential 1.6 s, and a poll that leased
        # service messages is followed by the next poll at once; an empty poll waits two seconds.
        self.assertLess(polls[1]["at"] - polls[0]["at"], 1.0)
        self.assertLess(polls[2]["at"] - polls[1]["at"], 1.0)
        self.assertGreaterEqual(polls[3]["at"] - polls[2]["at"], 1.9)
        with sqlite3.connect(state / "receipts.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM delivered WHERE status='queued'").fetchone()[0], 37)

    def test_full_service_journal_leaves_messages_on_the_hub(self):
        state = self.root / "full-journal-worker"
        state.mkdir()
        url = "https://service-full.invalid"
        records = self.service_records(state, url)
        cfg = {"connections": {url: {"url": url}}}
        polls = []
        capacities = [0, 5, 400]

        def request(conn, operation, **payload):
            if operation == "heartbeat":
                return {"ok": True}
            if operation == "poll_device":
                polls.append({"agents": payload["agents"], "batch": payload.get("batch")})
                if len(polls) == 3:
                    (state / "worker.stop").touch()
                return {"messages": []}
            raise AssertionError(operation)

        with mock.patch.object(bus_service, "capacity", side_effect=lambda root: capacities[min(len(polls), 2)]):
            self.run_worker(state, cfg, records, request, lambda record, envelope: self.fail("nothing to deliver"))
        self.assertEqual(len(polls), 3)
        # No journal capacity: the service is not polled, so the hub keeps its messages (24 h) instead of
        # the worker failing them; a small capacity bounds the batch; a large one is capped at the batch size.
        self.assertEqual((polls[0]["agents"], polls[0]["batch"]), (["agent-native"], None))
        self.assertEqual(polls[1]["batch"], {"agent-service": 5})
        self.assertEqual(polls[2]["batch"], {"agent-service": bus.SERVICE_LEASE_BATCH})

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

        with mock.patch.object(bus, "state_dir", return_value=state), \
                mock.patch.object(bus, "config", return_value=cfg), \
                mock.patch.object(bus, "registrations", return_value=records), \
                mock.patch.object(bus, "request", side_effect=fake_request), \
                mock.patch.object(bus, "deliver", side_effect=fake_deliver), \
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
