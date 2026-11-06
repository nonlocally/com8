#!/usr/bin/env python3
"""A running service worker is upgraded in place: same registration, same state, no lost message.

Two copies of this checkout stand in for two releases: the "old" one is a copy
whose worker runtime hash differs. The old release registers a service against
an isolated TLS hub and delivers; `bus start` from this checkout then replaces
the running worker cooperatively; `bus stop` opens a gap during which the hub
holds new messages; `bus start` closes it. Throughout, the agent id and the
state directory stay the same and every message reaches the fake service once.
Isolated temporary state only; no network.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "lib"))


def load_harness():
    spec = importlib.util.spec_from_file_location("measure_bus_delivery", ROOT / "scripts/measure-bus-delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerUpgrade(unittest.TestCase):
    def setUp(self):
        self.harness = load_harness()
        self.temp = Path(tempfile.mkdtemp(prefix="bus-upgrade-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        # The "old release": the same code with a different worker runtime hash.
        self.old = self.temp / "old-release"
        for name in ("bin", "lib"):
            shutil.copytree(ROOT / name, self.old / name, symlinks=True)
        (self.old / "VERSION").write_text("0.0.0-old\n")
        with open(self.old / "lib/bus_service.py", "a") as stream:
            stream.write("\n# old release fixture\n")
        self.recorder = self.harness.Recorder()
        cert, key = self.harness.make_certificate(self.temp)
        self.broker, hub, hub_thread, self.hub_url = self.harness.start_hub(self.temp, cert, key, self.recorder)
        self.token = "upgrade-bridge-token-" + uuid.uuid4().hex + uuid.uuid4().hex
        service, service_thread, self.endpoint = self.harness.start_service(cert, key, self.token, "immediate", self.recorder)
        self.servers = [(hub, hub_thread), (service, service_thread)]
        self.env = self.harness.client_environment(self.temp, cert)
        self.state = Path(self.env["COMM_STATE"]) / "bus"

    def tearDown(self):
        subprocess.run([str(ROOT / "bin/communicate"), "bus", "stop"], env=self.env, capture_output=True, timeout=60)
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def cli(self, root, *args):
        proc = subprocess.run([str(root / "bin/communicate"), "bus", *args], env=self.env, text=True,
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return proc.stdout.strip()

    def worker(self):
        return json.loads((self.state / "worker.json").read_text())

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def send(self, count):
        ids = []
        for index in range(count):
            result = self.broker.handle(self.broker.admin_token, {"op": "send", "sender": self.sender, "target": self.agent,
                                                                  "bus": "general", "message": "upgrade %d" % index})
            self.assertTrue(result.get("ok"), result)
            ids.append(result["id"])
        return ids

    def wait_delivered(self, ids, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            delivered = {event["id"] for event in self.recorder.snapshot("bridge") if event["op"] == "deliver"}
            if set(ids) <= delivered:
                return
            time.sleep(.2)
        self.fail("not delivered within %ss: %r" % (timeout, [mid for mid in ids if mid not in delivered]))

    def receipt(self, mid):
        return self.broker.handle(self.broker.admin_token, {"op": "receipt", "id": mid})["status"]

    def test_replace_running_worker_then_close_a_stop_gap_without_losing_the_registration(self):
        runtime_hash = __import__("bus").WORKER_RUNTIME
        self.cli(self.old, "connect", self.harness.invitation(self.broker, self.hub_url, "upgrade-account"))
        token_file = self.temp / "service.token"
        token_file.write_text(self.token)
        token_file.chmod(0o600)
        service_id = str(uuid.uuid4())
        registered = self.cli(self.old, "register-service", "upgraded-service", "--service-id", service_id,
                              "--endpoint", self.endpoint, "--token-file", str(token_file), "--contract-version", "2", "--json")
        self.agent = registered["id"]
        self.sender = self.broker.handle(self.broker.admin_token, {"op": "register", "session_key": "upgrade-sender",
                                                                   "name": "upgrade-sender", "kind": "codex"})["id"]
        old_worker = self.worker()
        self.assertNotEqual(old_worker["runtime"], runtime_hash)
        self.assertEqual(old_worker["version"], "0.0.0-old")
        self.assertEqual(old_worker["source"], str((self.old / "lib").resolve()))
        first = self.send(3)
        self.wait_delivered(first)
        # Upgrade in place: the current source replaces the running worker cooperatively.
        started = self.cli(ROOT, "start")
        self.assertTrue(started["current"], started)
        new_worker = self.worker()
        self.assertEqual((new_worker["runtime"], new_worker["version"], new_worker["source"]),
                         (runtime_hash, (ROOT / "VERSION").read_text().strip(), str((ROOT / "lib").resolve())))
        self.assertNotEqual(new_worker["pid"], old_worker["pid"])
        self.assertFalse(self.alive(old_worker["pid"]), "the old worker is still running")
        self.assertTrue(self.alive(new_worker["pid"]))
        status = self.cli(ROOT, "status", "--no-start", "--json")
        self.assertEqual((status["worker"]["pid"], status["worker"]["version"]), (new_worker["pid"], new_worker["version"]))
        # Same registration: the roster shows one service with the same id, and it delivers.
        roster = self.cli(ROOT, "agents", "--json")
        services = [row for bus in roster["buses"] for row in bus["agents"] if row["kind"] == "service"]
        self.assertEqual([row["id"] for row in services], [self.agent])
        second = self.send(5)
        self.wait_delivered(second)
        # A stop opens a gap: the hub holds new messages as accepted until a worker polls again.
        stopped = self.cli(ROOT, "stop")
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(self.alive(new_worker["pid"]))
        held = self.send(4)
        time.sleep(1)
        self.assertEqual({self.receipt(mid) for mid in held}, {"accepted"})
        resumed = self.cli(ROOT, "start")
        self.assertTrue(resumed["current"])
        self.assertNotEqual(resumed["worker"]["pid"], new_worker["pid"])
        self.wait_delivered(held)
        self.assertTrue(self.cli(ROOT, "start")["current"])
        self.assertEqual(self.cli(ROOT, "start")["worker"]["pid"], resumed["worker"]["pid"], "an unchanged source is not restarted")
        # Every message reached the service exactly once, every receipt ended queued, and only one worker ran at a time.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and {self.receipt(mid) for mid in first + second + held} != {"queued"}:
            time.sleep(.2)
        self.assertEqual({self.receipt(mid) for mid in first + second + held}, {"queued"})
        delivers = [event["id"] for event in self.recorder.snapshot("bridge") if event["op"] == "deliver"]
        self.assertEqual(sorted(delivers), sorted(first + second + held))
        self.assertEqual(json.loads((self.state / "registrations.json").read_text()).__len__(), 1)
        self.assertEqual(self.worker()["errors"], [])

    def test_start_refuses_without_a_registration(self):
        proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", "start"], env=self.env, text=True,
                              capture_output=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no active registration", proc.stderr)
        self.assertFalse((self.state / "worker.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
