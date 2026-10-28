#!/usr/bin/env python3
"""Retirement releases active registration quota without erasing history."""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bus_broker import Broker, MESSAGE_TTL, RECEIPT_RETENTION


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="communicate-retire-")
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
            self.assertEqual(result["code"], code, result)
        return result

    def join(self, device):
        invite = self.call("invite", bus="general")
        return self.call("redeem", token="", invite=invite["invite"], device=device)

    def register(self, token, name, **extra):
        return self.call("register", token=token, session_key=name, name=name, bus="general", **extra)["id"]

    def general_ids(self, token=None):
        snapshot = self.call("snapshot", token=token)
        return [a["id"] for a in next(b for b in snapshot["buses"] if b["name"] == "general")["agents"]]

    def test_retire_releases_active_quota_across_churn(self):
        device = self.join("svc")
        peer = self.join("peer")
        peer_agent = self.register(peer["token"], "peer")
        with mock.patch("bus_broker.MAX_AGENTS", 2):
            for run in range(6):
                agent = self.register(device["token"], "service:%d" % run, kind="service")
                sent = self.call("send", token=device["token"], sender=agent, target=peer_agent, message="run %d" % run)
                retired = self.call("retire", token=device["token"], agent=agent)
                self.assertEqual((retired["agent"], retired["retired"]), (agent, True))
                self.assertEqual(self.call("receipt", token=device["token"], id=sent["id"])["id"], sent["id"])
            self.register(device["token"], "service:active-1", kind="service")
            self.register(device["token"], "service:active-2", kind="service")
            self.refused("register", token=device["token"], session_key="service:overflow",
                         name="overflow", bus="general", code="limit")

    def test_leave_alone_still_counts_toward_quota(self):
        device = self.join("svc")
        with mock.patch("bus_broker.MAX_AGENTS", 2):
            for name in ("one", "two"):
                agent = self.register(device["token"], name)
                left = self.call("leave", token=device["token"], agent=agent, bus="general")
                self.assertEqual(left, {"ok": True, "agent": agent, "bus": "general", "left": True})
            self.refused("register", token=device["token"], session_key="three", name="three",
                         bus="general", code="limit")

    def test_reactivation_keeps_native_id_and_is_cap_checked(self):
        device = self.join("native")
        with mock.patch("bus_broker.MAX_AGENTS", 2):
            first = self.register(device["token"], "claude-session")
            self.call("retire", token=device["token"], agent=first)
            self.assertNotIn(first, self.general_ids())
            second = self.register(device["token"], "second")
            self.register(device["token"], "third")
            self.refused("register", token=device["token"], session_key="claude-session",
                         name="claude-session", bus="general", code="limit")
            self.call("retire", token=device["token"], agent=second)
            again = self.call("register", token=device["token"], session_key="claude-session",
                              name="renamed", bus="general")
        self.assertEqual(again["id"], first)
        self.assertEqual(again["buses"], ["general"])
        self.assertIn(first, self.general_ids())

    def test_retained_rows_are_bounded_per_principal_and_globally(self):
        device = self.join("churn")
        other = self.join("other")
        with mock.patch("bus_broker.MAX_AGENTS", 1), mock.patch("bus_broker.MAX_RETAINED_AGENTS", 3):
            retired = []
            for run in range(3):
                agent = self.register(device["token"], "s%d" % run)
                self.call("retire", token=device["token"], agent=agent)
                retired.append(agent)
            self.refused("register", token=device["token"], session_key="s3", name="s3", bus="general", code="limit")
            # Reactivating an existing retained row adds no row, so it remains possible.
            self.assertEqual(self.register(device["token"], "s0"), retired[0])
            # The bound is per principal; another device is unaffected.
            self.register(other["token"], "independent")
        with mock.patch("bus_broker.MAX_TOTAL_RETAINED_AGENTS", 4):
            self.refused("register", token=other["token"], session_key="global-overflow",
                         name="global-overflow", bus="general", code="limit")

    def test_retire_is_owner_only_without_disclosure(self):
        alice, bob = self.join("alice"), self.join("bob")
        bob_agent = self.register(bob["token"], "bob")
        missing = self.refused("retire", token=alice["token"], agent="a_" + "0" * 32, code="not_found")
        foreign = self.refused("retire", token=alice["token"], agent=bob_agent, code="not_found")
        self.assertEqual(foreign["error"], missing["error"])
        self.refused("retire", agent=bob_agent, code="not_found")  # administrators do not retire others
        self.assertIn(bob_agent, self.general_ids())
        alice_agent = self.register(alice["token"], "alice")
        self.call("send", token=alice["token"], sender=alice_agent, target=bob_agent, message="still reachable")

    def test_retired_agent_is_unreachable_and_cannot_originate(self):
        alice, bob = self.join("alice"), self.join("bob")
        a, b = self.register(alice["token"], "alice"), self.register(bob["token"], "bob")
        first = self.call("send", token=alice["token"], sender=a, target=b, message="first")
        delivered = self.call("poll", token=bob["token"], agent=b)["messages"][0]
        self.call("ack", token=bob["token"], agent=b, id=first["id"], lease=delivered["lease"], status="delivered")
        pending = self.call("send", token=alice["token"], sender=a, target=b, message="unread")
        self.call("retire", token=bob["token"], agent=b)
        self.assertEqual(self.call("receipt", token=alice["token"], id=pending["id"])["status"], "cancelled")
        self.refused("send", token=alice["token"], sender=a, target=b, message="after", code="not_found")
        self.refused("send", token=bob["token"], sender=b, target=a, message="retired sender", code="conflict")
        self.refused("reply", token=bob["token"], sender=b, id=first["id"], message="late", code="conflict")
        self.assertEqual(self.call("poll", token=bob["token"], agent=b)["messages"], [])

    def test_reactivation_does_not_reopen_a_request_already_fetched(self):
        # A service adapter acknowledges a fetched request as queued in its local
        # journal. Retirement does not settle that job, but no reply can follow.
        alice, svc = self.join("alice"), self.join("svc")
        a, s = self.register(alice["token"], "alice"), self.register(svc["token"], "svc")
        asked = self.call("send", token=alice["token"], sender=a, target=s, message="job")
        lease = self.call("poll", token=svc["token"], agent=s)["messages"][0]["lease"]
        self.call("ack", token=svc["token"], agent=s, id=asked["id"], lease=lease, status="queued")
        self.call("retire", token=svc["token"], agent=s)
        self.assertEqual(self.call("receipt", token=alice["token"], id=asked["id"])["status"], "queued")
        self.assertEqual(self.register(svc["token"], "svc"), s)
        self.refused("reply", token=svc["token"], sender=s, id=asked["id"], message="late", code="not_found")

    def test_history_receipts_and_reply_replay_survive_retirement(self):
        alice, bob = self.join("alice"), self.join("bob")
        a, b = self.register(alice["token"], "alice"), self.register(bob["token"], "bob")
        original = self.call("send", token=alice["token"], sender=a, target=b, message="question")
        lease = self.call("poll", token=bob["token"], agent=b)["messages"][0]["lease"]
        self.call("ack", token=bob["token"], agent=b, id=original["id"], lease=lease, status="delivered")
        answer = self.call("reply", token=bob["token"], sender=b, id=original["id"], message="answer", request_id="r1")
        self.call("retire", token=bob["token"], agent=b)
        self.now[0] += RECEIPT_RETENTION - 60
        for token, mid in ((alice["token"], original["id"]), (bob["token"], original["id"]),
                           (bob["token"], answer["id"]), (alice["token"], answer["id"])):
            self.assertEqual(self.call("receipt", token=token, id=mid)["id"], mid)
        replay = self.call("reply", token=bob["token"], sender=b, id=original["id"], message="answer", request_id="r1")
        self.assertEqual(replay["id"], answer["id"])
        self.refused("receipt", token=self.join("carol")["token"], id=original["id"], code="not_found")

    def test_retire_is_idempotent_and_heartbeat_does_not_revive(self):
        device = self.join("svc")
        agent = self.register(device["token"], "svc")
        first = self.call("retire", token=device["token"], agent=agent)
        self.now[0] += 5
        second = self.call("retire", token=device["token"], agent=agent)
        self.assertEqual((first["retired"], second["retired"]), (True, True))
        self.call("heartbeat", token=device["token"], agents=[{"id": agent, "status": "live"}])
        self.assertNotIn(agent, self.general_ids())
        with mock.patch("bus_broker.MAX_AGENTS", 1):
            self.register(device["token"], "replacement")

    def test_retired_rows_age_out_with_their_history(self):
        alice, bob = self.join("alice"), self.join("bob")
        a, b = self.register(alice["token"], "alice"), self.register(bob["token"], "bob")
        sent = self.call("send", token=alice["token"], sender=a, target=b, message="x")
        self.call("retire", token=bob["token"], agent=b)
        self.now[0] += 2 * RECEIPT_RETENTION + 60
        self.call("snapshot")
        with sqlite3.connect(Path(self.tmp.name) / "bus.sqlite3") as db:
            self.assertIsNone(db.execute("SELECT 1 FROM agents WHERE id=?", (b,)).fetchone())
        self.refused("receipt", token=alice["token"], id=sent["id"], code="not_found")

    def test_epoch_orders_retirement_and_same_id_reactivation(self):
        device = self.join("epoch")
        fields = {"session_key": "epoch", "name": "epoch", "bus": "general"}
        first = self.call("register", token=device["token"], **fields)
        self.assertEqual(first["epoch"], 0)
        self.assertEqual(self.call("register", token=device["token"], **fields)["epoch"], 0)
        retired = self.call("retire", token=device["token"], agent=first["id"])
        self.assertEqual(retired["epoch"], 1)
        self.assertEqual(self.call("retire", token=device["token"], agent=first["id"])["epoch"], 1)
        back = self.call("register", token=device["token"], **fields)
        self.assertEqual((back["id"], back["epoch"]), (first["id"], 2))
        hidden = self.call("identify", token=device["token"], session_key="epoch", name="epoch")
        self.assertEqual((hidden["id"], hidden["epoch"]), (first["id"], 2))

    def test_upgrade_adds_column_without_retiring_existing_rows(self):
        device = self.join("legacy")
        agent = self.register(device["token"], "legacy")
        self.call("leave", token=device["token"], agent=agent, bus="general")
        path = Path(self.tmp.name) / "bus.sqlite3"
        with sqlite3.connect(path) as db:
            self.assertIn("retired_at", [row[1] for row in db.execute("PRAGMA table_info(agents)")])
            # Recreate the exact pre-retirement table, as an existing hub has it.
            db.executescript("""PRAGMA foreign_keys=OFF;
                CREATE TABLE agents_old(
                  id TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES principals(id),
                  session_key TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                  description TEXT NOT NULL, status TEXT NOT NULL, last_seen REAL NOT NULL,
                  UNIQUE(principal,session_key));
                INSERT INTO agents_old SELECT id,principal,session_key,name,kind,description,status,last_seen FROM agents;
                DROP TABLE agents;
                ALTER TABLE agents_old RENAME TO agents;""")
            self.assertNotIn("retired_at", [row[1] for row in db.execute("PRAGMA table_info(agents)")])
        # Idle and unpublished for two days, yet never retired automatically.
        self.now[0] += 2 * MESSAGE_TTL
        reopened = Broker(self.tmp.name, clock=lambda: self.now[0])
        self.assertEqual(reopened.admin_token, self.admin)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT retired_at FROM agents WHERE id=?", (agent,)).fetchone(), (None,))
        self.b = reopened
        with mock.patch("bus_broker.MAX_AGENTS", 1):
            self.refused("register", token=device["token"], session_key="other", name="other",
                         bus="general", code="limit")
            self.assertEqual(self.register(device["token"], "legacy"), agent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
