#!/usr/bin/env python3
"""Isolated registry, admission, routing, revocation and HTTP security checks."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import http.server
import importlib.util
import json
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bus_broker import Broker, BusHTTPServer, LEASE_TTL, LIVE_TTL, MAX_EXPORT_PAGE, MAX_MESSAGE, MAX_PENDING, MAX_PUBLISHED_AGENTS, MESSAGE_TTL, RECEIPT_RETENTION, handler_factory, serve


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
        for op, fields in (("create", {"bus": "x"}), ("invite", {}), ("revoke", {"principal": "admin"}), ("members", {})):
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

    def service_pair(self):
        """A sending device and a worker device owning a native agent and a service."""
        a = self.join(device="alice")
        a["agent"] = self.register(token=a["token"], name="alice")
        w = self.join(device="worker")
        w["codex"] = self.register(token=w["token"], name="native", kind="codex", status="queueable")
        w["service"] = self.register(token=w["token"], name="service:" + "1" * 32, kind="service", status="queueable")
        return a, w

    def test_batch_leases_for_services_keep_order_and_lease_each_message_at_most_once(self):
        a, w = self.service_pair()
        native = [self.call("send", token=a["token"], sender=a["agent"], target=w["codex"], bus="general",
                            message="n%d" % i)["id"] for i in range(3)]
        queued = [self.call("send", token=a["token"], sender=a["agent"], target=w["service"], bus="general",
                            message="s%d" % i)["id"] for i in range(40)]
        # Without a batch every target, the service included, is leased one message at a time.
        plain = self.call("poll_device", token=w["token"], agents=[w["codex"], w["service"]])["messages"]
        self.assertEqual([m["id"] for m in plain], [native[0], queued[0]])
        self.call("ack", token=w["token"], agent=w["service"], id=queued[0], lease=plain[1]["lease"], status="queued")
        first = self.call("poll_device", token=w["token"], agents=[w["codex"], w["service"]], batch={w["service"]: 32})
        self.assertEqual(first["lease_seconds"], LEASE_TTL)
        # The native target still holds its lease; the service gets its next 32 in queue order, each with its own lease.
        self.assertEqual([m["id"] for m in first["messages"]], queued[1:33])
        self.assertEqual(len({m["lease"] for m in first["messages"]}), 32)
        self.assertTrue(all(m["target"] == w["service"] for m in first["messages"]))
        # A message is leased once per lease: nothing is handed out again while 32 leases are held.
        self.assertEqual(self.call("poll_device", token=w["token"], agents=[w["codex"], w["service"]],
                                   batch={w["service"]: 32})["messages"], [])
        for m in first["messages"][:8]:
            self.call("ack", token=w["token"], agent=w["service"], id=m["id"], lease=m["lease"], status="queued")
        rest = self.call("poll_device", token=w["token"], agents=[w["service"]], batch={w["service"]: 32})["messages"]
        self.assertEqual([m["id"] for m in rest], queued[33:40])
        self.assertEqual(self.call("receipt", token=a["token"], id=queued[9])["status"], "leased")
        # Expired leases are re-leased with new tokens; the old token can no longer acknowledge.
        self.now[0] += LEASE_TTL + 1
        renewed = self.call("poll_device", token=w["token"], agents=[w["service"]], batch={w["service"]: 32})["messages"]
        self.assertEqual([m["id"] for m in renewed], queued[9:40])
        stale = first["messages"][8]
        self.assertEqual(stale["id"], queued[9])
        self.refused("ack", token=w["token"], agent=w["service"], id=stale["id"], lease=stale["lease"], status="queued", code="not_found")
        self.call("ack", token=w["token"], agent=w["service"], id=renewed[0]["id"], lease=renewed[0]["lease"], status="queued")
        self.assertEqual(self.call("receipt", token=a["token"], id=queued[9])["status"], "queued")
        # A smaller batch and the per-poll limit both bound a poll; the native target never exceeds one.
        native_again = self.call("poll_device", token=w["token"], agents=[w["codex"]], batch={w["service"]: 4} if False else {})["messages"]
        self.assertEqual([m["id"] for m in native_again], [native[0]])
        self.call("ack", token=w["token"], agent=w["codex"], id=native[0], lease=native_again[0]["lease"], status="queued")
        self.now[0] += LEASE_TTL + 1
        small = self.call("poll_device", token=w["token"], agents=[w["codex"], w["service"]], batch={w["service"]: 4})["messages"]
        self.assertEqual([m["id"] for m in small], [native[1]] + queued[10:14])

    def test_batch_lease_selectors_validate_before_any_lease(self):
        a, w = self.service_pair()
        self.call("send", token=a["token"], sender=a["agent"], target=w["service"], bus="general", message="s")
        self.call("send", token=a["token"], sender=a["agent"], target=w["codex"], bus="general", message="n")
        for batch in ({w["codex"]: 2}, {w["service"]: 0}, {w["service"]: 33}, {w["service"]: "8"},
                      {w["service"]: True}, {a["agent"]: 4}, [w["service"]], {w["service"]: 2, w["codex"]: 1}):
            self.refused("poll_device", token=w["token"], agents=[w["codex"], w["service"]], batch=batch)
        self.refused("poll_device", token=w["token"], agents=[w["codex"]], batch={w["service"]: 4})
        with self.b._connect() as db:
            self.assertEqual({row["status"] for row in db.execute("SELECT status FROM messages")}, {"accepted"})
        leased = self.call("poll_device", token=w["token"], agents=[w["codex"], w["service"]], batch={w["service"]: 1})["messages"]
        self.assertEqual(len(leased), 2)

    def test_export_pages_an_agents_receipts_and_outlives_hub_retention(self):
        a, w = self.service_pair()
        sent = [self.call("send", token=a["token"], sender=a["agent"], target=w["service"], bus="general",
                          message="secret body %d" % i)["id"] for i in range(3)]
        other = self.send(a, {"agent": w["codex"]})["id"]
        leased = self.call("poll_device", token=w["token"], agents=[w["service"]], batch={w["service"]: 32})["messages"]
        acked_at = {}
        for m in leased:
            self.now[0] += 1
            self.call("ack", token=w["token"], agent=w["service"], id=m["id"], lease=m["lease"], status="queued")
            acked_at[m["id"]] = self.now[0]
        self.now[0] += 1
        reply = self.call("reply", token=w["token"], sender=w["service"], id=sent[0], message="secret answer")
        # Everything the service sent or received, oldest update first, in bounded pages.
        first = self.call("export", token=w["token"], agent=w["service"], limit=2)
        self.assertEqual([r["id"] for r in first["receipts"]], sent[:2])
        self.assertEqual(first["retention_seconds"], RECEIPT_RETENTION)
        second = self.call("export", token=w["token"], agent=w["service"], limit=2, after=first["next"])
        self.assertEqual([r["id"] for r in second["receipts"]], [sent[2], reply["id"]])
        self.assertIsNone(second["next"])
        row = first["receipts"][0]
        self.assertEqual({key: row[key] for key in ("sender", "target", "bus", "status", "kind", "correlation_version")},
                         {"sender": a["agent"], "target": w["service"], "bus": "general", "status": "queued", "kind": "agent",
                          "correlation_version": 1})
        self.assertEqual((row["updated_at"], row["created_at"]), (acked_at[sent[0]], 1800000000.0))
        self.assertEqual(row["expires_at"], 1800000000.0 + MESSAGE_TTL)
        answer = second["receipts"][1]
        self.assertEqual((answer["sender"], answer["target"], answer["status"], answer["in_reply_to"], answer["conversation_id"]),
                         (w["service"], a["agent"], "accepted", sent[0], row["conversation_id"]))
        # Receipts, never bodies or leases; the other agent's traffic is not this agent's.
        self.assertNotIn("secret", json.dumps(first) + json.dumps(second))
        self.assertFalse(any("lease" in r for r in first["receipts"] + second["receipts"]))
        self.assertNotIn(other, [r["id"] for r in first["receipts"] + second["receipts"]])
        self.assertEqual([r["id"] for r in self.call("export", token=w["token"], agent=w["codex"])["receipts"]], [other])
        # `since` selects by last update; the owner's device or the administrator may export, nobody else.
        recent = self.call("export", token=w["token"], agent=w["service"], since=acked_at[sent[2]])
        self.assertEqual([r["id"] for r in recent["receipts"]], [sent[2], reply["id"]])
        self.assertEqual(len(self.call("export", agent=w["service"])["receipts"]), 4)
        self.refused("export", token=a["token"], agent=w["service"], code="not_found")
        for fields in ({"limit": 0}, {"limit": MAX_EXPORT_PAGE + 1}, {"limit": "2"}, {"since": -1}, {"since": "0"},
                       {"after": "cursor"}, {"after": {"updated_at": "1", "id": "x"}}, {"after": {"id": "x"}}):
            self.refused("export", token=w["token"], agent=w["service"], **fields)
        # After the hub's retention the acknowledged receipts are gone from it; the reply the requester
        # never fetched has just expired, so its receipt is fresh for one more retention period.
        self.now[0] += RECEIPT_RETENTION + 1
        self.refused("receipt", token=a["token"], id=sent[0], code="not_found")
        remaining = self.call("export", token=w["token"], agent=w["service"])["receipts"]
        self.assertEqual([(r["id"], r["status"], r["updated_at"]) for r in remaining], [(reply["id"], "expired", self.now[0])])
        self.now[0] += RECEIPT_RETENTION + 1
        self.assertEqual(self.call("export", token=w["token"], agent=w["service"])["receipts"], [])
        self.assertEqual([r["id"] for r in first["receipts"]], sent[:2])

    def published(self, token, agent):
        return next(row for bus in self.call("snapshot", token=token)["buses"] for row in bus["agents"] if row["id"] == agent)

    def test_service_publishes_a_bounded_agent_list_that_the_snapshot_shows(self):
        a, w = self.service_pair()
        published = [{"id": "prover-1", "name": "Prover 1", "status": "live", "role": "prover"},
                     {"id": "lemma-7", "name": "Lemma 7", "status": "queueable", "role": "worker", "parent": "prover-1"}]
        self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": published}])
        self.assertEqual(self.published(w["token"], w["service"])["agents"], published)
        # Every viewer of the service sees the same claim; agents that publish nothing carry no field.
        self.assertEqual(self.published(a["token"], w["service"])["agents"], published)
        self.assertNotIn("agents", self.published(a["token"], w["codex"]))
        # Only a service publishes, and a malformed field is a bad request; a bad entry is dropped on its own,
        # counted in the result, and never stops the rest of the list or the heartbeat from landing.
        self.refused("heartbeat", token=w["token"], agents=[{"id": w["codex"], "status": "queueable", "agents": published}])
        self.refused("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": "list"}])
        self.assertEqual(self.published(w["token"], w["service"])["agents"], published)
        mixed = [{"id": "x", "message": "secret"}, {"name": "no id"}, {"id": ""}, {"id": "  "}, {"id": "x", "status": "busy"},
                 {"id": "keep"}, {"id": "keep"}, {"id": "orphan", "parent": "missing"}, {"id": "x" * 129}, {"id": "\u00e9" * 128},
                 {"id": "x", "name": "a\x00b"}, {"id": "x", "name": "a\x7fb"}, "text", {"id": "child", "parent": "keep"}]
        result = self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": mixed}])
        self.assertEqual(result["dropped_agents"], {w["service"]: 11})
        self.assertEqual(self.published(w["token"], w["service"])["agents"],
                         [{"id": "keep", "name": "keep", "status": "queueable"}, {"id": "orphan", "name": "orphan", "status": "queueable"},
                          {"id": "child", "name": "child", "status": "queueable", "parent": "keep"}])
        many = [{"id": "p%d" % i} for i in range(MAX_PUBLISHED_AGENTS + 10)]
        result = self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": many}])
        self.assertEqual((result["dropped_agents"], len(self.published(w["token"], w["service"])["agents"])), ({w["service"]: 10}, MAX_PUBLISHED_AGENTS))
        self.assertNotIn("dropped_agents", self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": published}]))
        # A heartbeat without the field keeps the list; an empty list clears it; a registration drops it.
        self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable"}])
        self.assertEqual(self.published(w["token"], w["service"])["agents"], published)
        self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": []}])
        self.assertEqual(self.published(w["token"], w["service"])["agents"], [])
        self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": published}])
        self.register(token=w["token"], name="service:" + "1" * 32, kind="service", status="queueable")
        self.assertNotIn("agents", self.published(w["token"], w["service"]))
        # Defaults: status queueable, name the id.
        self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable", "agents": [{"id": "solo"}]}])
        self.assertEqual(self.published(w["token"], w["service"])["agents"], [{"id": "solo", "name": "solo", "status": "queueable"}])
        # Only a live row carries the list: a service whose heartbeats stopped shows offline without agents.
        self.now[0] += LIVE_TTL + 1
        offline = self.published(w["token"], w["service"])
        self.assertEqual(offline["status"], "offline")
        self.assertNotIn("agents", offline)
        self.call("heartbeat", token=w["token"], agents=[{"id": w["service"], "status": "queueable"}])
        self.assertEqual(self.published(w["token"], w["service"])["agents"], [{"id": "solo", "name": "solo", "status": "queueable"}])

    def test_a_hub_without_published_agents_accepts_the_heartbeat_and_drops_the_field(self):
        # The 0.6.1 broker, as released, from this repository's history; a shallow checkout cannot qualify this.
        shown = subprocess.run(["git", "show", "b3c6815:lib/bus_broker.py"], cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True)
        if shown.returncode:
            self.skipTest("0.6.1 source is not in this checkout; old-hub acceptance is unqualified here")
        with tempfile.TemporaryDirectory(prefix="broker-0.6.1-") as temp:
            Path(temp, "bus_broker_061.py").write_text(shown.stdout)
            spec = importlib.util.spec_from_file_location("bus_broker_061", Path(temp, "bus_broker_061.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            old = module.Broker(Path(temp) / "hub", clock=lambda: self.now[0])
            service = old.handle(old.admin_token, {"op": "register", "session_key": "service:" + "2" * 32, "name": "svc",
                                                   "kind": "service", "status": "queueable"})
            result = old.handle(old.admin_token, {"op": "heartbeat", "agents": [{"id": service["id"], "status": "queueable",
                                                  "agents": [{"id": "w1", "name": "Worker", "status": "live"}]}]})
            self.assertEqual((result["ok"], result["updated"]), (True, 1))
            row = next(agent for bus in old.handle(old.admin_token, {"op": "snapshot"})["buses"] for agent in bus["agents"]
                       if agent["id"] == service["id"])
            self.assertEqual(row["status"], "queueable")
            self.assertNotIn("agents", row)

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
