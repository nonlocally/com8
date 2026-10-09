#!/usr/bin/env python3
"""Isolated registry, admission, routing, revocation and HTTP security checks."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import http.server
import json
from pathlib import Path
import sqlite3
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bus_broker import Broker, BusHTTPServer, LEASE_TTL, LIVE_TTL, MAX_MESSAGE, MAX_PENDING, MESSAGE_TTL, handler_factory, serve


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="communicate-bus-")
        self.now = [1800000000.0]
        self.b = Broker(self.tmp.name, clock=lambda: self.now[0])
        self.admin = self.b.admin_token

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, op, token=None, **fields):
        result = self.b.handle(self.admin if token is None else token, {"op": op, **fields})
        self.assertTrue(result.get("ok"), result)
        return result

    def refused(self, op, token=None, code=None, **fields):
        result = self.b.handle(self.admin if token is None else token, {"op": op, **fields})
        self.assertFalse(result.get("ok"), result)
        if code:
            self.assertEqual(result["code"], code)
        return result

    def join(self, bus="general", device="peer"):
        invite = self.call("invite", bus=bus)
        return self.call("redeem", token="", invite=invite["invite"], device=device)

    def register(self, token=None, name="agent", bus="general", **extra):
        return self.call("register", token=token, session_key=name, name=name, bus=bus, **extra)["id"]

    def pair(self):
        a = self.join(device="alice")
        b = self.join(device="bob")
        a["agent"] = self.register(token=a["token"], name="alice")
        b["agent"] = self.register(token=b["token"], name="bob", kind="codex", status="queueable")
        return a, b

    def send(self, a, b, message="hello"):
        return self.call("send", token=a["token"], sender=a["agent"], target=b["agent"], bus="general", message=message)

    def test_registration_default_private_idempotence_and_persistence(self):
        first = self.call("register", session_key="session", name="Ada", kind="claude")
        self.assertEqual(first["buses"], ["general"])
        self.call("create", bus="photonics")
        private = self.register(name="private", bus="photonics")
        again = self.call("register", session_key="session", name="renamed", bus="photonics")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["buses"], ["general", "photonics"])
        snap = self.call("snapshot")
        general = next(b for b in snap["buses"] if b["name"] == "general")
        self.assertNotIn(private, [a["id"] for a in general["agents"]])
        reopened = Broker(self.tmp.name, clock=lambda: self.now[0])
        self.assertEqual(reopened.admin_token, self.admin)
        self.assertEqual(reopened.server_id, self.b.server_id)
        self.assertEqual(len(reopened.handle(self.admin, {"op": "snapshot"})["buses"]), 2)

    def test_invitation_expiry_single_use_and_existing_principal(self):
        invite = self.call("invite", ttl=60)["invite"]
        self.now[0] += 60
        self.refused("redeem", token="", invite=invite, code="forbidden")
        self.call("create", bus="photonics")
        peer = self.join()
        invitation = self.call("invite", bus="photonics")["invite"]
        more = self.call("redeem", token=peer["token"], invite=invitation, device="unchanged")
        self.assertEqual(more["token"], peer["token"])
        self.assertEqual(more["principal"], peer["principal"])
        self.assertEqual(more["buses"], ["general", "photonics"])
        self.refused("redeem", token="", invite=invitation, code="forbidden")
        outstanding = self.call("invite")["invite"]
        self.call("invite_revoke", invite=outstanding)
        self.refused("redeem", token="", invite=outstanding, code="forbidden")
        self.refused("invite_revoke", token=peer["token"], invite=outstanding, code="forbidden")
        self.refused("register", token=peer["token"], session_key="secret", name="secret", bus="unknown", code="forbidden")
        # A device may invite only its own account's devices (scripts/test-bus-device-invites.py).
        for op, fields in (("create", {"bus": "x"}), ("invite", {"user": "someone-else"}),
                           ("revoke", {"principal": "admin"}), ("members", {})):
            self.refused(op, token=peer["token"], code="forbidden", **fields)

    def test_snapshot_does_not_disclose_private_names_memberships_or_principals(self):
        self.call("create", bus="secret")
        self.register(name="hidden", bus="secret")
        both = self.register(name="both")
        self.register(name="both", bus="secret")
        peer = self.join()
        snap = self.call("snapshot", token=peer["token"])
        self.assertNotIn("principals", snap)
        self.assertEqual([b["name"] for b in snap["buses"]], ["general"])
        visible = next(a for a in snap["buses"][0]["agents"] if a["id"] == both)
        self.assertEqual(visible["buses"], ["general"])
        self.assertNotIn("principal", visible)
        self.refused("snapshot", token="", code="unauthorized")
        self.refused("snapshot", token="not-a-valid-token" * 3, code="unauthorized")

    def test_send_poll_ack_receipt_and_foreign_ownership(self):
        a, b = self.pair()
        m = self.send(a, b, "literal $(touch /tmp/nope)\n`uname`; <script>alert(1)</script>")
        self.assertEqual(m["status"], "accepted")
        self.refused("send", token=b["token"], sender=a["agent"], target=b["agent"], message="forgery", code="not_found")
        self.refused("poll", token=a["token"], agent=b["agent"], code="not_found")
        self.refused("heartbeat", token=a["token"], agents=[{"id": b["agent"], "status": "live"}], code="not_found")
        foreign = self.join(device="eve")
        self.refused("receipt", token=foreign["token"], id=m["id"], code="not_found")
        envelope = self.call("poll", token=b["token"], agent=b["agent"])["messages"][0]
        self.assertEqual(envelope["sender"]["id"], a["agent"])
        self.assertEqual(envelope["sender"]["device"], "alice")
        self.assertIn("$(touch", envelope["message"])
        self.assertEqual(self.call("poll", token=b["token"], agent=b["agent"])["messages"], [])
        self.refused("ack", token=b["token"], agent=b["agent"], id=m["id"], lease="wrong", status="queued", code="not_found")
        self.call("ack", token=b["token"], agent=b["agent"], id=m["id"], lease=envelope["lease"], status="queued")
        self.call("ack", token=b["token"], agent=b["agent"], id=m["id"], lease=envelope["lease"], status="queued")
        self.assertEqual(self.call("receipt", token=a["token"], id=m["id"])["status"], "queued")

    def test_bus_boundary_duplicate_names_and_membership_revocation(self):
        a, b = self.pair()
        self.call("create", bus="photonics")
        remote = self.join(bus="photonics", device="photonics-peer")
        remote["agent"] = self.register(token=remote["token"], name="bob", bus="photonics")
        self.refused("send", token=a["token"], sender=a["agent"], target=remote["agent"], bus="general", message="cross bus", code="not_found")
        self.refused("send", token=a["token"], sender=a["agent"], target=remote["agent"], bus="photonics", message="cross bus", code="not_found")
        another = self.join(device="third")
        self.register(token=another["token"], name="bob")
        self.refused("send", token=a["token"], sender=a["agent"], target="bob", message="ambiguous", code="ambiguous")
        m = self.send(a, b)
        self.call("leave", token=b["token"], agent=b["agent"], bus="general")
        self.assertEqual(self.call("poll", token=b["token"], agent=b["agent"])["messages"], [])
        self.assertEqual(self.call("receipt", token=a["token"], id=m["id"])["status"], "cancelled")
        self.refused("send", token=a["token"], sender=a["agent"], target=b["agent"], message="late", code="not_found")

    def test_principal_revocation_cancels_leased_delivery_and_revokes_every_request(self):
        a, b = self.pair()
        m = self.send(a, b)
        envelope = self.call("poll", token=b["token"], agent=b["agent"])["messages"][0]
        self.call("revoke", principal=a["principal"])
        self.refused("snapshot", token=a["token"], code="unauthorized")
        self.refused("ack", token=b["token"], agent=b["agent"], id=m["id"], lease=envelope["lease"], status="delivered", code="conflict")
        self.assertEqual(self.call("receipt", token=b["token"], id=m["id"])["status"], "cancelled")
        snap = self.call("snapshot")
        self.assertFalse(any(agent["id"] == a["agent"] for bus in snap["buses"] for agent in bus["agents"]))
        self.call("revoke", principal=b["principal"])
        self.refused("poll", token=b["token"], agent=b["agent"], code="unauthorized")

    def test_leases_expiry_stale_ack_and_measured_liveness(self):
        a, b = self.pair()
        m = self.send(a, b)
        e1 = self.call("poll", token=b["token"], agent=b["agent"])["messages"][0]
        self.now[0] += LEASE_TTL + 1
        e2 = self.call("poll", token=b["token"], agent=b["agent"])["messages"][0]
        self.assertNotEqual(e1["lease"], e2["lease"])
        self.refused("ack", token=b["token"], agent=b["agent"], id=m["id"], lease=e1["lease"], status="delivered", code="not_found")
        self.now[0] += LIVE_TTL + 1
        states = [agent["status"] for bus in self.call("snapshot")["buses"] for agent in bus["agents"]]
        self.assertTrue(states and all(s == "offline" for s in states))
        self.call("heartbeat", token=b["token"], agents=[{"id": b["agent"], "status": "queueable"}])
        fresh = next(agent for agent in self.call("snapshot")["buses"][0]["agents"] if agent["id"] == b["agent"])
        self.assertEqual(fresh["status"], "queueable")
        self.now[0] += MESSAGE_TTL
        self.assertEqual(self.call("poll", token=b["token"], agent=b["agent"])["messages"], [])
        self.assertEqual(self.call("receipt", token=a["token"], id=m["id"])["status"], "expired")

    def test_atomic_invitation_and_registration_concurrency(self):
        invitation = self.call("invite")["invite"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: self.b.handle(None, {"op": "redeem", "invite": invitation, "device": "racer"}), range(8)))
        self.assertEqual(sum(bool(o.get("ok")) for o in outcomes), 1)
        token = next(o["token"] for o in outcomes if o.get("ok"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: self.b.handle(token, {"op": "register", "session_key": "shared", "name": "racer"}), range(8)))
        self.assertTrue(all(o.get("ok") for o in outcomes), outcomes)
        self.assertEqual(len({o["id"] for o in outcomes}), 1)

    def test_limits_validation_and_secrets_not_stored_in_database(self):
        a, b = self.pair()
        self.refused("create", bus="../escape")
        self.refused("create", bus="two\nbuses")
        self.refused("send", token=a["token"], sender=a["agent"], target=b["agent"], message="x" * (MAX_MESSAGE + 1))
        self.refused("register", session_key="bad\nkey", name="bad")
        self.refused("invite", ttl=float("nan"))
        self.refused("heartbeat", agents=[{"id": a["agent"], "status": "made-up"}])
        for i in range(MAX_PENDING):
            self.send(a, b, "message %d" % i)
        self.refused("send", token=a["token"], sender=a["agent"], target=b["agent"], message="overflow", code="limit")
        db_bytes = Path(self.tmp.name, "bus.sqlite3").read_bytes()
        self.assertNotIn(a["token"].encode(), db_bytes)
        self.assertNotIn(self.admin.encode(), db_bytes)
        self.assertEqual(Path(self.tmp.name, "admin.token").stat().st_mode & 0o777, 0o600)

    def test_enrollment_and_metadata_resource_caps(self):
        with mock.patch("bus_broker.MAX_BUSES", 2):
            self.call("create", bus="last")
            self.refused("create", bus="overflow", code="limit")
            self.call("create", bus="last")
        with mock.patch("bus_broker.MAX_INVITES", 1):
            invitation = self.call("invite")["invite"]
            self.refused("invite", code="limit")
            self.call("invite_revoke", invite=invitation)
            self.call("invite")
        with mock.patch("bus_broker.MAX_PRINCIPALS", 2):
            self.join()
            invitation = self.call("invite")["invite"]
            self.refused("redeem", token="", invite=invitation, code="limit")
        with mock.patch("bus_broker.MAX_TOTAL_AGENTS", 1):
            first = self.register(name="last-agent")
            self.refused("register", session_key="overflow", name="overflow", code="limit")
            self.assertEqual(self.register(name="last-agent"), first)

    def test_http_auth_origin_limits_static_and_loopback_binding(self):
        assets = Path(self.tmp.name, "web")
        assets.mkdir()
        (assets / "bus_ui.html").write_text("<!doctype html><title>Bus</title><script>void 0;</script>")
        server = BusHTTPServer(("127.0.0.1", 0), handler_factory(self.b, assets))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host, port = server.server_address

        def request(method, path, data=None, **headers):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
                response = conn.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                conn.close()

        try:
            status, headers, body = request("GET", "/")
            self.assertEqual(status, 200)
            self.assertNotIn(self.admin.encode(), body)
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("'sha256-", headers["Content-Security-Policy"])
            self.assertNotIn("script-src 'self' 'unsafe-inline'", headers["Content-Security-Policy"])
            status, _, _ = request("POST", "/v1", {"op": "snapshot"}, **{"Content-Type": "application/json"})
            self.assertEqual(status, 401)
            auth = {"Content-Type": "application/json", "Authorization": "Bearer " + self.admin}
            status, _, body = request("POST", "/v1", {"op": "snapshot"}, **auth)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_admin"])
            status, _, _ = request("POST", "/v1", {"op": "snapshot"}, Origin="https://evil.invalid", **auth)
            self.assertEqual(status, 403)
            status, _, _ = request("POST", "/v1", {"op": "snapshot"}, Origin="https://[invalid", **auth)
            self.assertEqual(status, 403)
            status, _, _ = request("POST", "/v1", {"op": "snapshot"}, Origin="http://%s:%d" % (host, port), **auth)
            self.assertEqual(status, 200)
            status, _, _ = request("GET", "/assets/../../admin.token")
            self.assertEqual(status, 404)
            status, _, _ = request("POST", "/v1", {"op": "snapshot"}, **{"Content-Type": "text/plain"})
            self.assertEqual(status, 400)
            with self.assertRaises(ValueError):
                serve(self.b, "0.0.0.0")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_http_connections_are_bounded_before_reading_headers(self):
        ready = threading.Event()
        base = handler_factory(self.b)

        class ReadyHandler(base):
            def setup(self):
                super().setup()
                ready.set()

        server = BusHTTPServer(("127.0.0.1", 0), ReadyHandler, max_connections=1)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        pending = socket.create_connection(server.server_address, timeout=5)
        try:
            pending.sendall(b"GET /health HTTP/1.1\r\n")
            self.assertTrue(ready.wait(timeout=5))
            conn = http.client.HTTPConnection(*server.server_address, timeout=5)
            conn.request("GET", "/health")
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            response.read()
            conn.close()
            pending.sendall(b"Host: localhost\r\n\r\n")
            self.assertIn(b"200 OK", pending.recv(1024))
        finally:
            pending.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
