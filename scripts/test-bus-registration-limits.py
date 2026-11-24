#!/usr/bin/env python3
"""Owner quota policy against temporary in-process brokers; no live effects."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import bus_broker as module
from bus_broker import Broker


class RegistrationLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bus-owner-limits-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        patch = mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                                            "USER": "fixture", "LOGNAME": "fixture"}, clear=True)
        patch.start(); self.addCleanup(patch.stop)
        self.serial = 0

    def limits(self, **changes):
        return {"active_per_principal": 2, "active_total": 4,
                "retained_per_principal": 4, "retained_total": 8, **changes}

    def broker(self, policy=None, state=None):
        self.serial += 1
        state = state or self.root / ("broker-" + str(self.serial))
        with mock.patch.dict(os.environ, {} if policy is None else {"BUS_REGISTRATION_LIMITS": json.dumps(policy)}):
            return Broker(state, clock=lambda: 1800000000.0)

    def call(self, broker, op, token=None, ok=True, **fields):
        result = broker.handle(broker.admin_token if token is None else token, {"op": op, **fields})
        self.assertIs(result["ok"], ok, result)
        return result

    def device(self, broker, name):
        invite = self.call(broker, "invite", bus="general")["invite"]
        return self.call(broker, "redeem", token="", invite=invite, device=name)["token"]

    def register(self, broker, name, token=None, ok=True, **fields):
        return self.call(broker, "register", token=token, ok=ok,
                         session_key=name, name=name, bus="general", kind="codex", **fields)

    def test_defaults_and_owner_policy_are_captured_without_request_overrides(self):
        old = self.broker()
        self.assertEqual({key: old._registration_limit(key) for key in module.REGISTRATION_LIMIT_CEILINGS},
                         {"active_per_principal": 128, "active_total": 4096,
                          "retained_per_principal": 1024, "retained_total": 32768})
        policy = self.limits()
        broker = self.broker(policy)
        with mock.patch.dict(os.environ, {"BUS_REGISTRATION_LIMITS": json.dumps(dict(module.REGISTRATION_LIMIT_CEILINGS))}):
            self.assertEqual(broker.registration_limits, policy)
            self.register(broker, "one")
            self.register(broker, "two")
            refused = self.register(broker, "three", ok=False,
                registration_limits=dict(module.REGISTRATION_LIMIT_CEILINGS),
                BUS_REGISTRATION_LIMITS=dict(module.REGISTRATION_LIMIT_CEILINGS))
        self.assertEqual((refused["code"], refused["error"]), ("limit", "agent registration limit reached"))

    def test_invalid_policy_fails_before_creating_state_or_credentials(self):
        valid = self.limits()
        bad = ["", "null", "[]", "{}", "not-json", "[" * 1000,
               json.dumps({**valid, "unknown": 1}), json.dumps({key: value for key, value in valid.items() if key != "active_total"}),
               '{"active_per_principal":2,' + json.dumps(valid)[1:]]
        for value in (True, False, None, "2", 2.0, 0, -1):
            bad.append(json.dumps({**valid, "active_per_principal": value}))
        for key, maximum in module.REGISTRATION_LIMIT_CEILINGS.items():
            bad.append(json.dumps({**valid, key: maximum + 1}))
        bad += [json.dumps(self.limits(active_per_principal=5)),
                json.dumps(self.limits(retained_per_principal=9)),
                json.dumps(self.limits(retained_per_principal=1)),
                json.dumps(self.limits(retained_total=3)), " " * 2049]
        # Rows that one rule alone refuses: a device's active bound above the hub's, the hub's
        # active bound above what it retains, and a valid policy that is one byte too long.
        padded = json.dumps(valid) + " " * (2049 - len(json.dumps(valid)))
        bad += [json.dumps({"active_per_principal": 5, "active_total": 4,
                            "retained_per_principal": 8, "retained_total": 8}),
                json.dumps({"active_per_principal": 2, "active_total": 9,
                            "retained_per_principal": 4, "retained_total": 8}),
                padded]
        self.assertEqual(module._registration_policy(padded[:-1]), valid)
        for index, raw in enumerate(bad):
            with self.subTest(index=index):
                path = self.root / ("invalid-" + str(index))
                with mock.patch.dict(os.environ, {"BUS_REGISTRATION_LIMITS": raw}), self.assertRaises(ValueError):
                    Broker(path)
                self.assertFalse(path.exists())
        for raw in (17, {}, "\ud800"):
            with self.subTest(raw_type=type(raw).__name__), self.assertRaises(ValueError):
                module._registration_policy(raw)

    def test_owner_can_raise_above_128_and_refresh_poll_and_heartbeat_all_ids(self):
        broker = self.broker(self.limits(active_per_principal=130, active_total=140,
                                         retained_per_principal=140, retained_total=160))
        actors = [self.register(broker, "actor-" + str(i))["id"] for i in range(130)]
        self.assertEqual(len(set(actors)), 130)
        self.assertEqual(self.call(broker, "poll_device", agents=actors)["messages"], [])
        self.assertEqual(self.call(broker, "heartbeat", agents=[{"id": actor, "status": "queueable"} for actor in actors])["updated"], 130)
        self.assertEqual(self.register(broker, "actor-0")["id"], actors[0])
        refused = self.register(broker, "overflow", ok=False)
        self.assertEqual(refused["code"], "limit")
        self.call(broker, "poll_device", agents=actors + [actors[0]], ok=False)
        self.call(broker, "heartbeat", agents=[{"id": actors[0], "status": "queueable"}] * 131, ok=False)
        # Removing the override does not evict rows, or permit new allocation
        # above the restored default. Existing exact IDs still refresh.
        reopened = self.broker(state=broker.root)
        self.assertEqual(self.register(reopened, "actor-0")["id"], actors[0])
        self.assertEqual(self.register(reopened, "default-overflow", ok=False)["code"], "limit")

    def test_a_bound_below_128_still_takes_the_lists_a_client_sends(self):
        # A client sends up to 128 registrations in one heartbeat or device poll, whatever the
        # hub's policy is. A device registered under a higher bound must keep heartbeat and
        # delivery for all of them when the owner lowers the bound below what it holds.
        default = self.broker()
        actors = [self.register(default, "actor-" + str(i))["id"] for i in range(100)]
        beats = [{"id": actor, "status": "queueable"} for actor in actors]
        selectors = {actor: 0 for actor in actors}
        self.assertEqual(self.call(default, "poll_device", agents=actors, service_epochs=selectors)["messages"], [])
        lowered = self.broker(self.limits(active_per_principal=64, active_total=4096,
                                          retained_per_principal=1024, retained_total=32768), state=default.root)
        self.assertEqual(self.call(lowered, "heartbeat", agents=beats)["updated"], 100)
        self.assertEqual(self.call(lowered, "poll_device", agents=actors)["messages"], [])
        self.assertEqual(self.call(lowered, "poll_device", agents=actors, service_epochs=selectors)["messages"], [])
        # The lowered bound still governs allocation: existing ids refresh, a new one is refused.
        self.assertEqual(self.register(lowered, "actor-0")["id"], actors[0])
        self.assertEqual(self.register(lowered, "one-more", ok=False)["code"], "limit")
        # And a list longer than any client sends is still refused, as without a policy.
        for broker in (default, lowered):
            self.assertEqual(self.call(broker, "heartbeat", agents=beats + beats[:29], ok=False)["error"],
                             "agents must be a bounded list")
            self.assertEqual(self.call(broker, "poll_device", agents=actors + actors[:29], ok=False)["error"],
                             "agents must be a nonempty bounded list of agent ids")

    def test_active_global_retirement_leave_and_reactivation_keep_existing_semantics(self):
        broker = self.broker(self.limits(active_per_principal=3, active_total=3))
        alice, bob = self.device(broker, "alice"), self.device(broker, "bob")
        first = self.register(broker, "a1", alice)["id"]
        self.register(broker, "a2", alice)
        other = self.register(broker, "b1", bob)["id"]
        self.call(broker, "leave", token=alice, agent=first, bus="general")
        self.assertEqual(self.register(broker, "b2", bob, ok=False)["code"], "limit")
        self.call(broker, "retire", token=alice, agent=first)
        self.register(broker, "b2", bob)
        self.assertEqual(self.register(broker, "a1", alice, ok=False)["code"], "limit")
        self.call(broker, "retire", token=bob, agent=other)
        self.assertEqual(self.register(broker, "a1", alice)["id"], first)

    def test_retained_principal_and_global_caps_keep_history_and_allow_same_id(self):
        broker = self.broker(self.limits(active_per_principal=1, active_total=2,
                                         retained_per_principal=2, retained_total=4))
        alice, bob, carol = [self.device(broker, name) for name in ("alice", "bob", "carol")]
        ids = []
        for token, prefix in ((alice, "a"), (bob, "b")):
            for i in range(2):
                actor = self.register(broker, prefix + str(i), token)["id"]
                ids.append(actor)
                self.call(broker, "retire", token=token, agent=actor)
        self.assertEqual(self.register(broker, "new", carol, ok=False)["code"], "limit")
        self.assertEqual(self.register(broker, "a0", alice)["id"], ids[0])
        self.assertEqual(self.register(broker, "a-new", alice, ok=False)["code"], "limit")
        with broker._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agents").fetchone()[0], 4)

    def test_agent_rows_obey_the_owner_retained_per_device_cap(self):
        # Only this bound binds: both rows are retired, so the device has no active
        # registration, and the hub's totals are far from full.
        broker = self.broker(self.limits(active_per_principal=2, active_total=4,
                                         retained_per_principal=2, retained_total=8))
        alice = self.device(broker, "alice")
        for i in range(2):
            actor = self.register(broker, "a" + str(i), alice)["id"]
            self.call(broker, "retire", token=alice, agent=actor)
        self.assertEqual(self.register(broker, "a2", alice, ok=False)["code"], "limit")

    def test_fences_obey_the_owner_retained_total_across_devices(self):
        # Three devices with one fenced retirement each: no device nears its own bound of
        # two, so the third is refused by the hub's total alone.
        broker = self.broker(self.limits(active_per_principal=1, active_total=2,
                                         retained_per_principal=2, retained_total=2))
        for index, name in enumerate(("alice", "bob", "carol")):
            token, service_id = self.device(broker, name), str(uuid.uuid4())
            actor = self.call(broker, "register", token=token, session_key="service:" + service_id,
                              name="service-" + name, kind="service", bus="general")["id"]
            retired = self.call(broker, "service_fence_retire", token=token, agent=actor, service_id=service_id,
                                expected_epoch=0, request_id="retire-" + name, ok=index < 2)
            if index == 2:
                self.assertEqual(retired["error"], "service fence limit reached")
                break
            # Model ordinary retained-agent removal; the durable fence remains.
            with broker._connect() as db:
                db.execute("DELETE FROM agents WHERE id=?", (actor,))
        with broker._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM service_fences").fetchone()[0], 2)

    def test_a_raised_bound_takes_as_many_service_epoch_selectors(self):
        broker = self.broker(self.limits(active_per_principal=130, active_total=140,
                                         retained_per_principal=140, retained_total=160))
        actors = [self.register(broker, "actor-" + str(i))["id"] for i in range(130)]
        self.assertEqual(self.call(broker, "poll_device", agents=actors,
                                   service_epochs={actor: 0 for actor in actors})["messages"], [])

    def test_the_documented_table_states_the_defaults_and_the_ceilings(self):
        page = (ROOT / "docs/BROKER-REGISTRATION-LIMITS.md").read_text(encoding="utf-8")
        rows = re.findall(r"^\| `(\w+)` \| (\d+) \| (\d+) \|$", page, re.M)
        self.assertEqual({name: int(ceiling) for name, _, ceiling in rows}, module.REGISTRATION_LIMIT_CEILINGS)
        broker = self.broker()
        self.assertEqual({name: int(default) for name, default, _ in rows},
                         {name: broker._registration_limit(name) for name in module.REGISTRATION_LIMIT_CEILINGS})

    def test_configured_retained_fence_bound_does_not_evict_or_bypass_epoch_auth(self):
        broker = self.broker(self.limits(retained_per_principal=2))
        actors = []
        for i in range(2):
            service_id = str(uuid.uuid4())
            actor = self.call(broker, "register", session_key="service:" + service_id, name="service-" + str(i), kind="service", bus="general")
            self.call(broker, "service_fence_retire", agent=actor["id"], service_id=service_id,
                      expected_epoch=0, request_id="retire-" + str(i))
            actors.append(actor["id"])
        # Model ordinary retained-agent removal; the durable fence remains.
        with broker._connect() as db:
            db.execute("DELETE FROM agents WHERE id IN (?,?)", actors)
        service_id = str(uuid.uuid4())
        third = self.call(broker, "register", session_key="service:" + service_id, name="third", kind="service", bus="general")
        refused = self.call(broker, "service_fence_retire", agent=third["id"], service_id=service_id,
                            expected_epoch=0, request_id="third", ok=False)
        self.assertEqual(refused["error"], "service fence limit reached")
        with broker._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM service_fences").fetchone()[0], 2)
        self.call(broker, "service_fence_retire", agent=third["id"], service_id=service_id,
                  expected_epoch=1, request_id="wrong-epoch", ok=False)

    def test_concurrent_registration_cannot_exceed_owner_policy(self):
        policy = self.limits(active_per_principal=3)
        first = self.broker(policy)
        brokers = [first, self.broker(policy, state=first.root)]
        def request(index):
            return brokers[index % 2].handle(first.admin_token, {"op": "register", "kind": "codex",
                "name": str(index), "session_key": str(index), "bus": "general"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(request, range(16)))
        self.assertEqual(sum(reply["ok"] for reply in replies), 3)
        self.assertTrue(all(reply.get("code") == "limit" for reply in replies if not reply["ok"]))

    def test_installer_runner_forwards_only_owner_settings_policy_to_real_broker(self):
        spec = importlib.util.spec_from_file_location("owner_policy_installer", ROOT / "scripts/install-bus-hub.py")
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"BUS_GATEWAY_SHARED_SECRET": "synthetic-" + "s" * 40,
                                        "BUS_REGISTRATION_LIMITS": self.limits()}))
        settings.chmod(0o600)
        state = self.root / "hub"
        argv = ["install-bus-hub.py", "--source", str(ROOT), "--state", str(state), "--settings", str(settings)]
        fake = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(installer.sys, "argv", argv), mock.patch.object(installer.sys, "platform", "darwin"), \
             mock.patch.object(Path, "home", return_value=self.root), \
             mock.patch.object(installer.subprocess, "run", return_value=fake) as commands, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            installer.main()
        self.assertEqual([call.args[0][0] for call in commands.call_args_list], ["plutil", "launchctl", "launchctl"])
        with mock.patch.object(module, "serve") as serve:
            exec(compile((state / "run-hub.py").read_bytes(), "synthetic-run-hub.py", "exec"), {})
        self.assertEqual(serve.call_count, 1)
        broker, host, port = serve.call_args.args
        self.assertIsInstance(broker, Broker)
        self.assertEqual(broker.registration_limits, self.limits())
        self.assertEqual((host, port), ("127.0.0.1", 7433))


if __name__ == "__main__":
    unittest.main(verbosity=2)
