#!/usr/bin/env python3
"""Isolated service queue, broker correlation, crash recovery and authority tests.

Use --runtime /path/to/extracted/com8-VERSION to qualify shipped modules and CLI.
Fixture helpers still come from this checkout; runtime selection never falls back.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import http.server
import ssl
import threading
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
options = argparse.ArgumentParser(add_help=False)
options.add_argument("--runtime", type=Path, help="checksum-verified extracted release")
selection, test_args = options.parse_known_args(sys.argv[1:] if __name__ == "__main__" else [])
RUNTIME = selection.runtime.resolve() if selection.runtime else None
PAYLOAD = RUNTIME / "vendor" if RUNTIME else ROOT
LIB = PAYLOAD / "lib"
sys.dont_write_bytecode = True
if RUNTIME:
    spec = importlib.util.spec_from_file_location("artifact_checks", ROOT / "scripts/qualify-installed.py")
    artifact_checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifact_checks)
    artifact_checks.artifact(RUNTIME)
for name in ("bus.py", "bus_service.py", "bus_broker.py"):
    if not (LIB / name).is_file():
        raise RuntimeError("selected runtime component missing: " + str(LIB / name))
sys.path.insert(0, str(LIB))
import bus
import bus_service as service
import bus_broker
from bus_broker import Broker


def check_runtime_sources():
    for module in (bus, service, bus_broker):
        expected = LIB / (module.__name__ + ".py")
        if Path(module.__file__).resolve() != expected.resolve():
            raise RuntimeError("module escaped selected runtime: " + module.__name__)
    cli = PAYLOAD / "bin/communicate"
    if not cli.is_file() or not os.access(cli, os.X_OK) or cli.resolve().parent != (PAYLOAD / "bin").resolve():
        raise RuntimeError("CLI escaped or is absent from selected runtime: " + str(cli))


check_runtime_sources()


def fixture_module(name):
    """Reuse source fixture setup while keeping imports and CLI in the selected payload."""
    path_before = sys.path[:]
    try:
        spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        # Helpers prepend their checkout lib; do not leave that fallback active.
        sys.path[:] = path_before
    check_runtime_sources()
    module.ROOT = PAYLOAD
    return module


class Services(unittest.TestCase):
    def setUp(self):
        # The TLS fixture serves clients directly; only the chat helper enables
        # gateway authentication, using its own disposable configuration.
        isolated_env = {key: value for key, value in os.environ.items() if not key.startswith("BUS_")}
        isolated_env.update(BUS_ACCOUNT_LABELS="{}", BUS_READER_USERS="{}", BUS_ADMIN_READERS="",
                            BUS_CHAT_READERS="{}", BUS_OPENWEBUI_READERS="0", BUS_CHAT_OPENWEBUI_TARGETS="[]",
                            PYTHONDONTWRITEBYTECODE="1")
        self.env_patch = mock.patch.dict(os.environ, isolated_env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="bus-service-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = Broker(self.root / "broker")
        self.sid = str(uuid4())
        self.token_file = self.root / "bridge.token"
        self.token_file.write_text("isolated-service-token-" + "x" * 32)
        self.token_file.chmod(0o600)
        self.record = service.registration(argparse.Namespace(service_id=self.sid, name="fixture-service",
            endpoint="https://platform.invalid/private/bridge", token_file=str(self.token_file)))
        self.target = self.call("register", session_key=self.record["session_key"], name="service", kind="service")["id"]
        self.record.update(id=self.target, url="https://broker.invalid", buses=["general"])
        self.sender = self.call("register", session_key="sender", name="sender", kind="codex")["id"]
        self.registrations = {"service": self.record}
        self.connections = {self.record["url"]: {"url": self.record["url"]}}
        self.envelope = self.send()

    def call(self, op, **kw):
        result = self.broker.handle(self.broker.admin_token, {"op": op, **kw})
        if not result["ok"]:
            raise bus.BusError(result["error"], result.get("code"))
        return result

    def send(self, message="build a straight"):
        sent = self.call("send", sender=self.sender, target=self.target, bus="general", message=message)
        envelope = self.call("poll", agent=self.target)["messages"][0]
        self.assertEqual(sent["id"], envelope["id"])
        return envelope

    def request(self, conn, op, **kw):
        return self.call(op, **kw)

    def row(self):
        db = service.journal(self.root)
        try:
            return dict(db.execute("SELECT * FROM jobs").fetchone())
        finally:
            db.close()

    def test_stable_service_identity_and_private_configuration(self):
        again = self.call("register", session_key=self.record["session_key"], name="renamed", kind="service")
        self.assertEqual(again["id"], self.target)
        self.assertEqual(bus.current_status(self.record), "queueable")
        self.token_file.chmod(0o644)
        self.assertEqual(bus.current_status(self.record), "offline")
        self.token_file.chmod(0o600)
        link = self.root / "token-link"
        link.symlink_to(self.token_file)
        with self.assertRaises(OSError):
            service.credential(str(link))
        for endpoint in ("http://platform.invalid/bridge", "https://u:p@platform.invalid/bridge", "https://platform.invalid/bridge?secret=x"):
            with self.assertRaises(ValueError):
                service.endpoint(endpoint)

    def test_durable_queue_dedup_and_broker_identity_not_message_fields(self):
        self.envelope["message"] = json.dumps({"user": "admin", "device_id": "forged", "endpoint": "https://evil.invalid"})
        outcome = service.enqueue(self.root, self.record, self.envelope)
        self.assertEqual(outcome[0], "queued")
        retried = dict(self.envelope, lease="new delivery lease")
        self.assertEqual(service.enqueue(self.root, self.record, retried), outcome)
        saved = json.loads(self.row()["envelope"])
        self.assertNotIn("lease", saved)
        self.assertEqual(saved["sender"]["user"], self.envelope["sender"]["user"])
        with self.assertRaises(ValueError):
            service.enqueue(self.root, self.record, dict(retried, message="changed"))
        with self.assertRaises(ValueError):
            service.enqueue(self.root, self.record, dict(retried, target="a_wrong"))

    def test_pending_poll_final_reply_and_lost_reply_response_survive_restart(self):
        service.enqueue(self.root, self.record, self.envelope)
        now = time.time()
        operations = []
        def pending(record, job):
            operations.append(job["operation"])
            return {"pending": True}
        service.advance(self.root, self.registrations, self.connections, self.request, call_bridge=pending, now=now)
        self.assertEqual(self.row()["operation"], "poll")
        reply_ids = []
        def lose_response(conn, op, **kw):
            result = self.request(conn, op, **kw)
            reply_ids.append(result["id"])
            raise OSError("lost response after committed reply")
        def final(record, job):
            operations.append(job["operation"])
            return {"pending": False, "reply": '{"status":"succeeded","artifacts":[]}'}
        service.advance(self.root, self.registrations, self.connections, lose_response, call_bridge=final, now=now+6)
        self.assertEqual(self.row()["state"], "pending")
        self.assertIsNotNone(self.row()["reply"])
        with mock.patch.object(service, "bridge", side_effect=AssertionError("do not resubmit")):
            service.advance(self.root, self.registrations, self.connections, self.request,
                            call_bridge=lambda *_: self.fail("must reuse persisted answer"), now=now+12)
        self.assertEqual(operations, ["deliver", "poll"])
        self.assertEqual(self.row()["state"], "sent")
        self.assertEqual(json.loads(self.row()["receipt"])["id"], reply_ids[0])
        with self.broker._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE sender=?", (self.target,)).fetchone()[0], 1)

    def test_concurrent_reply_dedup_and_content_conflict(self):
        def reply(_):
            return self.call("reply", sender=self.target, id=self.envelope["id"], message="answer", request_id="stable-key")
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(reply, range(16)))
        self.assertEqual(len({row["id"] for row in rows}), 1)
        with self.assertRaises(bus.BusError) as raised:
            self.call("reply", sender=self.target, id=self.envelope["id"], message="different", request_id="stable-key")
        self.assertEqual(raised.exception.code, "conflict")
        with self.assertRaises(bus.BusError):
            self.call("reply", sender=self.sender, id=self.envelope["id"], message="answer", request_id="stable-key")

    def test_expiry_and_endpoint_errors_keep_secrets_out(self):
        service.enqueue(self.root, self.record, self.envelope)
        now = time.time()
        def unavailable(*_):
            raise ValueError("secret-token-do-not-log")
        errors = service.advance(self.root, self.registrations, self.connections, self.request, call_bridge=unavailable, now=now)
        self.assertNotIn("secret-token", json.dumps(errors))
        self.assertEqual(self.row()["operation"], "deliver")
        service.advance(self.root, self.registrations, self.connections, self.request,
                        call_bridge=lambda *_: self.fail("expired job must not execute"), now=now+86401)
        self.assertEqual(self.row()["state"], "expired")

    def test_transport_queue_bounded_before_execution(self):
        with mock.patch.object(service, "MAX_PENDING", 1):
            service.enqueue(self.root, self.record, self.envelope)
            with self.assertRaisesRegex(ValueError, "queue is full"):
                service.enqueue(self.root, self.record, dict(self.envelope, id="m_other", reply_to="m_other"))

    def test_real_bridge_payload_auth_and_no_redirect_or_unbounded_response(self):
        service.enqueue(self.root, self.record, self.envelope)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"pending":true}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(service.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(service.bridge(self.record, self.row()), {"pending": True})
            req = opener.open.call_args.args[0]
            self.assertEqual(req.full_url, self.record["endpoint"])
            self.assertTrue(req.headers["Authorization"].startswith("Bearer "))
            payload = json.loads(req.data)
            self.assertEqual(payload["envelope"]["sender"]["user"], self.envelope["sender"]["user"])
            self.assertEqual(payload["service_id"], self.sid)
            self.assertNotIn("token", payload)
            self.assertEqual(response.read.call_args.args, (65537,))
            response.read.return_value = b'x'*65537
            with self.assertRaisesRegex(ValueError, "exceeds limit"):
                service.bridge(self.record, self.row())
        self.assertIsNone(service.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_rehome_refuses_pending_service_jobs_without_mutation(self):
        module = fixture_module("test-bus-rehome")
        fixture = module.RehomeTests()
        fixture.setUp()
        try:
            service.enqueue(fixture.root, dict(self.record, url=module.OLD), self.envelope)
            with self.assertRaisesRegex(bus.BusError, "service replies are pending"):
                fixture.run_rehome()
            self.assertEqual(bus.config(), fixture.cfg)
            self.assertEqual(bus.registrations(), fixture.regs)
        finally:
            fixture.tearDown()

    def test_cli_worker_roundtrip_to_real_authenticated_tls_endpoint(self):
        self.cli_worker_roundtrip(version=1)

    def test_v2_cli_worker_correlated_reply_is_acknowledged_without_a_reply_loop(self):
        self.cli_worker_roundtrip(version=2)

    def cli_worker_roundtrip(self, version):
        module = fixture_module("test-bus-client")
        fixture_cls = module.BusClientTest
        fixture_cls.setUpClass()
        fixture = fixture_cls()
        received = []
        token = "fixture-bridge-token-" + "x" * 40
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer " + token:
                    self.send_error(401)
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(payload)
                if version == 2 and payload["envelope"]["in_reply_to"] is not None:
                    result = {"pending": False, "disposition": "acknowledge"}
                else:
                    result = {"pending": True} if payload["op"] == "deliver" else {
                        "pending": False, "reply": "Qualified fixture service result"}
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(fixture.temp / "cert.pem", fixture.temp / "key.pem")
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = fixture.env("service-worker")
            fixture.cli(env, "connect", fixture.code(user="service-account"))
            token_file = fixture.temp / "service.token"
            token_file.write_text(token)
            token_file.chmod(0o600)
            registered = fixture.cli(env, "register-service", "qualified-fixture", "--service-id", self.sid,
                "--endpoint", "https://localhost:%d/bridge" % server.server_port,
                "--token-file", str(token_file), "--contract-version", str(version), "--json")
            self.assertEqual((module.ROOT / "bin/communicate").resolve(), (PAYLOAD / "bin/communicate").resolve())
            state = Path(env["COMM_STATE"]) / "bus"
            running = json.loads((state / "worker.json").read_text())
            self.assertEqual(running["runtime"], bus.WORKER_RUNTIME)
            command = subprocess.run(["ps", "-ww", "-p", str(running["pid"]), "-o", "command="],
                                     capture_output=True, text=True, check=True, timeout=5).stdout
            self.assertIn(str((LIB / "bus.py").resolve()) + " __worker", command,
                          "worker did not execute the selected payload")
            def call(op, **fields):
                result = fixture.broker.handle(fixture.admin, dict(fields, op=op))
                self.assertTrue(result["ok"], result)
                return result
            sender = call("register", session_key="fixture-requester", name="fixture-requester", kind="codex")["id"]
            sent = call("send", sender=sender, target=registered["id"], message="test prompt", bus="general")
            deadline = time.monotonic() + 25
            replies = []
            while time.monotonic() < deadline:
                replies = call("poll", agent=sender)["messages"]
                if replies:
                    break
                time.sleep(.2)
            diagnostics = {name: (state / name).read_text()[-3000:] for name in ("worker.json", "worker.log") if (state / name).exists()}
            self.assertEqual(len(replies), 1, "worker did not return a correlated answer: " + repr((received, diagnostics, call("receipt", id=sent["id"]))))
            self.assertEqual(replies[0]["sender"]["id"], registered["id"])
            self.assertEqual(replies[0]["message"], "Qualified fixture service result")
            self.assertEqual([item["op"] for item in received], ["deliver", "poll"])
            self.assertEqual(received[0]["envelope"]["id"], sent["id"])
            self.assertEqual(received[0]["envelope"]["sender"]["id"], sender)
            self.assertEqual(received[0]["service_id"], self.sid)
            self.assertEqual(received[0]["recipient_id"], registered["id"])
            self.assertEqual(received[0]["hub"], fixture.url)
            self.assertEqual(received[0]["version"], version)
            if version == 1:
                self.assertNotIn("in_reply_to", received[0]["envelope"])
            else:
                self.assertEqual(received[0]["envelope"]["conversation_id"], sent["conversation_id"])
                self.assertIsNone(received[0]["envelope"]["in_reply_to"])
                self.assertEqual(replies[0]["in_reply_to"], sent["id"])
                returned = call("reply", sender=sender, id=replies[0]["id"], message="received result")
                self.assertEqual(returned["conversation_id"], sent["conversation_id"])
                deadline = time.monotonic() + 20
                terminal = None
                while time.monotonic() < deadline:
                    with service.journal(state) as db:
                        terminal = db.execute("SELECT state FROM jobs WHERE id=?", (returned["id"],)).fetchone()
                    if terminal and terminal[0] == "acknowledged":
                        break
                    time.sleep(.2)
                self.assertIsNotNone(terminal)
                self.assertEqual(terminal[0], "acknowledged")
                self.assertEqual([item["op"] for item in received], ["deliver", "poll", "deliver"])
                self.assertEqual(received[-1]["envelope"]["in_reply_to"], replies[0]["id"])
                with fixture.broker._connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE conversation=?",
                                                (sent["conversation_id"],)).fetchone()[0], 3)
        finally:
            fixture_cls.tearDownClass()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_human_reply_dedup(self):
        module = fixture_module("test-bus-chat")
        fixture = module.ChatTests()
        fixture.setUp()
        try:
            fixture.send()
            message = fixture.poll()[0]
            one = fixture.admin("reply", sender=fixture.agent, id=message["id"], message="service answer", request_id="stable-key")
            two = fixture.admin("reply", sender=fixture.agent, id=message["id"], message="service answer", request_id="stable-key")
            self.assertEqual(one["id"], two["id"])
            self.assertEqual(one["correlation_version"], 1)
            self.assertEqual(one["in_reply_to"], message["id"])
            self.assertEqual(one["conversation_id"], message["conversation_id"])
            self.assertTrue(message["conversation_id"].startswith("hc_"))
            self.assertIsNone(message["in_reply_to"])
            self.assertEqual(service.normalized(message, 2)["conversation_id"], message["chat"])
        finally:
            fixture.tearDown()

    def test_broker_correlation_is_not_caller_controlled(self):
        receipt = self.call("receipt", id=self.envelope["id"])
        self.assertEqual(receipt["correlation_version"], 1)
        self.assertEqual(receipt["conversation_id"], self.envelope["conversation_id"])
        self.assertIsNone(receipt["in_reply_to"])
        for field, value in (("conversation_id", "c_" + "a" * 32), ("in_reply_to", self.envelope["id"]),
                             ("correlation_version", 0)):
            with self.subTest(field=field):
                with self.assertRaises(bus.BusError):
                    self.call("send", sender=self.sender, target=self.target, message="forged", **{field: value})
                with self.assertRaises(bus.BusError):
                    self.call("reply", sender=self.target, id=self.envelope["id"], message="forged", **{field: value})
        result = self.call("reply", sender=self.target, id=self.envelope["id"], message="answer")
        received = self.call("poll", agent=self.sender)["messages"][0]
        for row in (result, received, self.call("receipt", id=result["id"])):
            self.assertEqual(row["conversation_id"], receipt["conversation_id"])
            self.assertEqual(row["in_reply_to"], self.envelope["id"])
            self.assertEqual(row["correlation_version"], 1)

    def test_v2_refuses_missing_legacy_or_invalid_correlation(self):
        self.record["contract_version"] = 2
        cases = [dict(self.envelope, correlation_version=value) for value in (0, 2, True, None)]
        cases += [dict(self.envelope, conversation_id=value) for value in (None, "c_unknown", "" )]
        cases += [dict(self.envelope, in_reply_to=value) for value in (False, "forged", "")]
        cases += [{k: v for k, v in self.envelope.items() if k != field}
                  for field in ("correlation_version", "conversation_id", "in_reply_to")]
        for envelope in cases:
            with self.subTest(envelope=envelope):
                with self.assertRaises(service.ServiceError):
                    service.enqueue(self.root, self.record, envelope)
        service.enqueue(self.root, self.record, self.envelope)
        self.assertEqual(self.row()["contract_version"], 2)
        with self.assertRaises(service.ServiceError):
            service.enqueue(self.root, self.record, dict(self.envelope, in_reply_to="m_" + "b" * 32))

    def test_existing_job_version_is_pinned_across_registration_and_journal_upgrade(self):
        service.enqueue(self.root, self.record, self.envelope)
        before = self.row()
        with sqlite3.connect(self.root / "service-jobs.sqlite") as db:
            db.execute("ALTER TABLE jobs DROP COLUMN contract_version")
        self.record["contract_version"] = 2
        service.enqueue(self.root, self.record, self.envelope)
        after = self.row()
        self.assertEqual(after["contract_version"], 1)
        self.assertEqual(after["envelope"], before["envelope"])
        self.assertEqual(after["request_hash"], before["request_hash"])
        self.assertNotIn("in_reply_to", json.loads(after["envelope"]))

    def test_legacy_broker_rows_remain_unknown_after_migration(self):
        with self.broker._connect() as db:
            db.execute("ALTER TABLE messages DROP COLUMN in_reply_to")
            db.execute("ALTER TABLE messages DROP COLUMN correlation_version")
            db.execute("UPDATE messages SET lease_until=0")
        self.broker = Broker(self.root / "broker")
        received = self.call("poll", agent=self.target)["messages"][0]
        self.assertEqual(received["correlation_version"], 0)
        with self.assertRaises(service.ServiceError):
            service.normalized(received, 2)
        self.assertEqual(service.normalized(received), service.normalized(self.envelope))

    def test_acknowledgment_is_terminal_across_restart_and_only_v2(self):
        acknowledgment = {"pending": False, "disposition": "acknowledge"}
        with self.assertRaises(service.ServiceError):
            service.validate_response(acknowledgment, 1)
        for invalid in (dict(acknowledgment, reply="also reply"), dict(acknowledgment, pending=True),
                        dict(acknowledgment, disposition="skip")):
            with self.assertRaises(service.ServiceError):
                service.validate_response(invalid, 2)
        self.record["contract_version"] = 2
        service.enqueue(self.root, self.record, self.envelope)
        fail = lambda *_args, **_kw: self.fail("terminal acknowledgment must not send or re-execute")
        service.advance(self.root, self.registrations, self.connections, fail,
                        call_bridge=lambda *_: acknowledgment)
        self.assertEqual(self.row()["state"], "acknowledged")
        service.enqueue(self.root, self.record, self.envelope)
        service.advance(self.root, self.registrations, self.connections, fail, call_bridge=fail, now=time.time()+6)
        self.assertIsNone(self.row()["reply"])
        self.assertIsNone(self.row()["receipt"])


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0], *test_args], exit=False)
    if RUNTIME:
        artifact_checks.artifact(RUNTIME)
    raise SystemExit(not result.result.wasSuccessful())
