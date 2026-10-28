#!/usr/bin/env python3
"""Service epoch/CAS boundaries against a real, temporary in-process broker."""
import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import bus_broker as module
from bus_broker import Broker, SERVICE_FENCE_PROTOCOL as P


class ServiceEpochTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="service-epoch-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.b = Broker(self.root / "hub")
        self.token = self.device("owner")

    def device(self, label):
        invite = self.b.handle(self.b.admin_token, {"op": "invite", "bus": "general"})
        return self.b.handle("", {"op": "redeem", "invite": invite["invite"], "device": label})["token"]

    def call(self, op, *, token=None, ok=True, **payload):
        result = self.b.handle(token or self.token, {"op": op, **payload})
        self.assertEqual(result["ok"], ok, result)
        return result

    def actor(self, name, kind="service", token=None):
        sid = str(uuid.uuid4())
        request = {"session_key": ("service:" if kind == "service" else "native:") + sid,
                   "name": name, "kind": kind, "bus": "general", "status": "queueable"}
        actor = self.call("register", token=token, **request)
        return {**actor, "service_id": sid, "request": request}

    def retire(self, actor, key=None, **changes):
        return self.call("service_fence_retire", **{"agent": actor["id"], "service_id": actor["service_id"],
            "expected_epoch": actor["epoch"], "request_id": key or uuid.uuid4().hex, **changes})

    def reactivate(self, actor, receipt, key=None, **changes):
        result = self.call("register", **{**actor["request"], "agent": actor["id"],
            "expected_epoch": receipt["epoch"], "request_id": key or uuid.uuid4().hex,
            "service_config_hash": "a" * 64, **changes})
        return {**actor, **result}

    def fenced(self, name):
        old = self.actor(name)
        return self.reactivate(old, self.retire(old))

    def send(self, source, target, key=None, **changes):
        args = {"sender": source["id"], "target": target["id"], "bus": "general", "message": "fixture"}
        if source.get("fence_protocol") == P:
            args["expected_sender_epoch"] = source["epoch"]
        if target.get("fence_protocol") == P:
            args["expected_recipient_epoch"] = target["epoch"]
        if source.get("fence_protocol") == P or target.get("fence_protocol") == P:
            args["request_id"] = key or uuid.uuid4().hex
        return self.call("send", **{**args, **changes})

    def count(self, table):
        with closing(self.b._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_exact_known_service_only_and_fence_capacity_never_evicts(self):
        native = self.actor("native", "codex")
        self.call("service_fence_status", agent=native["id"], ok=False)
        a, b = self.actor("a"), self.actor("b")
        foreign = self.device("foreign")
        self.call("service_fence_status", agent=a["id"], token=foreign, ok=False)
        self.call("service_fence_status", agent="a_" + "0" * 32, ok=False)
        with mock.patch.object(module, "MAX_RETAINED_AGENTS", 1):
            first = self.retire(a)
            self.call("service_fence_retire", agent=b["id"], service_id=b["service_id"], expected_epoch=0,
                      request_id="overflow", ok=False)
        self.assertEqual(self.count("service_fences"), 1)
        self.assertTrue(self.call("service_fence_status", agent=a["id"])["enabled"])
        self.assertEqual(first["protocol"], P)

    def test_response_loss_retire_and_register_have_one_epoch_effect(self):
        a = self.actor("a")
        one = self.retire(a, "retire-same")
        self.assertEqual(one, self.retire(a, "retire-same"))
        a2 = self.reactivate(a, one, "register-same")
        self.assertEqual(a2, self.reactivate(a, one, "register-same"))
        self.assertEqual(a2["epoch"], 2)
        historical = self.retire(a, "retire-same")
        self.assertTrue(historical["historical_replay"])
        status = self.call("service_fence_status", agent=a["id"])
        self.assertEqual((status["epoch"], status["retired"]), (2, False))
        self.call("register", **{**a["request"], "agent": a["id"], "expected_epoch": 1,
                  "request_id": "register-same", "service_config_hash": "a" * 64, "name": "changed"}, ok=False)
        self.call("service_fence_retire", agent=a["id"], service_id=a["service_id"],
                  expected_epoch=2, request_id="retire-same", ok=False)

    def test_delayed_register_identify_retire_leave_send_never_mutate_new_generation(self):
        old = self.actor("old")
        current = self.reactivate(old, self.retire(old))
        target = self.actor("target", "codex")
        for op, payload in [("register", old["request"]), ("identify", old["request"]),
                ("retire", {"agent": old["id"]}), ("leave", {"agent": old["id"], "bus": "general"}),
                ("send", {"sender": old["id"], "target": target["id"], "message": "old"})]:
            with self.subTest(op=op):
                self.call(op, **payload, ok=False)
        for op in ("register", "identify"):
            self.call(op, **{**old["request"], "agent": old["id"], "expected_epoch": 0,
                            "request_id": "stale-" + op, "service_config_hash": "a" * 64}, ok=False)
        self.call("send", sender=old["id"], target=target["id"], message="old CAS",
                  expected_sender_epoch=0, request_id="old-wire", ok=False)
        self.call("service_fence_retire", agent=old["id"], service_id=old["service_id"],
                  expected_epoch=0, request_id="old-retire", ok=False)
        status = self.call("service_fence_status", agent=current["id"])
        self.assertEqual((status["epoch"], status["retired"]), (2, False))
        self.assertEqual(self.count("messages"), 0)

    def test_native_to_fenced_receiver_requires_captured_epoch_and_one_effect(self):
        source, target = self.actor("source", "codex"), self.fenced("target")
        base = {"sender": source["id"], "target": target["id"], "message": "old", "request_id": "same"}
        self.call("send", **base, ok=False)
        self.call("send", **base, expected_recipient_epoch=0, ok=False)
        first = self.call("send", **base, expected_recipient_epoch=target["epoch"])
        self.assertEqual(first, self.call("send", **base, expected_recipient_epoch=target["epoch"]))
        self.assertEqual(self.count("messages"), 1)
        self.call("send", **{**base, "message": "different"}, expected_recipient_epoch=target["epoch"], ok=False)

    def test_delayed_alias_cannot_retarget_another_opted_actor_at_the_same_epoch(self):
        source, first = self.actor("source", "codex"), self.fenced("alias")
        delayed = {"sender": source["id"], "target": "alias", "message": "held",
                   "expected_recipient_epoch": first["epoch"], "request_id": "held-alias"}
        self.call("register", **{**first["request"], "agent": first["id"], "expected_epoch": first["epoch"],
                  "request_id": "rename", "service_config_hash": "a" * 64, "name": "other"})
        second = self.fenced("alias")
        self.assertEqual(first["epoch"], second["epoch"])
        self.call("send", **delayed, ok=False)
        self.assertEqual(self.count("messages"), 0)
        self.send(source, second, "exact-id")

    def test_active_generation_keys_survive_age_and_overflow_without_replay(self):
        source, target = self.actor("source", "codex"), self.fenced("target")
        with mock.patch.object(module, "MAX_SERVICE_REQUESTS_PER_GENERATION", 3):
            first = self.send(source, target, "lost-response")
            with closing(self.b._connect()) as db:
                db.execute("UPDATE service_requests SET created_at=0")
                db.commit()
            self.send(source, target, "fill")
            self.call("send", sender=source["id"], target=target["id"], message="new",
                      expected_recipient_epoch=target["epoch"], request_id="overflow", ok=False)
            self.assertEqual(first, self.send(source, target, "lost-response"))
        self.assertEqual(self.count("messages"), 2)

    def test_global_key_overflow_retains_active_records_and_fails_before_effect(self):
        source, target = self.actor("source", "codex"), self.fenced("target")
        first = self.send(source, target, "first")
        with mock.patch.object(module, "MAX_SERVICE_REQUESTS", 3):
            self.send(source, target, "second")  # May GC only the retired epoch1 key.
            before = self.count("messages")
            self.call("send", sender=source["id"], target=target["id"], message="overflow",
                      expected_recipient_epoch=target["epoch"], request_id="third", ok=False)
            self.assertEqual(self.count("messages"), before)
            self.assertEqual(first, self.send(source, target, "first"))

    def test_active_configuration_cannot_be_retargeted_and_unknown_id_cannot_allocate(self):
        target = self.fenced("target")
        self.call("register", **{**target["request"], "agent": target["id"], "expected_epoch": target["epoch"],
                  "request_id": "retarget", "service_config_hash": "b" * 64}, ok=False)
        before = self.count("agents")
        self.call("register", session_key="service:" + str(uuid.uuid4()), name="unknown", kind="service",
                  agent="a_" + "0" * 32, expected_epoch=0, request_id="guess", service_config_hash="a" * 64, ok=False)
        self.assertEqual(self.count("agents"), before)

    def test_fence_survives_restart_and_agent_row_removal(self):
        a = self.actor("a")
        self.retire(a)
        with closing(self.b._connect()) as db:
            db.execute("DELETE FROM agents WHERE id=?", (a["id"],))
            db.commit()
        self.b = Broker(self.root / "hub")
        self.call("register", **a["request"], ok=False)
        self.call("register", **{**a["request"], "agent": a["id"], "expected_epoch": 1,
                                  "request_id": "new"}, ok=False)
        self.assertEqual(self.count("service_fences"), 1)

    def test_legacy_mixed_poll_and_heartbeat_skip_only_fenced_actors(self):
        sender, native, service = self.actor("sender", "codex"), self.actor("native", "claude"), self.fenced("service")
        normal, protected = self.send(sender, native), self.send(sender, service)
        old = self.call("poll_device", agents=[native["id"], service["id"]])
        self.assertEqual([x["id"] for x in old["messages"]], [normal["id"]])
        self.assertEqual(old["refusals"], [{"agent": service["id"], "code": "service_epoch_required"}])
        heartbeat = self.call("heartbeat", agents=[{"id": native["id"], "status": "offline"},
                              {"id": service["id"], "status": "offline"}])
        self.assertEqual(heartbeat["updated"], 1)
        self.assertEqual(heartbeat["refusals"][0]["agent"], service["id"])
        current = self.call("poll_device", agents=[service["id"]], service_epochs={service["id"]: service["epoch"]})
        env = current["messages"][0]
        self.assertEqual((env["id"], env["protocol"], env["recipient_epoch"], env["sender"]["epoch"]),
                         (protected["id"], P, service["epoch"], None))

    def test_poll_selector_ownership_and_shape_validate_before_any_lease(self):
        sender, native, service = self.actor("sender", "codex"), self.actor("native", "claude"), self.fenced("service")
        mid = self.send(sender, native)["id"]
        for epochs in ({service["id"]: True}, {"a_" + "0" * 32: 1}, [], {service["id"]: -1}):
            self.call("poll_device", agents=[native["id"], service["id"]], service_epochs=epochs, ok=False)
        env = self.call("poll_device", agents=[native["id"]])["messages"][0]
        self.assertEqual(env["id"], mid)

    def test_duplicate_poll_ids_reject_before_native_or_service_lease(self):
        sender, native, service = self.actor("sender", "codex"), self.actor("native", "claude"), self.fenced("service")
        normal, protected = self.send(sender, native), self.send(sender, service)
        for ids in ([native["id"], native["id"]], [service["id"], native["id"], service["id"]]):
            self.call("poll_device", agents=ids, service_epochs={service["id"]: service["epoch"]}
                      if service["id"] in ids else {}, ok=False)
        with closing(self.b._connect()) as db:
            self.assertEqual({r["status"] for r in db.execute("SELECT status FROM messages")}, {"accepted"})
        messages = self.call("poll_device", agents=[native["id"], service["id"]],
                             service_epochs={service["id"]: service["epoch"]})["messages"]
        self.assertEqual({r["id"] for r in messages}, {normal["id"], protected["id"]})

    def test_fenced_sender_to_unopted_service_preserves_unknown_receiver_epoch(self):
        import bus_service
        source, target = self.fenced("source"), self.actor("legacy-target")
        self.send(source, target)
        env = self.call("poll", agent=target["id"])["messages"][0]
        self.assertEqual((env["recipient_epoch"], env["sender"]["epoch"]), (None, source["epoch"]))
        root = self.root / "legacy-journal"
        root.mkdir(mode=0o700)
        record = {"id": target["id"], "service_id": target["service_id"], "url": "https://fixture.invalid",
                  "kind": "service", "buses": ["general"], "reply_until": 0, "epoch": target["epoch"]}
        self.assertEqual(bus_service.enqueue(root, record, env)[0], "queued")
        with closing(bus_service.journal(root)) as db:
            stored = json.loads(db.execute("SELECT envelope FROM jobs").fetchone()[0])
        self.assertIsNone(stored["recipient_epoch"])
        self.assertEqual(stored["sender"]["epoch"], source["epoch"])

    def test_epoch_metadata_composes_with_v2_parent_identity_and_acknowledgment(self):
        import bus_service
        source, target = self.fenced("source"), self.actor("v2-target")
        sent = self.send(source, target)
        env = self.call("poll", agent=target["id"])["messages"][0]
        self.assertEqual((env["correlation_version"], env["conversation_id"], env["in_reply_to"]),
                         (1, sent["conversation_id"], None))
        root = self.root / "v2-journal"
        root.mkdir(mode=0o700)
        record = {"id": target["id"], "service_id": target["service_id"], "url": "https://fixture.invalid",
                  "kind": "service", "buses": ["general"], "contract_version": 2}
        bus_service.enqueue(root, record, env)
        # A queued job's protocol cannot change with later registration defaults.
        bus_service.enqueue(root, {**record, "contract_version": 1}, env)
        bus_service.advance(root, {"v2": record}, {record["url"]: {}},
            lambda *a, **kw: self.fail("terminal app acknowledgment must not send another reply"),
            call_bridge=lambda *a: {"pending": False, "disposition": "acknowledge"})
        with closing(bus_service.journal(root)) as db:
            row = db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual((row["contract_version"], row["state"]), (2, "acknowledged"))
        self.assertEqual(json.loads(row["envelope"])["conversation_id"], sent["conversation_id"])
        self.assertEqual(self.count("messages"), 1)

    def test_old_ack_and_reply_after_reactivation_are_fenced(self):
        sender, target = self.actor("sender", "codex"), self.fenced("target")
        self.send(sender, target)
        env = self.call("poll", agent=target["id"], expected_epoch=target["epoch"])["messages"][0]
        old = dict(target)
        target = self.reactivate(target, self.retire(target))
        self.call("ack", agent=old["id"], id=env["id"], lease=env["lease"], status="queued", expected_epoch=old["epoch"], ok=False)
        self.call("reply", sender=old["id"], id=env["id"], message="late", expected_sender_epoch=old["epoch"], request_id="late", ok=False)
        self.assertEqual(self.call("poll", agent=target["id"], expected_epoch=target["epoch"])["messages"], [])

    def test_reply_uses_original_receiver_epoch_and_deduplicates(self):
        service, native = self.fenced("service"), self.actor("native", "codex")
        self.send(service, native)
        env = self.call("poll", agent=native["id"])["messages"][0]
        self.call("ack", agent=native["id"], id=env["id"], lease=env["lease"], status="queued")
        reply = {"sender": native["id"], "id": env["id"], "message": "reply", "request_id": "reply-same"}
        first = self.call("reply", **reply)
        self.assertEqual(first, self.call("reply", **reply))
        service = self.reactivate(service, self.retire(service))
        self.call("reply", **{**reply, "request_id": "late-new-key"}, ok=False)
        self.assertEqual(self.count("messages"), 2)

    def test_simultaneous_reactivation_same_key_has_one_transition(self):
        actor = self.actor("actor")
        receipt = self.retire(actor)
        barrier = threading.Barrier(2)
        def run():
            barrier.wait(timeout=3)
            return self.reactivate(actor, receipt, "race-same")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=5) for f in [pool.submit(run), pool.submit(run)]]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["epoch"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
