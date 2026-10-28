#!/usr/bin/env python3
"""Cross-device publication and bounded reply grants; no live state or agents."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bus_broker import Broker, MESSAGE_TTL, LEASE_TTL


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bus-conversations-")
        self.now = [1800000000.0]
        self.b = Broker(self.tmp.name, clock=lambda: self.now[0])
        self.admin = self.b.admin_token
        self.left = self.device("left")
        self.right = self.device("right")
        self.third = self.device("third")
        self.a = self.agent(self.left, "hidden", publish=False)
        self.bob = self.agent(self.right, "bob")
        self.eve = self.agent(self.third, "eve")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, token, op, **fields):
        result = self.b.handle(token, {"op": op, **fields})
        self.assertTrue(result["ok"], result)
        return result

    def deny(self, token, op, **fields):
        result = self.b.handle(token, {"op": op, **fields})
        self.assertFalse(result["ok"], result)
        return result

    def device(self, name, bus="general"):
        invite = self.call(self.admin, "invite", bus=bus)
        return self.call(None, "redeem", invite=invite["invite"], device=name)

    def admit(self, device, bus):
        invite = self.call(self.admin, "invite", bus=bus)
        self.call(device["token"], "redeem", invite=invite["invite"])

    def agent(self, device, name, publish=True, bus="general"):
        fields = {"session_key": name, "name": name, "kind": "codex"}
        if publish:
            fields["bus"] = bus
        return self.call(device["token"], "register" if publish else "identify", **fields)["id"]

    def send(self, bus="general"):
        return self.call(self.left["token"], "send", sender=self.a, target=self.bob, bus=bus, message="question")

    def poll(self, device, agent):
        return self.call(device["token"], "poll_device", agents=[agent])["messages"]

    def ack(self, device, envelope):
        self.call(device["token"], "ack", agent=envelope["target"], id=envelope["id"],
                  lease=envelope["lease"], status="queued")

    def test_general_unpublished_sender_and_reply_without_directory_exposure(self):
        sent = self.send()
        for device in (self.left, self.right, self.third):
            snapshot = self.call(device["token"], "snapshot")
            self.assertNotIn(self.a, [a["id"] for bus in snapshot["buses"] for a in bus["agents"]])
        envelope = self.poll(self.right, self.bob)[0]
        self.assertEqual(envelope["reply_to"], sent["id"])
        self.assertEqual(envelope["sender"]["id"], self.a)
        self.assertEqual(envelope["conversation_expires_at"], self.now[0] + MESSAGE_TTL)
        self.ack(self.right, envelope)
        reply = self.call(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="answer")
        received = self.poll(self.left, self.a)[0]
        self.assertEqual(received["id"], reply["id"])
        self.assertEqual(received["message"], "answer")
        self.ack(self.left, received)
        self.now[0] += 300
        followup = self.call(self.left["token"], "reply", sender=self.a, id=reply["id"], message="followup")
        self.assertEqual(followup["conversation_expires_at"], sent["conversation_expires_at"])
        self.assertEqual(self.poll(self.right, self.bob)[0]["id"], followup["id"])
        # Identifying again neither creates membership nor changes the address.
        identified = self.call(self.left["token"], "identify", session_key="hidden", name="renamed", kind="codex")
        self.assertEqual(identified["id"], self.a)
        self.assertEqual(identified["buses"], [])

    def test_reply_is_bound_to_fetched_message_recipient_device_and_original_bus(self):
        sent = self.send()
        self.deny(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="before fetch")
        self.poll(self.right, self.bob)
        other = self.agent(self.right, "other-on-bobs-device")
        for device, sender in ((self.third, self.eve), (self.right, other), (self.left, self.a)):
            self.deny(device["token"], "reply", sender=sender, id=sent["id"], message="wrong participant")
        self.deny(self.third["token"], "reply", sender=self.bob, id=sent["id"], message="stolen id")
        self.deny(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="wrong bus", bus="photonics")
        self.deny(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="wrong target", target=self.eve)
        for device, sender in ((self.third, self.eve), (self.right, self.bob)):
            self.deny(device["token"], "send", sender=sender, target=self.a, message="unsolicited")
            self.deny(device["token"], "send", sender=sender, target="hidden", message="unsolicited")
        self.call(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="allowed")

    def test_private_requires_device_grants_and_both_explicit_memberships(self):
        self.call(self.admin, "create", bus="photonics")
        self.admit(self.right, "photonics")
        self.agent(self.right, "bob", bus="photonics")
        self.deny(self.left["token"], "send", sender=self.a, target=self.bob, bus="photonics", message="no grant")
        self.admit(self.left, "photonics")
        self.deny(self.left["token"], "send", sender=self.a, target=self.bob, bus="photonics", message="no membership")
        self.assertEqual(self.agent(self.left, "hidden", bus="photonics"), self.a)
        sent = self.send(bus="photonics")
        envelope = self.poll(self.right, self.bob)[0]
        self.ack(self.right, envelope)
        reply = self.call(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="private answer")
        self.call(self.left["token"], "leave", agent=self.a, bus="photonics")
        self.assertEqual(self.poll(self.left, self.a), [])
        self.assertEqual(self.call(self.right["token"], "receipt", id=reply["id"])["status"], "cancelled")
        self.agent(self.left, "hidden", bus="photonics")
        self.deny(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="cannot revive")
        # Private participation doesn't publish on general; general outbound still works.
        self.send()
        general = next(bus for bus in self.call(self.left["token"], "snapshot")["buses"] if bus["name"] == "general")
        self.assertNotIn(self.a, [a["id"] for a in general["agents"]])

    def test_private_only_device_cannot_identify_or_initiate_general(self):
        self.call(self.admin, "create", bus="private")
        device = self.device("restricted", bus="private")
        agent = self.agent(device, "private-agent", bus="private")
        self.deny(device["token"], "identify", session_key="new", name="new")
        self.deny(device["token"], "send", sender=agent, target=self.bob, message="no general admission")

    def test_unpublication_closes_queued_replies_and_rejoining_cannot_revive(self):
        sent = self.send()
        self.poll(self.right, self.bob)
        reply = self.call(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="answer")
        self.call(self.right["token"], "leave", agent=self.bob, bus="general")
        self.assertEqual(self.poll(self.left, self.a), [])
        self.assertEqual(self.call(self.left["token"], "receipt", id=reply["id"])["status"], "cancelled")
        self.agent(self.right, "bob")
        self.deny(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="old conversation")
        self.send()

    def test_hidden_sender_can_end_reply_paths_without_ever_joining(self):
        sent = self.send()
        self.poll(self.right, self.bob)
        self.call(self.left["token"], "leave", agent=self.a, bus="general")
        self.deny(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="late")
        self.send()

    def test_reply_expiry_is_fixed_and_revocation_blocks_accepted_replies(self):
        sent = self.send()
        envelope = self.poll(self.right, self.bob)[0]
        self.ack(self.right, envelope)
        self.now[0] += MESSAGE_TTL - 1
        reply = self.call(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="last second")
        self.assertEqual(reply["expires_at"], sent["expires_at"])
        self.now[0] += 1
        self.assertEqual(self.poll(self.left, self.a), [])
        self.assertEqual(self.call(self.left["token"], "receipt", id=reply["id"])["status"], "expired")
        self.deny(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="too late")
        new = self.send()
        self.poll(self.right, self.bob)
        reply = self.call(self.right["token"], "reply", sender=self.bob, id=new["id"], message="answer")
        self.call(self.admin, "revoke", principal=self.right["principal"])
        self.assertEqual(self.poll(self.left, self.a), [])
        self.assertEqual(self.call(self.left["token"], "receipt", id=reply["id"])["status"], "cancelled")

    def test_device_poll_is_atomic_bounded_and_preserves_per_agent_order(self):
        other = self.agent(self.right, "second")
        one = self.send()
        self.now[0] += 1
        two = self.send()
        three = self.call(self.left["token"], "send", sender=self.a, target=other, message="second agent")
        self.deny(self.right["token"], "poll_device", agents=[self.bob, self.eve])
        self.deny(self.right["token"], "poll_device", agents=[])
        self.deny(self.right["token"], "poll_device", agents=[self.bob] * 129)
        self.deny(self.right["token"], "poll_device", agents=[{}])
        self.deny(self.right["token"], "poll_device", agents=[self.bob, other, self.bob])
        db = self.b._connect()
        try:
            rows = db.execute("SELECT status,lease FROM messages WHERE id IN (?,?,?)",
                              (one["id"], two["id"], three["id"])).fetchall()
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["status"] == "accepted" and row["lease"] is None for row in rows))
        finally:
            db.close()
        batch = self.call(self.right["token"], "poll_device", agents=[self.bob, other])["messages"]
        self.assertEqual({m["id"] for m in batch}, {one["id"], three["id"]})
        self.assertEqual(self.poll(self.right, self.bob), [])
        self.now[0] += LEASE_TTL + 1
        retry = self.poll(self.right, self.bob)[0]
        self.assertEqual(retry["id"], one["id"])
        self.ack(self.right, retry)
        self.assertEqual(self.poll(self.right, self.bob)[0]["id"], two["id"])

    def test_hidden_identity_storage_is_reclaimed_after_receipt_retention(self):
        sent = self.send()
        self.now[0] += MESSAGE_TTL + 1
        self.assertEqual(self.poll(self.right, self.bob), [])
        self.now[0] += 7 * MESSAGE_TTL + 1
        self.call(self.admin, "snapshot")
        with self.b._connect() as db:
            self.assertIsNone(db.execute("SELECT id FROM agents WHERE id=?", (self.a,)).fetchone())
            self.assertIsNone(db.execute("SELECT id FROM messages WHERE id=?", (sent["id"],)).fetchone())
            self.assertIsNotNone(db.execute("SELECT id FROM agents WHERE id=?", (self.bob,)).fetchone())

    def test_clock_rollback_or_equal_timestamps_cannot_bypass_active_lease(self):
        first = self.send()
        lease = self.poll(self.right, self.bob)[0]
        # Equal timestamps and a backwards wall clock must preserve acceptance
        # order, regardless of how randomly-generated message IDs sort.
        second = self.send()
        self.now[0] -= 1
        third = self.send()
        self.assertEqual(self.poll(self.right, self.bob), [])
        self.ack(self.right, lease)
        next_lease = self.poll(self.right, self.bob)[0]
        self.assertEqual(next_lease["id"], second["id"])
        self.ack(self.right, next_lease)
        self.assertEqual(self.poll(self.right, self.bob)[0]["id"], third["id"])

    def test_upgrade_preserves_queued_payload_expiry_and_scoped_reply(self):
        self.agent(self.left, "hidden")
        with self.b._connect() as db:
            db.execute("""INSERT INTO messages(id,sender,target,bus,message,status,created_at,expires_at,updated_at)
                          VALUES('m_legacy',?,?,'general','preserved payload','accepted',?,?,?)""",
                       (self.a, self.bob, self.now[0], self.now[0] + 123, self.now[0]))
        self.b = Broker(self.tmp.name, clock=lambda: self.now[0])
        envelope = self.poll(self.right, self.bob)[0]
        self.assertEqual(envelope["message"], "preserved payload")
        reply = self.call(self.right["token"], "reply", sender=self.bob, id=envelope["reply_to"], message="answer")
        self.assertEqual(reply["conversation_expires_at"], self.now[0] + 123)
        self.assertEqual(self.poll(self.left, self.a)[0]["message"], "answer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
