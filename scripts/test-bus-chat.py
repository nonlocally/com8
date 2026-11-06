#!/usr/bin/env python3
"""Human inbox, exact-agent delivery and gateway boundaries; isolated fixtures only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import bus_broker
from bus_broker import Broker, BusHTTPServer, CHAT_RETENTION, LEASE_TTL, MESSAGE_TTL, handler_factory

READER = "owui.12345678-1234-4234-8234-123456789abc"
OTHER_READER = "owui.87654321-4321-4321-8321-cba987654321"
HASH = hashlib.sha256(b"chat-fixture").hexdigest()
SECRET = "isolated-chat-fixture-" + "s" * 40


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="communicate-chat-")
        self.now = 1800000000
        self.grants = {
            "aadarsh": {"identity": "aadarwal", "buses": ["general", "qit-wilde"]},
            READER: {"identity": "aadarwal", "buses": ["qit-wilde"]},
            OTHER_READER: {"identity": "other-fixture", "buses": ["qit-wilde"]},
        }
        self.env = mock.patch.dict(os.environ, {
            "BUS_GATEWAY_SHARED_SECRET": SECRET, "BUS_OPENWEBUI_READERS": "1",
            "BUS_ADMIN_READERS": "aadarsh", "BUS_READER_USERS": json.dumps({"aadarsh": "aadarwal", "peer": "peer"}),
            "BUS_CHAT_READERS": json.dumps(self.grants),
            "BUS_CHAT_OPENWEBUI_TARGETS": "[]",
        })
        self.env.start()
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        self.admin("create", bus="qit-wilde")
        self.agent = self.admin("register", bus="qit-wilde", session_key="fixture-target", name="Exact fixture", kind="codex")["id"]
        self.second = self.admin("register", bus="qit-wilde", session_key="fixture-second", name="Exact fixture")["id"]
        self.unpublished = self.admin("identify", session_key="hidden", name="Hidden fixture")["id"]
        self.tokens = {reader: self.b.browser_session(reader, HASH, self.view(reader) if reader.startswith("owui.") else None)["token"]
                       for reader in ("aadarsh", "peer", READER, OTHER_READER)}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def view(self, reader=READER, buses=None, **fields):
        return dict({"v": 1, "reader": reader, "reader_hash": HASH, "iat": self.now, "exp": self.now + 60,
                     "buses": ["qit-wilde"] if buses is None else buses}, **fields)

    def raw(self, op, reader=READER, view=None, **fields):
        kwargs = {"reader": reader, "reader_hash": HASH}
        if reader.startswith("owui."):
            kwargs["view"] = self.view(reader) if view is None else view
        return self.b.handle(self.tokens[reader], {"op": op, **fields}, **kwargs)

    def call(self, op, **fields):
        result = self.raw(op, **fields)
        self.assertTrue(result["ok"], result)
        return result

    def admin(self, op, **fields):
        result = self.b.handle(self.b.admin_token, {"op": op, **fields})
        self.assertTrue(result["ok"], result)
        return result

    def open(self, reader=READER, agent=None, bus="qit-wilde"):
        return self.call("chat_open", reader=reader, bus=bus, agent=agent or self.agent)["chat"]["id"]

    def send(self, chat=None, reader=READER, request_id=None, message="Fixture question"):
        return self.call("chat_send", chat=chat or self.open(reader), reader=reader,
                         request_id=request_id or str(uuid.uuid4()), message=message)["message"]

    def poll(self, agent=None):
        return self.admin("poll", agent=agent or self.agent)["messages"]

    def ack(self, message, status="queued"):
        return self.admin("ack", agent=message["target"], id=message["id"], lease=message["lease"], status=status)

    def reply(self, message, text="Fixture answer"):
        return self.admin("reply", sender=self.agent, id=message["id"], message=text)

    def test_explicit_grants_only_and_narrow_readonly_exception(self):
        for token in (self.b.admin_token, "x" * 40):
            for op in bus_broker.CHAT_OPS:
                self.assertFalse(self.b.handle(token, {"op": op})["ok"])
        for op in bus_broker.CHAT_OPS:
            self.assertEqual(self.raw(op, reader="peer")["code"], "forbidden")
        snapshot = self.call("snapshot")
        self.assertTrue(snapshot["read_only"])
        self.assertFalse(snapshot["is_admin"])
        self.assertEqual(snapshot["chat"], {"enabled": True, "buses": ["qit-wilde"], "openwebui": []})
        self.assertEqual(self.call("snapshot", reader="peer")["chat"], {"enabled": False, "buses": [], "openwebui": []})
        for op in ("register", "identify", "send", "reply", "ack", "invite", "create", "revoke", "poll", "members"):
            self.assertEqual(self.raw(op)["code"], "forbidden")
        self.b.chat_readers = {}
        self.assertEqual(self.raw("chat_list")["code"], "forbidden")
        self.assertFalse(self.call("snapshot")["chat"]["enabled"])

    def test_open_exact_id_never_registers_or_dispatches_and_aliases_share(self):
        with self.b._connect() as db:
            before = {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for table in ("agents", "principals", "memberships", "messages", "outbound_queue")}
        chat = self.open()
        self.assertEqual(chat, self.open())
        self.assertEqual(chat, self.open("aadarsh"))
        self.assertNotEqual(chat, self.open(OTHER_READER))
        for target in ("Exact fixture", self.unpublished, "a_missing"):
            self.assertEqual(self.raw("chat_open", bus="qit-wilde", agent=target)["code"], "not_found")
        self.assertEqual(self.raw("chat_open", bus="general", agent=self.agent)["code"], "not_found")
        with self.b._connect() as db:
            self.assertEqual(before, {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in before})
        self.assertEqual(self.poll(), [])

    def test_export_includes_the_agents_human_chat_receipts(self):
        chat = self.open()
        message = self.send(chat, message="Fixture secret question")
        received = self.poll()[0]
        self.now += 1
        self.ack(received)
        self.now += 1
        answer = self.reply(received, "Fixture secret answer")
        export = self.admin("export", agent=self.agent)
        rows = {row["id"]: row for row in export["receipts"]}
        self.assertEqual(list(rows), [message["id"], answer["id"]])
        asked, answered = rows[message["id"]], rows[answer["id"]]
        self.assertEqual((asked["sender"], asked["target"], asked["bus"], asked["status"], asked["kind"], asked["conversation_id"]),
                         ("human.aadarwal", self.agent, "qit-wilde", "queued", "human", chat))
        self.assertEqual((answered["sender"], answered["target"], answered["status"], answered["in_reply_to"], answered["kind"]),
                         (self.agent, "human.aadarwal", "replied", message["id"], "human"))
        self.assertEqual(asked["updated_at"], self.now - 1)
        self.assertNotIn("secret", json.dumps(export))
        self.assertEqual(self.admin("export", agent=self.second)["receipts"], [])

    def test_human_attribution_exact_reply_and_inbox_not_directory(self):
        chat = self.open()
        message = self.send(chat, message="Literal $(no-shell)\n<script>fixture</script>")
        received = self.poll()[0]
        self.assertEqual(received["id"], message["id"])
        self.assertEqual(received["sender"], {"id": "human.aadarwal", "name": "aadarwal", "kind": "human",
                                            "user": "aadarwal", "device": "browser", "device_id": None})
        self.assertEqual(received["sender_type"], "human")
        self.assertEqual(received["reply_to"], message["id"])
        self.assertEqual(received["chat"], chat)
        self.assertEqual(received["conversation_expires_at"], self.now + MESSAGE_TTL)
        self.ack(received)
        answer = self.reply(received)
        rows = self.call("chat_messages", chat=chat)["messages"]
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["status"], "queued")
        self.assertEqual(rows[1]["id"], answer["id"])
        self.assertEqual(rows[1]["in_reply_to"], message["id"])
        self.assertEqual(rows[1]["status"], "replied")
        snap = self.call("snapshot", reader="aadarsh")
        for secret in ("Literal", "Fixture answer", "human.aadarwal", chat, message["id"]):
            self.assertNotIn(secret, json.dumps(snap))
        self.assertEqual(next(bus for bus in snap["buses"] if bus["name"] == "qit-wilde")["graph"]["edges"], [])
        self.assertEqual(self.admin("receipt", id=message["id"])["status"], "queued")

    def test_idempotency_is_durable_atomic_and_payload_bound(self):
        chat, request = self.open(), str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: self.call("chat_send", chat=chat, request_id=request, message="same"), range(10)))
        self.assertEqual(len({reply["message"]["id"] for reply in replies}), 1)
        self.assertEqual(sum(not reply["deduplicated"] for reply in replies), 1)
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        retry = self.call("chat_send", reader="aadarsh", chat=chat, request_id=request, message="same")
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(self.raw("chat_send", chat=chat, request_id=request, message="changed")["code"], "conflict")
        self.assertEqual(self.raw("chat_send", chat=self.open(agent=self.second), request_id=request, message="same")["code"], "conflict")
        self.assertEqual(len(self.call("chat_messages", chat=chat)["messages"]), 1)
        self.send(chat, message="same")
        self.assertEqual(len(self.call("chat_messages", chat=chat)["messages"]), 2)

    def test_own_inbox_only_including_same_names_and_forged_fields(self):
        chat = self.open()
        self.send(chat)
        self.assertEqual(self.call("chat_list", reader=OTHER_READER)["chats"], [])
        for op, extra in (("chat_messages", {}), ("chat_read", {"through": 1}),
                          ("chat_send", {"request_id": str(uuid.uuid4()), "message": "forgery"})):
            self.assertEqual(self.raw(op, reader=OTHER_READER, chat=chat, **extra)["code"], "not_found")
        self.assertEqual(self.raw("chat_send", chat=chat, request_id=str(uuid.uuid4()), message="forgery", sender="other")["code"], "invalid_request")
        self.assertEqual(self.raw("chat_list", identity="aadarwal")["code"], "invalid_request")

    def test_reply_requires_exact_recipient_fetched_original_and_fixed_target(self):
        sent = self.send()
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "reply", "sender": self.agent,
                                                          "id": sent["id"], "message": "before poll"})["ok"])
        message = self.poll()[0]
        for extra in ({"sender": self.second}, {"target": self.second}, {"bus": "general"}, {"chat": self.open()}, {"identity": "someone"}):
            request = {"op": "reply", "sender": self.agent, "id": message["id"], "message": "forgery", **extra}
            self.assertFalse(self.b.handle(self.b.admin_token, request)["ok"])
        invitation = self.admin("invite", bus="qit-wilde", user="peer")
        device = self.b.handle(None, {"op": "redeem", "invite": invitation["invite"], "device": "other"})
        self.assertTrue(device["ok"])
        self.assertEqual(self.b.handle(device["token"], {"op": "reply", "sender": self.agent, "id": message["id"], "message": "forgery"})["code"], "not_found")
        self.assertEqual(self.b.handle(device["token"], {"op": "receipt", "id": message["id"]})["code"], "not_found")
        self.reply(message)

    def test_shared_fifo_leases_restart_recovery_and_transport_status(self):
        first = self.admin("send", sender=self.second, target=self.agent, bus="qit-wilde", message="legacy first")
        chat_message = self.send()
        self.now -= 10
        third = self.admin("send", sender=self.second, target=self.agent, bus="qit-wilde", message="legacy third")
        self.assertEqual(self.poll()[0]["id"], first["id"])
        self.assertEqual(self.poll(), [])
        self.now += LEASE_TTL + 10
        self.ack(self.poll()[0])
        human = self.poll()[0]
        self.assertEqual(human["id"], chat_message["id"])
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        self.assertEqual(self.poll(), [])
        self.now += LEASE_TTL
        recovered = self.poll()[0]
        self.assertEqual(recovered["id"], human["id"])
        self.assertNotEqual(recovered["lease"], human["lease"])
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "ack", "agent": self.agent, "id": human["id"], "lease": human["lease"], "status": "queued"})["ok"])
        self.ack(recovered)
        self.ack(recovered)
        self.assertEqual(self.poll()[0]["id"], third["id"])

    def test_pagination_unread_and_only_fetched_rows_can_be_acknowledged(self):
        chat = self.open()
        self.send(chat)
        received = self.poll()[0]
        self.ack(received)
        self.reply(received, "first answer")
        self.reply(received, "second answer")
        self.assertEqual(self.call("chat_list")["chats"][0]["unread"], 2)
        self.assertEqual(self.raw("chat_read", chat=chat, through=3)["code"], "conflict")
        page = self.call("chat_messages", chat=chat, limit=2)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_after"], 2)
        self.assertEqual(self.call("chat_read", chat=chat, through=2)["unread"], 1)
        self.call("chat_messages", reader="aadarsh", chat=chat, after=2)
        self.assertEqual(self.raw("chat_read", chat=chat, through=3)["code"], "conflict")
        self.assertEqual(self.call("chat_read", reader="aadarsh", chat=chat, through=3)["unread"], 0)
        self.assertEqual(self.call("chat_list")["chats"][0]["unread"], 0)

    def test_scope_loss_hides_history_and_permanently_closes_old_windows(self):
        chat = self.open()
        self.send(chat)
        received = self.poll()[0]
        self.reply(received)
        self.send(chat, message="pending before loss")
        self.assertEqual(self.raw("chat_messages", chat=chat, view=self.view(buses=[]))["code"], "not_found")
        self.assertEqual(self.call("chat_list", view=self.view(buses=[]))["chats"], [])
        self.assertFalse(self.call("snapshot", view=self.view(buses=[]))["chat"]["enabled"])
        self.assertEqual(self.poll(), [])
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "reply", "sender": self.agent, "id": received["id"], "message": "late"})["ok"])
        self.assertEqual(self.open(), chat)
        history = self.call("chat_messages", chat=chat)["messages"]
        self.assertEqual(history[0]["status"], "cancelled")
        self.send(chat, message="new after readmission")
        latest = self.poll()[0]
        self.reply(latest)
        self.assertNotEqual(received["id"], latest["id"])

    def test_bootstrap_observes_revocation_without_followup_chat_request(self):
        self.send()
        received = self.poll()[0]
        self.b.browser_session(READER, HASH, self.view(buses=[]))
        self.b.browser_session(READER, HASH, self.view())
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "reply", "sender": self.agent,
                                                          "id": received["id"], "message": "late"})["ok"])

    def test_expired_pending_message_reports_expired(self):
        chat = self.open()
        message = self.send(chat)
        self.now += MESSAGE_TTL
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.admin("receipt", id=message["id"])["status"], "expired")
        self.assertEqual(self.call("chat_messages", chat=chat)["messages"][0]["status"], "expired")

    def test_read_tracking_rereads_do_not_consume_capacity_and_ack_reclaims(self):
        chat = self.open()
        request = str(uuid.uuid4())
        self.send(chat, request_id=request)
        received = self.poll()[0]
        self.ack(received)
        self.reply(received)
        self.call("chat_messages", chat=chat)
        with mock.patch.object(bus_broker, "MAX_CHAT_SEEN", 2):
            self.assertTrue(self.call("chat_messages", chat=chat)["messages"])
            self.assertTrue(self.call("chat_send", chat=chat, request_id=request, message="Fixture question")["deduplicated"])
            self.assertEqual(self.raw("chat_messages", chat=chat, reader="aadarsh")["code"], "limit")
            self.call("chat_read", chat=chat, through=2)
            self.assertTrue(self.call("chat_messages", chat=chat, reader="aadarsh")["messages"])
        with self.b._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM human_chat_seen").fetchone()[0], 0)

    def test_unicode_and_control_history_pages_fit_exact_http_budget(self):
        chat = self.open()
        sent = self.send(chat)
        received = self.poll()[0]
        content = ["é" * 16384 if n % 2 else "\x01" * 20000 for n in range(100)]
        for message in content:
            self.reply(received, message)
        server = BusHTTPServer(("127.0.0.1", 0), handler_factory(self.b))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            after, actual, first = 0, [], True
            while True:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("POST", "/v1", body=json.dumps({"op": "chat_messages", "chat": chat, "after": after, "limit": 100}),
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.tokens[READER],
                                      "X-Communicate-Bus-Gateway": SECRET, "X-Communicate-Bus-Reader": READER,
                                      "X-Communicate-Bus-Reader-Hash": HASH, "X-Communicate-Bus-View": json.dumps(self.view())})
                response = conn.getresponse()
                encoded = response.read()
                self.assertEqual(response.status, 200, encoded[:200])
                self.assertLessEqual(len(encoded), bus_broker.MAX_CHAT_PAGE_BYTES)
                page = json.loads(encoded)
                conn.close()
                self.assertTrue(page["messages"])
                self.assertEqual(page["next_after"], page["messages"][-1]["seq"])
                if first:
                    self.assertTrue(page["has_more"])
                    with self.b._connect() as db:
                        seen = [row[0] for row in db.execute("SELECT seq FROM human_chat_seen WHERE chat=? AND reader=? ORDER BY seq", (chat, READER))]
                    self.assertEqual(seen, [row["seq"] for row in page["messages"]])
                    self.assertEqual(self.raw("chat_read", chat=chat, through=page["next_after"] + 1)["code"], "conflict")
                    first = False
                actual.extend(page["messages"])
                self.assertGreater(page["next_after"], after)
                after = page["next_after"]
                if not page["has_more"]:
                    break
            self.assertEqual([row["seq"] for row in actual], list(range(1, 102)))
            self.assertEqual(actual[0]["id"], sent["id"])
            self.assertEqual([row["content"] for row in actual[1:]], content)
            self.assertEqual(self.call("chat_read", chat=chat, through=101)["unread"], 0)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_openwebui_targets_are_explicit_deduplicated_and_permission_scoped(self):
        self.assertEqual(self.call("snapshot")["chat"]["openwebui"], [])
        self.admin("register", bus="general", session_key="fixture-target", name="Exact fixture", kind="codex")
        targets = [{"bus": "qit-wilde", "agent": self.agent}, {"bus": "general", "agent": self.agent},
                   {"bus": "qit-wilde", "agent": self.agent}, {"bus": "qit-wilde", "agent": self.unpublished},
                   {"bus": "qit-wilde", "agent": "a_" + "f" * 32}]
        with mock.patch.dict(os.environ, {"BUS_CHAT_OPENWEBUI_TARGETS": json.dumps(targets)}):
            self.b = Broker(self.tmp.name, clock=lambda: self.now)
        self.assertEqual(self.call("snapshot")["chat"]["openwebui"], [targets[0]])
        self.assertEqual(self.call("snapshot", reader="aadarsh")["chat"]["openwebui"], [targets[1], targets[0]])
        self.assertEqual(self.call("snapshot", reader="peer")["chat"]["openwebui"], [])
        self.assertEqual(self.admin("snapshot")["chat"]["openwebui"], [])
        self.assertEqual(self.call("snapshot", view=self.view(buses=[]))["chat"]["openwebui"], [])
        self.admin("leave", bus="qit-wilde", agent=self.agent)
        self.assertEqual(self.call("snapshot")["chat"]["openwebui"], [])
        invalid = ["{}", "null", '[{"bus":"general","agent":"Exact fixture"}]',
                   '[{"bus":"*","agent":"a_' + 'a' * 32 + '"}]',
                   '[{"bus":"general","agent":"a_' + 'a' * 32 + '","enabled":true}]',
                   json.dumps([targets[0]] * (bus_broker.MAX_CHAT_OPENWEBUI_TARGETS + 1))]
        for setting in invalid:
            with self.subTest(setting=setting[:100]), mock.patch.dict(os.environ, {"BUS_CHAT_OPENWEBUI_TARGETS": setting}), self.assertRaises(ValueError):
                Broker(self.tmp.name, clock=lambda: self.now)

    def test_separate_brokers_serialize_send_and_preserve_queue_order(self):
        chat, request = self.open(), str(uuid.uuid4())
        brokers = [Broker(self.tmp.name, clock=lambda: self.now) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda broker: broker.handle(self.tokens[READER],
                {"op": "chat_send", "chat": chat, "request_id": request, "message": "concurrent"},
                reader=READER, reader_hash=HASH, view=self.view()), brokers))
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(sum(not result["deduplicated"] for result in results), 1)
        self.admin("send", sender=self.second, target=self.agent, bus="qit-wilde", message="following legacy")
        with self.b._connect() as db:
            ordering = list(map(tuple, db.execute("SELECT ordering,message FROM outbound_queue ORDER BY ordering")))
        self.b = Broker(self.tmp.name, clock=lambda: self.now)
        with self.b._connect() as db:
            self.assertEqual(ordering, list(map(tuple, db.execute("SELECT ordering,message FROM outbound_queue ORDER BY ordering"))))
        self.assertEqual(self.poll()[0]["id"], results[0]["message"]["id"])

    def test_missing_expired_and_forged_view_never_reads_or_sends(self):
        chat = self.open()
        for op in ("chat_messages", "chat_send", "chat_read", "chat_list"):
            self.assertEqual(self.b.handle(self.tokens[READER], {"op": op, "chat": chat}, reader=READER, reader_hash=HASH)["code"], "unauthorized")
            self.assertEqual(self.raw(op, chat=chat, view=self.view(exp=self.now))["code"], "unauthorized")
            self.assertEqual(self.raw(op, chat=chat, view=self.view(reader=OTHER_READER))["code"], "unauthorized")

    def test_unpublish_and_revoke_hide_history_and_never_reactivate_old_reply(self):
        chat = self.open()
        self.send(chat)
        received = self.poll()[0]
        self.admin("leave", agent=self.agent, bus="qit-wilde")
        self.assertEqual(self.call("chat_list")["chats"], [])
        self.assertEqual(self.raw("chat_messages", chat=chat)["code"], "not_found")
        self.admin("register", bus="qit-wilde", session_key="fixture-target", name="Exact fixture", kind="codex")
        self.assertEqual(self.open(), chat)
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "reply", "sender": self.agent, "id": received["id"], "message": "late"})["ok"])
        self.send(chat)
        self.assertEqual(len(self.poll()), 1)

    def test_config_revocation_and_remapping_close_existing_sends(self):
        chat = self.open()
        self.send(chat)
        received = self.poll()[0]
        self.b.chat_readers[READER] = {"identity": "renamed-owner", "buses": ["qit-wilde"]}
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "reply", "sender": self.agent, "id": received["id"], "message": "late"})["ok"])
        self.b.chat_readers[READER] = self.grants[READER]
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "reply", "sender": self.agent, "id": received["id"], "message": "still late"})["ok"])

    def test_reply_window_24h_history_30d_sequence_never_reused(self):
        chat = self.open()
        old = self.send(chat)
        received = self.poll()[0]
        self.ack(received)
        self.reply(received)
        self.now += MESSAGE_TTL
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "reply", "sender": self.agent, "id": received["id"], "message": "expired"})["ok"])
        self.assertEqual(len(self.call("chat_messages", chat=chat)["messages"]), 2)
        self.now += CHAT_RETENTION - MESSAGE_TTL - 1
        newer = self.send(chat)
        self.now += 2
        rows = self.call("chat_messages", chat=chat)["messages"]
        self.assertEqual([row["id"] for row in rows], [newer["id"]])
        self.assertGreater(newer["seq"], old["seq"])
        self.now += CHAT_RETENTION
        self.assertEqual(self.call("chat_list")["chats"], [])

    def test_content_pagination_request_validation_and_bounded_storage(self):
        chat = self.open()
        for message in ("", " ", None, 12, "x\0y", "x" * (bus_broker.MAX_MESSAGE + 1)):
            self.assertFalse(self.raw("chat_send", chat=chat, message=message, request_id=str(uuid.uuid4()))["ok"])
        for request in ("x", str(uuid.uuid4()).upper(), 12, None):
            self.assertFalse(self.raw("chat_send", chat=chat, message="test", request_id=request)["ok"])
        for field, values in (("after", [-1, True, "0", 2**63]), ("limit", [0, 101, True, "2"])):
            for value in values:
                self.assertFalse(self.raw("chat_messages", chat=chat, **{field: value})["ok"])
        with mock.patch.object(bus_broker, "MAX_PENDING", 1):
            self.send(chat)
            self.assertEqual(self.raw("chat_send", chat=chat, message="full", request_id=str(uuid.uuid4()))["code"], "limit")
            self.assertEqual(self.b.handle(self.b.admin_token, {"op": "send", "sender": self.second, "target": self.agent,
                                                               "bus": "qit-wilde", "message": "legacy full"})["code"], "limit")
        with mock.patch.object(bus_broker, "MAX_CHAT_MESSAGES_PER_CHAT", 1):
            self.assertEqual(self.raw("chat_send", chat=chat, message="full", request_id=str(uuid.uuid4()))["code"], "limit")
        with mock.patch.object(bus_broker, "MAX_CHATS_PER_IDENTITY", 1):
            self.assertEqual(self.raw("chat_open", agent=self.second, bus="qit-wilde")["code"], "limit")

    def test_http_bridge_only_trusted_context_and_chat_operations(self):
        server = BusHTTPServer(("127.0.0.1", 0), handler_factory(self.b))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(body=None, extra=None, omit=(), path="/_bus/chat"):
            headers = {"Content-Type": "application/json", "X-Communicate-Bus-Gateway": SECRET,
                       "X-Communicate-Bus-Reader": READER, "X-Communicate-Bus-Reader-Hash": HASH,
                       "X-Communicate-Bus-View": json.dumps(self.view())}
            headers.update(extra or {})
            for name in omit:
                headers.pop(name, None)
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("POST", path, body=body if isinstance(body, str) else json.dumps(body or {"op": "chat_list"}), headers=headers)
            response = conn.getresponse()
            status, result = response.status, json.loads(response.read())
            conn.close()
            return status, result
        try:
            self.assertEqual(post()[0], 200)
            self.assertEqual(post({"op": "chat_open", "bus": "qit-wilde", "agent": self.agent})[0], 200)
            for field in ("Authorization", "Cookie"):
                self.assertEqual(post(extra={field: "forbidden"})[0], 403)
            for field in ("X-Communicate-Bus-Gateway", "X-Communicate-Bus-Reader", "X-Communicate-Bus-Reader-Hash", "X-Communicate-Bus-View"):
                self.assertEqual(post(omit=[field])[0], 401)
            self.assertEqual(post({"op": "snapshot"})[0], 403)
            self.assertEqual(post('{"op":"chat_list","op":"chat_open"}')[0], 400)
            self.assertEqual(post(extra={"Origin": "https://attacker.example"})[0], 403)
            self.assertEqual(post(path="/_bus/chat?bypass=1")[0], 404)
            self.b.gateway_shared_secret = None
            self.assertEqual(post()[0], 404)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_config_is_strict_and_default_disabled(self):
        invalid = ["[]", "null", '{"a":{"identity":"a","buses":["general","general"]}}',
                   '{"a":{"identity":"a","buses":["*"]}}', '{"a":{"identity":"a","buses":"general"}}',
                   '{"a":{"identity":"a","buses":[],"admin":true}}', '{"a":{"identity":"bad user","buses":[]}}',
                   '{"a":{"identity":"a","buses":[]},"a":{"identity":"b","buses":[]}}']
        for config in invalid:
            with self.subTest(config=config), mock.patch.dict(os.environ, {"BUS_CHAT_READERS": config}), self.assertRaises(ValueError):
                Broker(self.tmp.name, clock=lambda: self.now)
        with mock.patch.dict(os.environ, {"BUS_CHAT_READERS": "{}"}):
            self.b = Broker(self.tmp.name, clock=lambda: self.now)
            self.assertEqual(self.raw("chat_list")["code"], "forbidden")


if __name__ == "__main__":
    unittest.main(verbosity=2)
