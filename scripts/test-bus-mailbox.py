#!/usr/bin/env python3
"""Hub mailboxes hold mail for an absent name, for the owner of that mailbox only.

Every broker here is an in-process instance on a temporary state directory.
The CLI cases route bus.request to such a broker; no HTTP listener, worker,
socket, provider or user configuration is touched.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import bus
import bus_broker as module
from bus_broker import Broker, MESSAGE_TTL, RECEIPT_RETENTION

DAY = 86400
T0 = 1800000000.0
NOT_FOUND = {"ok": False, "error": "unknown or unavailable recipient", "code": "not_found"}
V04_SHA256 = "bb5aeb1cf17dad3c36ab84c346c0bbdf8f8285c5b6ee8a21e51c329f9ef7ea63"


def released_v04():
    fixture = ROOT / "scripts/fixtures/bus_broker_v04.py"
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != V04_SHA256:
        raise AssertionError("the frozen v0.4 fixture changed")
    spec = importlib.util.spec_from_file_location("released_bus_broker_v04_mailbox", fixture)
    released = importlib.util.module_from_spec(spec)
    prior = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(released)
    finally:
        sys.dont_write_bytecode = prior
    return released


class Base(unittest.TestCase):
    env = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bus-mailbox-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        patch = mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                                            "USER": "fixture", "LOGNAME": "fixture", **self.env}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.now = [T0]
        self.state = self.root / "hub"
        self.b = self.open()
        self.sessions = {}

    def open(self, state=None):
        return Broker(state or self.state, clock=lambda: self.now[0])

    def raw(self, op, who=None, **fields):
        if who in self.sessions:
            token, digest = self.sessions[who]
            return self.b.handle(token, dict(fields, op=op), reader=who, reader_hash=digest)
        return self.b.handle(self.b.admin_token if who is None else who, dict(fields, op=op))

    def call(self, op, who=None, **fields):
        result = self.raw(op, who, **fields)
        self.assertTrue(result.get("ok"), result)
        return result

    def refused(self, op, who=None, code=None, **fields):
        result = self.raw(op, who, **fields)
        self.assertFalse(result.get("ok"), result)
        if code is not None:
            self.assertEqual(result["code"], code, result)
        return result

    def device(self, bus="general", user=None, issuer=None, label="device"):
        fields = {"bus": bus} if user is None else {"bus": bus, "user": user}
        invite = self.call("invite", issuer, **fields)["invite"]
        return self.call("redeem", "", invite=invite, device=label)

    def grant(self, device, bus):
        invite = self.call("invite", bus=bus, user=device["user"])["invite"]
        self.call("redeem", device["token"], invite=invite)

    def register(self, token, name, bus="general", session=None, **extra):
        return self.call("register", token, session_key=session or "codex:" + name, name=name,
                         kind="codex", bus=bus, **extra)

    def identify(self, token, name, session=None):
        return self.call("identify", token, session_key=session or "codex:" + name, name=name, kind="codex")

    def send(self, token, sender, target, message="fixture", bus="general", **extra):
        return self.raw("send", token, sender=sender, target=target, message=message, bus=bus, **extra)

    def hold(self, token, sender, target, message="fixture", bus="general", **extra):
        result = self.send(token, sender, target, message, bus, hold=True, **extra)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["status"], "held", result)
        return result

    def inbox(self, token, agent):
        got = []
        while True:
            messages = self.call("poll", token, agent=agent)["messages"]
            if not messages:
                return got
            got.append(messages[0])
            self.call("ack", token, agent=agent, id=messages[0]["id"], lease=messages[0]["lease"], status="delivered")

    def receipt(self, token, mid):
        return self.call("receipt", token, id=mid)

    def rows(self, sql, *params):
        with sqlite3.connect(str(self.b.db_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def box(self, report, bus="general"):
        return next(entry for entry in report["mailbox"] if entry["bus"] == bus)

    def absent_box(self, token, name, bus="general"):
        """A mailbox made the way the issue describes: register, then leave."""
        agent = self.register(token, name, bus=bus)
        self.call("leave", token, agent=agent["id"], bus=bus)
        return agent


class LocalHubTests(Base):
    def test_no_mailbox_fails_exactly_as_before(self):
        coord = self.identify(self.b.admin_token, "coord")["id"]
        for extra in ({}, {"hold": True}, {"hold": True, "hold_for": "principal:admin"}, {"hold": "yes"}):
            self.assertEqual(self.send(None, coord, "nobody", **extra), NOT_FOUND)
        # A mailbox belongs to one bus: a name kept on photonics does not hold on general.
        self.call("create", bus="photonics")
        self.absent_box(self.b.admin_token, "router", bus="photonics")
        self.assertEqual(self.send(None, coord, "router", hold=True), NOT_FOUND)
        self.assertEqual(self.rows("SELECT * FROM held_messages"), [])
        self.assertEqual([row["name"] for row in self.rows("SELECT * FROM mailboxes")], ["router"])

    def test_previously_registered_name_holds_then_drains_in_order_to_its_owner(self):
        first = self.register(self.b.admin_token, "router", session="codex:first")
        self.call("retire", agent=first["id"])
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = []
        for index in range(3):
            self.now[0] += 10
            held.append(self.hold(None, coord, "router", "task %d" % index))
        self.assertEqual(held[0]["owner"], "principal:admin")
        self.assertEqual(held[0]["mailbox"], "router")
        self.assertIsNone(held[0]["target"])
        self.assertEqual(held[0]["expires_at"], T0 + 10 + 7 * DAY)
        self.assertEqual(held[0]["reply_until"], held[0]["expires_at"] + MESSAGE_TTL)
        self.assertEqual(held[0]["mailbox_protocol"], "bus-mailbox/v1")
        self.assertEqual([self.receipt(None, row["id"])["status"] for row in held], ["held"] * 3)
        # A peer device holds only into a mailbox it names explicitly.
        peer = self.device(label="peer")
        peer_coord = self.identify(peer["token"], "peer-coord")["id"]
        self.assertEqual(self.send(peer["token"], peer_coord, "router", hold=True), NOT_FOUND)
        explicit = self.hold(peer["token"], peer_coord, "router", "from the peer", hold_for="principal:admin")
        self.now[0] += 2 * DAY
        second = self.register(self.b.admin_token, "router", session="codex:second")
        self.assertEqual(self.box(second), {"bus": "general", "name": "router", "recorded": True,
                                            "drained": 4, "cancelled": 0})
        self.assertEqual(self.receipt(None, held[0]["id"])["status"], "accepted")
        got = self.inbox(self.b.admin_token, second["id"])
        self.assertEqual([m["message"] for m in got], ["task 0", "task 1", "task 2", "from the peer"])
        self.assertEqual([m["id"] for m in got], [row["id"] for row in held] + [explicit["id"]])
        self.assertEqual([m["created_at"] for m in got], [T0 + 10, T0 + 20, T0 + 30, T0 + 30])
        self.assertEqual([m["sender"]["id"] for m in got], [coord] * 3 + [peer_coord])
        self.assertEqual(self.receipt(None, held[0]["id"])["status"], "delivered")
        self.assertEqual(self.receipt(peer["token"], explicit["id"])["status"], "delivered")
        self.call("reply", sender=second["id"], id=got[0]["id"], message="done")
        answer = self.inbox(self.b.admin_token, coord)
        self.assertEqual([(m["message"], m["in_reply_to"]) for m in answer], [("done", held[0]["id"])])
        self.assertEqual(self.rows("SELECT * FROM held_messages"), [])

    def test_reserved_name_on_private_bus_holds_before_first_registration(self):
        self.call("create", bus="photonics")
        member = self.device(bus="photonics", label="member")
        outsider = self.device(label="outsider")
        reserved = self.call("mailbox_reserve", member["token"], bus="photonics", name="worker-1")
        self.assertEqual((reserved["owner"], reserved["origin"]), ("principal:" + member["principal"], "reserved"))
        self.refused("mailbox_reserve", outsider["token"], "forbidden", bus="photonics", name="worker-1")
        self.refused("mailbox_reserve", member["token"], "invalid_request", bus="photonics", name="a_" + "0" * 32)
        self.refused("mailbox_reserve", member["token"], "invalid_request", bus="photonics", name="")
        coord = self.register(member["token"], "coord", bus="photonics")["id"]
        held = self.hold(member["token"], coord, "worker-1", "start here", bus="photonics")
        # A device that is not a member of photonics learns nothing there.
        outside = self.identify(outsider["token"], "outside")["id"]
        self.assertEqual(self.send(outsider["token"], outside, "worker-1", bus="photonics", hold=True,
                                   hold_for=reserved["owner"]), NOT_FOUND)
        worker = self.register(member["token"], "worker-1", bus="photonics")
        self.assertEqual(self.box(worker, "photonics")["drained"], 1)
        self.assertEqual([m["id"] for m in self.inbox(member["token"], worker["id"])], [held["id"]])

    def test_a_different_owner_registering_the_name_receives_nothing(self):
        self.absent_box(self.b.admin_token, "router")
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = self.hold(None, coord, "router", "for the operator only")
        bob = self.device(user="bob", label="bob")
        bob_router = self.register(bob["token"], "router")
        self.assertEqual(self.box(bob_router)["drained"], 0)
        self.assertEqual(self.inbox(bob["token"], bob_router["id"]), [])
        self.assertEqual(self.receipt(None, held["id"])["status"], "held")
        # Live delivery is unchanged: a send to a registered name goes to it.
        live = self.send(None, coord, "router", "live", hold=True)
        self.assertEqual((live["status"], live["target"]), ("accepted", bob_router["id"]))
        self.call("leave", bob["token"], agent=bob_router["id"], bus="general")
        # Two owners now keep a mailbox for router; each send names exactly one.
        again = self.hold(None, coord, "router", "second for the operator")
        self.assertEqual(again["owner"], "principal:admin")
        bob_coord = self.identify(bob["token"], "bob-coord")["id"]
        bob_held = self.hold(bob["token"], bob_coord, "router", "bob's own")
        self.assertEqual(bob_held["owner"], "principal:" + bob["principal"])
        mine = self.register(self.b.admin_token, "router", session="codex:operator-router")
        self.assertEqual(self.box(mine)["drained"], 2)
        self.assertEqual([m["message"] for m in self.inbox(self.b.admin_token, mine["id"])],
                         ["for the operator only", "second for the operator"])
        self.assertEqual(self.receipt(bob["token"], bob_held["id"])["status"], "held")

    def test_default_invites_on_a_hub_without_accounts_are_keyed_per_device(self):
        self.absent_box(self.b.admin_token, "router")
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = self.hold(None, coord, "router")
        colleague = self.device(label="colleague")  # attributed to the operator's login
        self.assertEqual(colleague["user"], "fixture")
        peer = self.device(user="peer", label="peer")
        for device in (colleague, peer):
            agent = self.register(device["token"], "router")
            self.assertEqual(self.box(agent)["drained"], 0)
            self.assertEqual(self.inbox(device["token"], agent["id"]), [])
            self.call("leave", device["token"], agent=agent["id"], bus="general")
        self.assertEqual(self.receipt(None, held["id"])["status"], "held")

    def test_held_mail_survives_restart_and_reply_window_starts_at_delivery(self):
        self.absent_box(self.b.admin_token, "router")
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = self.hold(None, coord, "router", "survive")
        self.b = self.open()
        self.assertEqual(self.receipt(None, held["id"])["status"], "held")
        self.assertEqual(self.rows("SELECT message FROM held_messages"), [{"message": "survive"}])
        self.now[0] = T0 + 2 * DAY
        router = self.register(self.b.admin_token, "router", session="codex:later")
        envelope = self.call("poll", agent=router["id"])["messages"][0]
        self.assertEqual(envelope["created_at"], T0)
        self.assertEqual(envelope["expires_at"], T0 + 2 * DAY + MESSAGE_TTL)
        self.assertEqual(envelope["conversation_expires_at"], T0 + 2 * DAY + MESSAGE_TTL)
        self.call("ack", agent=router["id"], id=envelope["id"], lease=envelope["lease"], status="delivered")
        self.now[0] = T0 + 2 * DAY + 23 * 3600
        self.call("reply", sender=router["id"], id=envelope["id"], message="within the window")
        self.now[0] = T0 + 2 * DAY + 25 * 3600
        self.refused("reply", None, "not_found", sender=router["id"], id=envelope["id"], message="too late")

    def test_live_and_registered_offline_recipients_are_unchanged(self):
        coord = self.identify(self.b.admin_token, "coord")["id"]
        peer = self.device(label="peer")
        offline = self.register(peer["token"], "offline")
        self.now[0] += 3600  # no heartbeat for an hour: registered but offline
        sent = self.send(None, coord, "offline", "still a live-path message", hold=True)
        self.assertEqual((sent["status"], sent["target"], sent["expires_at"]),
                         ("accepted", offline["id"], self.now[0] + MESSAGE_TTL))
        ordinary = self.call("poll", peer["token"], agent=offline["id"])["messages"][0]
        # Drained mail reaches the worker in exactly the ordinary envelope shape.
        self.absent_box(self.b.admin_token, "router")
        self.hold(None, coord, "router")
        router = self.register(self.b.admin_token, "router", session="codex:router-2")
        drained = self.call("poll", agent=router["id"])["messages"][0]
        self.assertEqual(set(drained), set(ordinary))
        # Ambiguity and absent names without a hold request are unchanged.
        for session in ("codex:x1", "codex:x2"):
            self.register(peer["token"], "twin", session=session)
        self.refused("send", None, "ambiguous", sender=coord, target="twin", message="x", hold=True)
        self.absent_box(self.b.admin_token, "gone")
        self.assertEqual(self.send(None, coord, "gone"), NOT_FOUND)

    def test_identify_rename_and_live_send_drain_held_mail_first(self):
        self.call("create", bus="photonics")
        member = self.device(bus="photonics", label="member")
        self.grant(member, "general")
        self.call("mailbox_reserve", member["token"], bus="photonics", name="router")
        worker = self.register(member["token"], "worker-0", bus="photonics", session="codex:w")
        coord = self.register(member["token"], "coord", bus="photonics")["id"]
        first = self.hold(member["token"], coord, "router", "held first", bus="photonics")
        renamed = self.identify(member["token"], "router", session="codex:w")
        self.assertEqual(renamed["id"], worker["id"])
        self.assertEqual(self.box(renamed, "photonics")["drained"], 1)
        live = self.send(member["token"], coord, "router", "live second", bus="photonics")
        self.assertEqual(live["status"], "accepted")
        self.assertEqual([m["id"] for m in self.inbox(member["token"], worker["id"])], [first["id"], live["id"]])

    def test_access_changes_cancel_held_mail_and_rejoining_revives_nothing(self):
        self.call("create", bus="photonics")
        owner = self.device(bus="photonics", label="owner")
        sender = self.device(bus="photonics", label="sender")
        self.absent_box(owner["token"], "router", bus="photonics")
        key = "principal:" + owner["principal"]
        coord = self.register(sender["token"], "coord", bus="photonics", session="codex:coord")["id"]
        left = self.hold(sender["token"], coord, "router", bus="photonics", hold_for=key)
        self.call("leave", sender["token"], agent=coord, bus="photonics")
        self.assertEqual(self.receipt(sender["token"], left["id"])["status"], "cancelled")
        self.register(sender["token"], "coord", bus="photonics", session="codex:coord")
        retired = self.hold(sender["token"], coord, "router", bus="photonics", hold_for=key)
        self.call("retire", sender["token"], agent=coord)
        self.assertEqual(self.receipt(sender["token"], retired["id"])["status"], "cancelled")
        coord = self.register(sender["token"], "coord", bus="photonics", session="codex:coord")["id"]
        router = self.register(owner["token"], "router", bus="photonics")
        self.assertEqual(self.box(router, "photonics")["drained"], 0)
        self.call("leave", owner["token"], agent=router["id"], bus="photonics")
        # The owner purges one sender, releases its mailbox, and a sender withdraws its own message.
        stranger = self.device(bus="photonics", label="stranger")
        noisy = self.register(stranger["token"], "noisy", bus="photonics")["id"]
        spam = self.hold(stranger["token"], noisy, "router", bus="photonics", hold_for=key)
        kept = self.hold(sender["token"], coord, "router", bus="photonics", hold_for=key)
        purged = self.call("mailbox_release", owner["token"], bus="photonics", name="router", sender=noisy)
        self.assertEqual((purged["cancelled"], purged["released"]), (1, False))
        self.assertEqual(self.receipt(stranger["token"], spam["id"])["status"], "cancelled")
        self.assertEqual(self.receipt(sender["token"], kept["id"])["status"], "held")
        self.refused("mailbox_cancel", stranger["token"], "not_found", id=kept["id"])
        withdrawn = self.call("mailbox_cancel", sender["token"], id=kept["id"])
        self.assertEqual(withdrawn["status"], "cancelled")
        self.assertEqual(self.receipt(sender["token"], kept["id"])["detail"], "cancelled by its sender")
        self.refused("mailbox_cancel", sender["token"], "conflict", id=kept["id"])
        last = self.hold(sender["token"], coord, "router", bus="photonics", hold_for=key)
        released = self.call("mailbox_release", owner["token"], bus="photonics", name="router")
        self.assertEqual((released["cancelled"], released["released"]), (1, True))
        self.assertEqual(self.receipt(sender["token"], last["id"])["status"], "cancelled")
        self.assertEqual(self.send(sender["token"], coord, "router", bus="photonics", hold=True, hold_for=key), NOT_FOUND)
        # Revoking the owner's device cancels its mail and removes its mailboxes.
        self.absent_box(owner["token"], "router", bus="photonics")
        doomed = self.hold(sender["token"], coord, "router", bus="photonics", hold_for=key)
        self.call("revoke", principal=owner["principal"])
        self.assertEqual(self.receipt(sender["token"], doomed["id"])["status"], "cancelled")
        self.assertEqual(self.rows("SELECT * FROM mailboxes WHERE owner=?", key), [])
        self.assertTrue(all(row["message"] == "" for row in self.rows("SELECT message FROM held_messages")))

    def test_listing_shows_held_counts_only_on_visible_buses(self):
        self.call("create", bus="photonics")
        member = self.device(bus="photonics", label="member")
        self.grant(member, "general")
        general_only = self.device(label="general-only")
        self.absent_box(member["token"], "router", bus="photonics")
        self.absent_box(member["token"], "desk")
        coord = self.register(member["token"], "coord", bus="photonics")["id"]
        self.hold(member["token"], coord, "router", bus="photonics")
        self.hold(member["token"], coord, "router", bus="photonics")
        mine = self.call("mailbox_ls", member["token"])
        self.assertEqual(mine["owner"], "principal:" + member["principal"])
        rows = {(row["bus"], row["name"]): row for row in mine["mailboxes"]}
        self.assertEqual(rows[("photonics", "router")]["held"], 2)
        self.assertTrue(rows[("photonics", "router")]["yours"])
        self.assertEqual(rows[("photonics", "router")]["origin"], "registered")
        self.assertEqual(rows[("photonics", "router")]["retention_seconds"], 7 * DAY)
        self.assertFalse(mine["truncated"])
        theirs = self.call("mailbox_ls", general_only["token"])
        self.assertEqual([(row["bus"], row["name"], row["held"], row["yours"]) for row in theirs["mailboxes"]],
                         [("general", "desk", 0, False)])
        self.assertNotIn("last_used_at", theirs["mailboxes"][0])
        self.assertNotIn("origin", theirs["mailboxes"][0])
        self.refused("mailbox_ls", general_only["token"], "forbidden", bus="photonics")
        everything = self.call("mailbox_ls")
        self.assertEqual(len(everything["mailboxes"]), 3)  # desk, router, and coord's own
        self.assertTrue(all("last_used_at" in row for row in everything["mailboxes"]))

    def test_services_and_exact_ids_never_hold_or_own_mailboxes(self):
        coord = self.identify(self.b.admin_token, "coord")["id"]
        ghost = self.register(self.b.admin_token, "ghost")
        self.call("retire", agent=ghost["id"])
        self.assertEqual(self.send(None, coord, ghost["id"], hold=True), NOT_FOUND)
        shaped = "a_" + "f" * 32
        self.assertNotIn("mailbox", self.register(self.b.admin_token, shaped))
        sid = str(uuid.uuid4())
        service = self.call("register", session_key="service:" + sid, name="svc", kind="service",
                            bus="general", status="queueable")
        self.assertNotIn("mailbox", service)
        self.assertEqual(self.rows("SELECT name FROM mailboxes WHERE name IN (?,?)", shaped, "svc"), [])
        # A fenced service sender keeps today's exact-recipient refusal.
        retired = self.call("service_fence_retire", agent=service["id"], service_id=sid,
                            expected_epoch=0, request_id="retire")
        fenced = self.call("register", session_key="service:" + sid, name="svc", kind="service", bus="general",
                           status="queueable", agent=service["id"], expected_epoch=retired["epoch"],
                           request_id="reactivate", service_config_hash="a" * 64)
        self.absent_box(self.b.admin_token, "router")
        self.assertEqual(self.send(None, service["id"], "router", hold=True, expected_sender_epoch=fenced["epoch"],
                                   request_id="fenced-send"), NOT_FOUND)


class LimitTests(Base):
    def test_overflow_refuses_the_new_message_and_never_evicts(self):
        self.absent_box(self.b.admin_token, "router")
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = [self.hold(None, coord, "router", "body %d" % index)["id"] for index in range(module.MAX_PENDING)]
        full = self.refused("send", None, "limit", sender=coord, target="router", message="one too many", hold=True)
        self.assertIn("router", full["error"])
        self.assertIn("nothing was stored", full["error"])
        stored = self.rows("SELECT id,message FROM held_messages ORDER BY ordering")
        self.assertEqual([row["id"] for row in stored], held)
        self.assertEqual([row["message"] for row in stored], ["body %d" % i for i in range(module.MAX_PENDING)])

    def test_storage_caps_count_every_row_and_blank_terminal_bodies(self):
        coord = self.identify(self.b.admin_token, "coord")["id"]
        for name in ("one", "two"):
            self.absent_box(self.b.admin_token, name)
        with mock.patch.object(module, "MAX_HELD", 3):
            self.hold(None, coord, "one")
            self.hold(None, coord, "one")
            self.assertEqual(self.call("mailbox_release", name="one")["cancelled"], 2)
            self.hold(None, coord, "two")
            hub = self.refused("send", None, "limit", sender=coord, target="two", message="x", hold=True)
            self.assertIn("nothing was stored", hub["error"])
        self.assertEqual(self.rows("SELECT status,message FROM held_messages WHERE status='cancelled'"),
                         [{"status": "cancelled", "message": ""}] * 2)
        # One device cannot use up the hub; others keep their own allowance.
        peer = self.device(label="peer")
        peer_coord = self.identify(peer["token"], "peer-coord")["id"]
        with mock.patch.object(module, "MAX_HELD_PER_SENDER", 3):
            self.refused("send", None, "limit", sender=coord, target="two", message="x", hold=True)
            self.hold(peer["token"], peer_coord, "two", hold_for="principal:admin")
        with mock.patch.object(module, "MAX_HELD_PER_SENDER_BOX", 1):
            self.refused("send", peer["token"], "limit", sender=peer_coord, target="two", message="x",
                         hold=True, hold_for="principal:admin")
            self.hold(None, coord, "two")  # the owner's own mail is not capped per sender
        self.call("create", bus="photonics")
        self.grant(peer, "photonics")
        self.absent_box(peer["token"], "pbox", bus="photonics")
        pcoord = self.register(peer["token"], "pcoord", bus="photonics")["id"]
        with mock.patch.object(module, "MAX_HELD_PER_BUS", 0):
            self.refused("send", peer["token"], "limit", sender=pcoord, target="pbox", message="x",
                         bus="photonics", hold=True)

    def test_a_drain_moves_only_what_fits_and_keeps_the_rest_in_order(self):
        self.absent_box(self.b.admin_token, "router")
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = [self.hold(None, coord, "router", "part %d" % index)["id"] for index in range(3)]
        used = self.rows("SELECT COUNT(*) AS n FROM messages")[0]["n"]
        with mock.patch.object(module, "MAX_RECORDS", used + 1):
            router = self.register(self.b.admin_token, "router", session="codex:router-2")
            self.assertEqual(self.box(router)["drained"], 1)
            self.assertEqual([self.receipt(None, mid)["status"] for mid in held], ["accepted", "held", "held"])
        got = self.inbox(self.b.admin_token, router["id"])
        self.assertEqual([m["id"] for m in got], held)

    def test_mailbox_caps_evict_only_your_own_empty_registered_mailboxes(self):
        device = self.device(label="renamer")
        coord = self.identify(device["token"], "coord")["id"]
        self.absent_box(self.b.admin_token, "m0")  # another owner's oldest empty mailbox
        self.now[0] += 1
        with mock.patch.object(module, "MAX_MAILBOXES_PER_PRINCIPAL", 2):
            self.absent_box(device["token"], "n1")
            self.now[0] += 1
            self.absent_box(device["token"], "n2")
            self.now[0] += 1
            third = self.register(device["token"], "n3")
            self.assertTrue(self.box(third)["recorded"])
            self.assertEqual(sorted(row["name"] for row in self.rows("SELECT name FROM mailboxes")), ["m0", "n2", "n3"])
            self.call("leave", device["token"], agent=third["id"], bus="general")
            self.hold(device["token"], coord, "n2")
            self.hold(device["token"], coord, "n3")
            fourth = self.register(device["token"], "n4")
            self.assertFalse(self.box(fourth)["recorded"])
            self.refused("mailbox_reserve", device["token"], "limit", name="n5")
            other = self.device(label="other")
            self.assertTrue(self.box(self.register(other["token"], "n6"))["recorded"])
        with mock.patch.object(module, "MAX_MAILBOXES_PER_BUS", 3):
            self.assertFalse(self.box(self.register(self.b.admin_token, "n7"))["recorded"])
        with mock.patch.object(module, "MAX_MAILBOXES", 3):
            self.assertFalse(self.box(self.register(self.b.admin_token, "n8"))["recorded"])

    def test_idle_mailboxes_are_pruned_but_long_lived_registrations_keep_theirs(self):
        router = self.register(self.b.admin_token, "router")
        self.call("mailbox_reserve", name="ghost")
        for days in (10, 20, 30, 40):
            self.now[0] = T0 + days * DAY
            self.call("heartbeat", agents=[{"id": router["id"], "status": "queueable"}])
        coord = self.identify(self.b.admin_token, "coord")["id"]
        self.call("leave", agent=router["id"], bus="general")
        self.hold(None, coord, "router")
        self.assertEqual(self.send(None, coord, "ghost", hold=True), NOT_FOUND)
        self.assertEqual([row["name"] for row in self.rows("SELECT name FROM mailboxes")], ["router"])


class RetentionTests(Base):
    env = {"BUS_MAILBOX_RETENTION": json.dumps({"default": 2 * DAY, "buses": {"photonics": 3 * DAY, "quiet": 0}})}

    def test_retention_is_per_bus_and_expiry_is_terminal(self):
        for bus_name in ("photonics", "quiet"):
            self.call("create", bus=bus_name)
        coord = self.identify(self.b.admin_token, "coord")["id"]
        self.absent_box(self.b.admin_token, "router")
        held = self.hold(None, coord, "router", "expires")
        self.assertEqual(held["expires_at"], T0 + 2 * DAY)
        self.absent_box(self.b.admin_token, "router", bus="photonics")
        pcoord = self.register(self.b.admin_token, "pcoord", bus="photonics")["id"]
        self.assertEqual(self.hold(None, pcoord, "router", bus="photonics")["expires_at"], T0 + 3 * DAY)
        self.absent_box(self.b.admin_token, "router", bus="quiet")
        qcoord = self.register(self.b.admin_token, "qcoord", bus="quiet")["id"]
        self.assertEqual(self.send(None, qcoord, "router", bus="quiet", hold=True), NOT_FOUND)
        self.refused("mailbox_reserve", None, "forbidden", bus="quiet", name="later")
        self.now[0] = T0 + 2 * DAY
        expired = self.receipt(None, held["id"])
        self.assertEqual((expired["status"], expired["detail"]), ("expired", "held message expired"))
        self.assertEqual(self.rows("SELECT message FROM held_messages WHERE id=?", held["id"]), [{"message": ""}])
        router = self.register(self.b.admin_token, "router", session="codex:back")
        self.assertEqual(self.box(router)["drained"], 0)
        self.now[0] = T0 + 2 * DAY + RECEIPT_RETENTION + 1
        self.refused("receipt", None, "not_found", id=held["id"])

    def test_invalid_policies_fail_before_any_state_is_opened(self):
        for raw in ("true", "1.5", json.dumps({"default": True}), json.dumps({"default": 3600}),
                    json.dumps({"default": 31 * DAY}), json.dumps({"default": DAY, "other": 1}),
                    json.dumps({"default": DAY, "buses": {"Bad Name": DAY}}),
                    '{"default": 86400, "default": 86400}', json.dumps({"buses": {}}),
                    json.dumps({"default": DAY, "buses": {"x%d" % i: DAY for i in range(400)}}),
                    json.dumps({"default": DAY, "buses": []}), "", "{"):
            state = self.root / ("never-" + hashlib.sha256(raw.encode()).hexdigest()[:12])
            with self.subTest(raw=raw[:40]), mock.patch.dict(os.environ, {"BUS_MAILBOX_RETENTION": raw}):
                with self.assertRaises(ValueError):
                    Broker(state)
                self.assertFalse(state.exists())
        with mock.patch.dict(os.environ, {"BUS_MAILBOX_RETENTION": json.dumps({"default": 0})}):
            self.assertEqual(Broker(self.root / "disabled").mailbox_retention, {"default": 0, "buses": {}})

    def test_absent_policy_defaults_to_seven_days(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BUS_MAILBOX_RETENTION")
            self.assertEqual(Broker(self.root / "default").mailbox_retention, {"default": 7 * DAY, "buses": {}})


class HostedAccountTests(Base):
    env = {"BUS_GATEWAY_SHARED_SECRET": "fixture-" + "s" * 40,
           "BUS_READER_USERS": json.dumps({"alice-reader": "alice", "bob-reader": "bob", "ops-reader": "ops"}),
           "BUS_ADMIN_READERS": "ops-reader"}

    def setUp(self):
        super().setUp()
        for reader in ("alice-reader", "bob-reader", "ops-reader"):
            digest = hashlib.sha256(reader.encode()).hexdigest()
            self.sessions[reader] = (self.b.browser_session(reader, digest)["token"], digest)
        self.call("create", "alice-reader", bus="team")

    def test_an_account_key_needs_enrollment_evidence(self):
        mine = [self.device("team", "alice", "alice-reader", "alice-%d" % i) for i in range(2)]
        first = self.register(mine[0]["token"], "router", bus="team")
        self.call("retire", mine[0]["token"], agent=first["id"])
        coord = self.register(mine[1]["token"], "coord", bus="team")["id"]
        held = self.hold(mine[1]["token"], coord, "router", "for alice", bus="team")
        self.assertEqual(held["owner"], "account:alice")
        # bob owns another bus, adds alice to it, and mints a device attributed to her.
        self.call("create", "bob-reader", bus="trap")
        self.call("member_add", "bob-reader", bus="trap", user="alice")
        minted = self.device("trap", "alice", "bob-reader", "minted")
        self.assertEqual(minted["user"], "alice")
        event = self.call("event_create", "alice-reader", bus="team")["invite"]
        self.call("redeem", minted["token"], invite=event)
        captured = self.register(minted["token"], "router", bus="team")
        self.assertEqual(self.box(captured, "team")["drained"], 0)
        self.assertEqual(self.inbox(minted["token"], captured["id"]), [])
        self.assertEqual(self.receipt(mine[1]["token"], held["id"])["status"], "held")
        # Two guests of one event code are separate owners.
        guests = [self.call("redeem", "", invite=event, device="guest-%d" % i) for i in range(2)]
        self.absent_box(guests[0]["token"], "helper", bus="team")
        gcoord = self.register(guests[0]["token"], "gcoord", bus="team")["id"]
        guest_mail = self.hold(guests[0]["token"], gcoord, "helper", bus="team")
        self.assertEqual(self.box(self.register(guests[1]["token"], "helper", bus="team"), "team")["drained"], 0)
        self.assertEqual(self.receipt(guests[0]["token"], guest_mail["id"])["status"], "held")
        # The hub operator's own invitation for alice is trusted enrollment evidence.
        operator_issued = self.device("team", "alice", None, "alice-3")
        mine_again = self.register(operator_issued["token"], "router", bus="team")
        self.assertEqual(self.box(mine_again, "team")["drained"], 1)

    def test_a_reassigned_legacy_device_keeps_its_per_device_key(self):
        alice = self.device("general", "alice", None, "alice")
        self.absent_box(alice["token"], "desk")
        coord = self.identify(alice["token"], "coord")["id"]
        held = self.hold(alice["token"], coord, "desk")
        legacy = self.device("general", "bob", None, "legacy")
        with sqlite3.connect(str(self.b.db_path)) as db:
            db.execute("UPDATE principals SET user=NULL,mailbox_account=NULL WHERE id=?", (legacy["principal"],))
        self.call("create", "bob-reader", bus="trap")
        self.call("member_add", "bob-reader", bus="trap", user="alice")
        invite = self.call("invite", "bob-reader", bus="trap", user="alice")["invite"]
        self.assertEqual(self.call("redeem", legacy["token"], invite=invite)["user"], "alice")
        claimed = self.register(legacy["token"], "desk")
        self.assertEqual(self.box(claimed)["drained"], 0)
        self.assertEqual(self.receipt(alice["token"], held["id"])["status"], "held")

    def test_removing_an_account_from_a_bus_cancels_mail_held_for_it(self):
        self.call("create", "bob-reader", bus="shared")
        self.call("member_add", "bob-reader", bus="shared", user="alice")
        alice = self.device("shared", "alice", "alice-reader", "alice")
        bob = self.device("shared", "bob", "bob-reader", "bob")
        self.absent_box(alice["token"], "worker", bus="shared")
        bcoord = self.register(bob["token"], "bcoord", bus="shared")["id"]
        listed = {row["name"]: row for row in self.call("mailbox_ls", bob["token"], bus="shared")["mailboxes"]}
        self.assertEqual(listed["worker"]["owner"], "account:alice")
        held = self.hold(bob["token"], bcoord, "worker", bus="shared", hold_for="account:alice")
        self.call("member_remove", "bob-reader", bus="shared", user="alice")
        self.assertEqual(self.receipt(bob["token"], held["id"])["status"], "cancelled")
        self.assertNotIn("worker", [row["name"] for row in self.call("mailbox_ls", bob["token"], bus="shared")["mailboxes"]])

    def test_browser_sessions_cannot_use_mailbox_operations(self):
        for reader in ("alice-reader", "ops-reader"):
            for op, fields in (("mailbox_ls", {}), ("mailbox_reserve", {"name": "x"}),
                               ("mailbox_release", {"name": "x"}), ("mailbox_cancel", {"id": "m_" + "0" * 32})):
                self.refused(op, reader, "forbidden", **fields)


class ReleasedBrokerTests(Base):
    env = {"BUS_MAILBOX_RETENTION": json.dumps({"default": 10 * DAY})}

    def test_an_unpublished_sender_is_kept_while_its_mail_is_held(self):
        self.absent_box(self.b.admin_token, "router")
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = self.hold(None, coord, "router")
        self.now[0] += 8 * DAY
        self.call("snapshot")
        router = self.register(self.b.admin_token, "router", session="codex:router-2")
        self.assertEqual(self.box(router)["drained"], 1)
        self.assertEqual([m["id"] for m in self.inbox(self.b.admin_token, router["id"])], [held["id"]])

    def test_a_drain_rechecks_what_an_older_broker_may_have_changed(self):
        self.absent_box(self.b.admin_token, "router")
        coord = self.identify(self.b.admin_token, "coord")["id"]
        held = self.hold(None, coord, "router")
        with sqlite3.connect(str(self.b.db_path)) as db:
            # What a retirement and reactivation leave when no mailbox hook ran.
            db.execute("UPDATE agents SET epoch=epoch+1 WHERE id=?", (coord,))
        router = self.register(self.b.admin_token, "router", session="codex:router-2")
        self.assertEqual((self.box(router)["drained"], self.box(router)["cancelled"]), (0, 1))
        self.assertEqual(self.receipt(None, held["id"])["status"], "cancelled")
        owner = self.device(label="owner")
        self.absent_box(owner["token"], "desk")
        with sqlite3.connect(str(self.b.db_path)) as db:
            # An owner removed from the bus by a broker that kept the mailbox row.
            db.execute("DELETE FROM grants WHERE principal=?", (owner["principal"],))
        self.assertEqual(self.send(None, coord, "desk", hold=True, hold_for="principal:" + owner["principal"]), NOT_FOUND)

    def test_released_v04_runs_on_a_hub_with_held_mail_and_reupgrade_drains(self):
        released = released_v04()
        device = self.device(label="device")
        token = device["token"]
        self.absent_box(token, "router")
        coord = self.identify(token, "coord")["id"]
        held = self.hold(token, coord, "router", "across a rollback")

        def old(broker, who, op, ok=True, **fields):
            result = broker.handle(who, dict(fields, op=op))
            self.assertIs(result.get("ok"), ok, result)
            return result

        rolled = released.Broker(self.state, clock=lambda: self.now[0])
        old(rolled, rolled.admin_token, "create", bus="during-rollback")
        invite = old(rolled, rolled.admin_token, "invite", bus="general", user="peer")["invite"]
        old(rolled, "", "redeem", invite=invite, device="peer")
        self.assertEqual(old(rolled, token, "send", ok=False, sender=coord, target="nowhere", message="x", hold=True),
                         NOT_FOUND)
        self.assertEqual(old(rolled, token, "mailbox_ls", ok=False),
                         {"ok": False, "error": "unknown operation", "code": "invalid_request"})
        # The same session registers its name again while the old broker runs,
        # which neither shows nor drains held mail.
        again = old(rolled, token, "register", session_key="codex:router", name="router", kind="codex", bus="general")
        old(rolled, token, "heartbeat", agents=[{"id": again["id"], "status": "queueable"}])
        self.assertEqual(old(rolled, token, "poll", agent=again["id"])["messages"], [])
        self.b = self.open()
        self.assertEqual(self.receipt(token, held["id"])["status"], "held")
        # Without registering again, held mail drains first: a live send does not overtake it.
        live = self.send(token, coord, "router", "sent after the re-upgrade")
        self.assertEqual(live["status"], "accepted")
        self.assertEqual([m["id"] for m in self.inbox(token, again["id"])], [held["id"], live["id"]])
        self.absent_box(token, "polled")
        polled = self.hold(token, coord, "polled")
        rolled = released.Broker(self.state, clock=lambda: self.now[0])
        quiet = old(rolled, token, "register", session_key="codex:polled", name="polled", kind="codex", bus="general")
        self.b = self.open()
        # Nobody sends; the worker's next poll drains it.
        self.assertEqual([m["id"] for m in self.inbox(token, quiet["id"])], [polled["id"]])

        # An old broker may delete an unpublished sender's row; the drain then cancels with a reason.
        self.absent_box(token, "later")
        lone = self.identify(token, "lone")["id"]
        orphan = self.hold(token, lone, "later")
        rolled = released.Broker(self.state, clock=lambda: self.now[0])
        self.now[0] += 8 * DAY
        old(rolled, rolled.admin_token, "snapshot")
        self.b = self.open()
        self.assertEqual(self.rows("SELECT id FROM agents WHERE id=?", lone), [])
        back = self.register(token, "later", session="codex:later-2")
        self.assertEqual((self.box(back)["drained"], self.box(back)["cancelled"]), (0, 1))
        receipt = self.receipt(token, orphan["id"])
        self.assertEqual(receipt["status"], "cancelled")
        self.assertIn("sender", receipt["detail"])


class MailboxCliTests(unittest.TestCase):
    """The CLI against an in-process broker: no HTTP, worker or session probe."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bus-mailbox-cli-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        patch = mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
                                            "COMM_STATE": str(root / "client"), "USER": "fixture",
                                            "LOGNAME": "fixture"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.broker = Broker(root / "broker")
        invite = self.broker.handle(self.broker.admin_token, {"op": "invite", "bus": "general"})["invite"]
        enrolled = self.broker.handle("", {"op": "redeem", "invite": invite, "device": "fixture"})
        self.token, self.principal = enrolled["token"], enrolled["principal"]
        self.conn = {"url": "https://fixture.invalid", "token": self.token}
        self.calls = []
        for patcher in (mock.patch.object(bus, "connection", return_value=self.conn),
                        mock.patch.object(bus, "request", side_effect=self.request),
                        mock.patch.object(bus, "device_metadata", return_value={}),
                        mock.patch.object(bus, "start_worker"),
                        mock.patch.object(bus, "identity", side_effect=self.identity)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def identity(target="self", session=None, kind=None, name=None):
        name = name or "coord"
        return {"session_key": "codex:" + name, "kind": "codex", "thread": name, "name": name,
                "binary": "/usr/bin/true", "status": "queueable"}

    def request(self, conn, op, **payload):
        self.calls.append({"op": op, **payload})
        result = self.broker.handle(self.token, {"op": op, **payload})
        if not result["ok"]:
            raise bus.BusError(result["error"], result.get("code"))
        return result

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = bus.run(bus.parser().parse_args(arguments))
        return result, out.getvalue(), err.getvalue()

    def absent_router(self):
        router = self.request(self.conn, "register", session_key="codex:router", name="router", kind="codex", bus="general")
        self.request(self.conn, "leave", agent=router["id"], bus="general")
        return router

    def test_send_holds_for_an_absent_name_and_keeps_the_reply_adapter(self):
        registered, _, _ = self.cli("register", "--name", "router")
        self.assertEqual(registered["mailbox"], [{"bus": "general", "name": "router", "recorded": True,
                                                  "drained": 0, "cancelled": 0}])
        self.request(self.conn, "leave", agent=registered["id"], bus="general")
        _, out, err = self.cli("send", "router", "--", "please", "start")
        receipt = json.loads(out)
        self.assertEqual(receipt["status"], "held")
        self.assertIn("not delivered", receipt["note"])
        self.assertIn("do not resend", receipt["note"].lower())
        self.assertIn("held", err)
        wire = [row for row in self.calls if row["op"] == "send"][-1]
        self.assertIs(wire["hold"], True)
        self.assertNotIn("hold_for", wire)
        regs = json.loads((bus.state_dir() / "registrations.json").read_text())
        coord = next(row for row in regs.values() if row["name"] == "coord")
        self.assertEqual(coord["reply_until"], receipt["reply_until"])
        status, _, _ = self.cli("receipt", receipt["id"])
        self.assertEqual(status["status"], "held")

    def test_wire_adds_hold_only_for_names_and_older_namespaces_still_send(self):
        router = self.absent_router()
        with self.assertRaises(bus.BusError):
            self.cli("send", "router", "--no-hold", "--", "refused as before")
        self.assertNotIn("hold", self.calls[-1])
        with self.assertRaises(bus.BusError):
            self.cli("send", router["id"], "--", "exact ids never hold")
        self.assertNotIn("hold", self.calls[-1])
        _, out, _ = self.cli("send", "router", "--hold-for", "principal:" + self.principal, "--", "named owner")
        self.assertEqual(json.loads(out)["status"], "held")
        self.assertEqual(self.calls[-1]["hold_for"], "principal:" + self.principal)
        with self.assertRaises(bus.BusError):
            self.cli("send", "router", "--no-hold", "--hold-for", "principal:x", "--", "contradiction")
        # A caller that builds the Namespace itself, as mention delivery does, lacks the new fields.
        namespace = argparse.Namespace(command="send", hub=None, target="router", bus="general", sender="self",
                                       expected_sender_epoch=None, expected_recipient_epoch=None,
                                       request_id=None, message=["from", "a", "mention"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            bus.run(namespace)
        self.assertEqual(json.loads(out.getvalue())["status"], "held")

    def test_mailbox_commands_round_trip(self):
        reserved, _, _ = self.cli("mailbox", "reserve", "worker-1")
        self.assertEqual((reserved["name"], reserved["origin"]), ("worker-1", "reserved"))
        listed, _, _ = self.cli("mailbox", "ls")
        self.assertEqual([(row["name"], row["yours"], row["held"]) for row in listed["mailboxes"]],
                         [("worker-1", True, 0)])
        _, out, _ = self.cli("send", "worker-1", "--", "first task")
        held = json.loads(out)
        listed, _, _ = self.cli("mailbox", "ls", "--bus", "general")
        self.assertEqual(listed["mailboxes"][0]["held"], 1)
        cancelled, _, _ = self.cli("mailbox", "cancel", held["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        released, _, _ = self.cli("mailbox", "release", "worker-1")
        self.assertEqual((released["released"], released["cancelled"]), (True, 0))
        self.assertEqual(self.cli("mailbox", "ls")[0]["mailboxes"], [])

    def test_an_older_hub_gets_a_clear_upgrade_error(self):
        released = released_v04()
        old = released.Broker(Path(self.temp.name) / "old-hub")

        def old_request(conn, op, **payload):
            result = old.handle(old.admin_token, {"op": op, **payload})
            if not result["ok"]:
                raise bus.BusError(result["error"], result.get("code"))
            return result

        with mock.patch.object(bus, "request", side_effect=old_request):
            with self.assertRaises(bus.BusError) as raised:
                self.cli("mailbox", "ls")
        self.assertIn("does not support bus mailboxes", str(raised.exception))

    def test_a_drained_message_says_how_long_it_was_held(self):
        record = {"kind": "codex", "binary": "/usr/bin/true", "thread": "t", "url": "https://fixture.invalid",
                  "id": "a_" + "1" * 32}
        base = {"id": "m_" + "2" * 32, "sender": {"id": "a_" + "3" * 32, "name": "coord"}, "bus": "general",
                "message": "body", "reply_to": "m_" + "2" * 32}
        sent = []
        def fake_run(argv, **kwargs):
            sent.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(bus.subprocess, "run", side_effect=fake_run):
            bus.deliver(record, {**base, "created_at": T0, "expires_at": T0 + MESSAGE_TTL})
            bus.deliver(record, {**base, "created_at": T0, "expires_at": T0 + 3 * DAY + MESSAGE_TTL})
        self.assertNotIn("Held at the hub", sent[0])
        self.assertIn("Held at the hub since " + time.strftime("%Y-%m-%d %H:%M", time.gmtime(T0)), sent[1])


class InstallerTests(unittest.TestCase):
    def test_installer_forwards_mailbox_retention_to_the_real_broker(self):
        with tempfile.TemporaryDirectory(prefix="bus-mailbox-installer-") as temp:
            root = Path(temp).resolve()
            spec = importlib.util.spec_from_file_location("mailbox_installer", ROOT / "scripts/install-bus-hub.py")
            installer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installer)
            policy = {"default": 3 * DAY, "buses": {"photonics": 0}}
            settings = root / "settings.json"
            settings.write_text(json.dumps({"BUS_GATEWAY_SHARED_SECRET": "synthetic-" + "s" * 40,
                                            "BUS_MAILBOX_RETENTION": policy}))
            settings.chmod(0o600)
            state = root / "hub"
            argv = ["install-bus-hub.py", "--source", str(ROOT), "--state", str(state), "--settings", str(settings)]
            fake = subprocess.CompletedProcess([], 0, "", "")
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "USER": "fixture", "LOGNAME": "fixture"}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(installer.sys, "argv", argv), mock.patch.object(installer.sys, "platform", "darwin"), \
                 mock.patch.object(Path, "home", return_value=root), \
                 mock.patch.object(installer.subprocess, "run", return_value=fake), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                installer.main()
                with mock.patch.object(module, "serve") as serve:
                    exec(compile((state / "run-hub.py").read_bytes(), "synthetic-run-hub.py", "exec"), {})
            broker = serve.call_args.args[0]
            self.assertEqual(broker.mailbox_retention, policy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
