#!/usr/bin/env python3
"""Observed bus graph, bounded retention and existing directory privacy."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bus_broker import Broker, MESSAGE_TTL, RECEIPT_RETENTION

READER = "owui.12345678-1234-4234-8234-123456789abc"
READER_HASH = hashlib.sha256(b"graph-viewer-fixture").hexdigest()


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bus-graph-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "BUS_GATEWAY_SHARED_SECRET": "graph-tests-only-" + "s" * 40,
            "BUS_OPENWEBUI_READERS": "1", "BUS_READER_USERS": "{}", "BUS_ADMIN_READERS": READER,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 1800000000
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        self.admin = self.b.admin_token
        for bus in ("qit-wilde", "other-private"):
            self.call(self.admin, "create", bus=bus)
        self.left = self.device("Air", "aadarwal")
        self.right = self.device("Workstation", "peer")
        # Same display name on two owners' devices still means two graph nodes.
        self.a = self.publish(self.left)
        self.bob = self.publish(self.right)

    def call(self, token, op, **fields):
        result = self.b.handle(token, {"op": op, **fields})
        self.assertTrue(result["ok"], result)
        return result

    def device(self, name, user):
        invite = self.call(self.admin, "invite", bus="general", user=user)
        return self.call(None, "redeem", invite=invite["invite"], device=name,
                         device_metadata={"hostname": name.lower(), "platform": "test"})

    def publish(self, device, bus="general"):
        return self.call(device["token"], "register", bus=bus, session_key="fixture:agent",
                         name="research", kind="codex", status="queueable")["id"]

    def admit_pair(self, bus):
        for device in (self.left, self.right):
            invite = self.call(self.admin, "invite", bus=bus, user=device["user"])
            self.call(device["token"], "redeem", invite=invite["invite"])
            self.publish(device, bus)

    def send(self, bus="general", reverse=False, message="private payload sentinel"):
        device, sender, target = ((self.right, self.bob, self.a) if reverse else
                                  (self.left, self.a, self.bob))
        return self.call(device["token"], "send", sender=sender, target=target, bus=bus, message=message)

    def acknowledge(self, device, agent, status="delivered"):
        envelope = self.call(device["token"], "poll", agent=agent)["messages"][0]
        self.call(device["token"], "ack", agent=agent, id=envelope["id"], lease=envelope["lease"],
                  status=status, detail="private receipt detail sentinel")
        return envelope

    def buses(self, token=None):
        snapshot = self.call(self.admin if token is None else token, "snapshot")
        return {bus["name"]: bus for bus in snapshot["buses"]}

    def view(self, buses=None, **changes):
        return dict({"v": 1, "reader": READER, "reader_hash": READER_HASH,
                     "iat": self.now, "exp": self.now + 60,
                     "buses": ["qit-wilde"] if buses is None else buses}, **changes)

    def viewer_snapshot(self, token, view=None):
        return self.b.handle(token, {"op": "snapshot"}, reader=READER, reader_hash=READER_HASH,
                             view=self.view() if view is None else view)

    def test_empty_graph_does_not_invent_connections_and_preserves_node_identity(self):
        general = self.buses(self.left["token"])["general"]
        self.assertEqual(general["graph"], {"edges": [], "retention_seconds": 604800,
                                            "basis": "retained_bus_messages"})
        nodes = {node["id"]: node for node in general["agents"]}
        self.assertEqual(set(nodes), {self.a, self.bob})
        for agent, device in ((self.a, self.left), (self.bob, self.right)):
            self.assertEqual(nodes[agent]["device_id"], device["principal"])
            self.assertEqual(nodes[agent]["user"], device["user"])
            self.assertEqual(nodes[agent]["status"], "queueable")
        # A device sees its own host details; another device's stay with administrators.
        self.assertEqual(nodes[self.a]["device_metadata"], self.left["device_metadata"])
        self.assertNotIn("device_metadata", nodes[self.bob])
        administered = {node["id"]: node for node in self.buses()["general"]["agents"]}
        self.assertEqual(administered[self.bob]["device_metadata"], self.right["device_metadata"])

    def test_direction_counts_status_and_time_are_metadata_only(self):
        sent = self.send()
        first = self.acknowledge(self.right, self.bob, "queued")
        self.now += 3
        self.send()
        self.acknowledge(self.right, self.bob)
        self.now += 2
        self.send()
        self.send(reverse=True)
        graph = self.buses()["general"]["graph"]
        edges = {(edge["source"], edge["target"]): edge for edge in graph["edges"]}
        self.assertEqual(set(edges), {(self.a, self.bob), (self.bob, self.a)})
        self.assertEqual(edges[(self.a, self.bob)], {
            "source": self.a, "target": self.bob, "message_count": 3, "last_sent_at": self.now,
            "status_counts": {"accepted": 1, "delivered": 1, "queued": 1},
        })
        self.assertEqual(edges[(self.bob, self.a)]["message_count"], 1)
        self.assertEqual(edges[(self.bob, self.a)]["status_counts"], {"accepted": 1})
        for edge in graph["edges"]:
            self.assertEqual(set(edge), {"source", "target", "message_count", "last_sent_at", "status_counts"})
        encoded = json.dumps(graph)
        for private in ("private payload sentinel", "private receipt detail sentinel", sent["id"], first["lease"]):
            self.assertNotIn(private, encoded)

    def test_owui_scope_never_counts_other_bus_traffic_between_same_agents(self):
        self.admit_pair("qit-wilde")
        self.admit_pair("other-private")
        self.send()
        self.send("qit-wilde")
        self.send("qit-wilde")
        self.send("other-private", reverse=True)
        self.send("other-private", reverse=True)
        token = self.b.browser_session(READER, READER_HASH, self.view())["token"]
        snapshot = self.viewer_snapshot(token)
        self.assertTrue(snapshot["ok"], snapshot)
        self.assertTrue(snapshot["read_only"])
        self.assertFalse(snapshot["is_admin"])
        self.assertEqual([bus["name"] for bus in snapshot["buses"]], ["qit-wilde"])
        graph = snapshot["buses"][0]["graph"]
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["message_count"], 2)
        self.assertEqual(graph["edges"][0]["source"], self.a)
        for node in snapshot["buses"][0]["agents"]:
            self.assertEqual(node["buses"], ["qit-wilde"])
        self.assertNotIn("other-private", json.dumps(snapshot))
        self.assertNotIn("principals", snapshot)
        self.assertEqual(self.viewer_snapshot(token, self.view(buses=[]))["buses"], [])
        expired = self.view()
        self.now += 60
        self.assertEqual(self.viewer_snapshot(token, expired)["code"], "unauthorized")

    def test_device_view_excludes_private_graphs(self):
        self.admit_pair("qit-wilde")
        self.send()
        self.send("qit-wilde")
        third = self.device("Third", "viewer")
        buses = self.buses(third["token"])
        self.assertEqual(list(buses), ["general"])
        self.assertEqual(buses["general"]["graph"]["edges"][0]["message_count"], 1)

    def test_unpublished_general_initiator_and_reply_stay_hidden(self):
        hidden = self.call(self.left["token"], "identify", session_key="fixture:hidden",
                           name="unpublished sentinel", kind="codex")["id"]
        sent = self.call(self.left["token"], "send", sender=hidden, target=self.bob,
                         bus="general", message="private payload sentinel")
        self.acknowledge(self.right, self.bob)
        self.call(self.right["token"], "reply", sender=self.bob, id=sent["id"], message="private reply sentinel")
        for token in (self.admin, self.left["token"], self.right["token"]):
            snapshot = self.call(token, "snapshot")
            general = next(bus for bus in snapshot["buses"] if bus["name"] == "general")
            self.assertEqual(general["graph"]["edges"], [])
            self.assertNotIn(hidden, json.dumps(snapshot))
            self.assertNotIn("unpublished sentinel", json.dumps(snapshot))

    def test_leave_and_device_revoke_remove_edges_with_hidden_endpoints(self):
        self.admit_pair("qit-wilde")
        self.send("qit-wilde")
        self.send()
        self.call(self.left["token"], "leave", agent=self.a, bus="qit-wilde")
        buses = self.buses()
        self.assertEqual(buses["qit-wilde"]["graph"]["edges"], [])
        self.assertEqual(buses["general"]["graph"]["edges"][0]["message_count"], 1)
        self.call(self.admin, "revoke", principal=self.right["principal"])
        for bus in self.buses().values():
            self.assertEqual(bus["graph"]["edges"], [])
            self.assertNotIn(self.bob, [agent["id"] for agent in bus["agents"]])

    def test_terminal_receipt_retention_is_since_update_not_send_time(self):
        self.send()
        self.now += 10
        self.acknowledge(self.right, self.bob)
        self.now += RECEIPT_RETENTION
        # More than seven days after send, but exactly seven days since ack.
        self.assertEqual(self.buses()["general"]["graph"]["edges"][0]["status_counts"], {"delivered": 1})
        self.now += 1
        self.assertEqual(self.buses()["general"]["graph"]["edges"], [])

    def test_pending_expiry_remains_a_receipt_then_is_reclaimed(self):
        self.send()
        self.now += MESSAGE_TTL
        self.assertEqual(self.buses()["general"]["graph"]["edges"][0]["status_counts"], {"expired": 1})
        self.now += RECEIPT_RETENTION + 1
        self.assertEqual(self.buses()["general"]["graph"]["edges"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
