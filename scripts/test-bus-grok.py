#!/usr/bin/env python3
"""Contract: a shell agent can join a bus without a Claude or Codex session.

A Grok agent is a one-shot shell on an enrolled device. It connects with the
existing invitation, then registers as kind grok with a private key file.
That key, not the display name and not the device token alone, is the agent:

- the same key file registers the same agent id; another key is another agent
- send, reply, leave, retire, poll and inbox require the matching key
- a plain shell that only knows the name or agent id cannot act as that agent
- the device poll used by the shared worker never releases a grok payload
- inbox reads queued messages without a live pane; queueable is not "running"
- private-bus membership is unchanged, and Claude/Codex sends stay key-free
- key bytes are never printed or stored in registration state
- an older hub reports that it must be upgraded; this client does not deploy it
"""
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from bus_broker import Broker, handler_factory


def digest(secret):
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


class GrokBrokerContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="grok-bus-")
        self.b = Broker(self.tmp.name)
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

    def join(self, bus="general", device="peer"):
        invite = self.call("invite", bus=bus)
        return self.call("redeem", token="", invite=invite["invite"], device=device)

    def grok(self, secret, name, **extra):
        fields = {"session_key": "grok:" + digest(secret), "name": name, "kind": "grok",
                  "status": "queueable", "agent_key": secret, "description": "shell agent"}
        fields.update(extra)
        return fields

    def test_grok_registration_is_idempotent_queueable_and_key_bound(self):
        device = self.join(device="grok-box")
        secret = "a" * 32
        first = self.call("register", token=device["token"], **self.grok(secret, "alpha"))
        self.assertEqual(first["status"], "queueable")
        self.assertNotIn("agent_key", first)
        self.assertNotIn(secret, json.dumps(first))
        again = self.call("register", token=device["token"], **self.grok(secret, "alpha"))
        self.assertEqual(first["id"], again["id"])
        renamed = self.call("register", token=device["token"], **self.grok(secret, "alpha-2"))
        self.assertEqual(renamed["id"], first["id"])
        self.assertEqual(renamed["name"], "alpha-2")
        other = self.call("register", token=device["token"], **self.grok("b" * 32, "beta"))
        self.assertNotEqual(other["id"], first["id"])
        clash = self.refused("register", token=device["token"], code="conflict", **self.grok("c" * 32, "beta"))
        self.assertIn("name", clash["error"])
        hijack = self.refused("register", token=device["token"], code="forbidden",
                              session_key="grok:" + digest(secret), agent_key="d" * 32,
                              name="alpha-2", kind="grok", status="queueable")
        self.assertNotIn(secret, hijack["error"])
        self.assertNotIn("d" * 32, hijack["error"])
        snap = self.call("snapshot", token=device["token"])
        blob = json.dumps(snap)
        self.assertNotIn(secret, blob)
        self.assertNotIn("agent_key", blob)
        self.assertNotIn("session_key", blob)
        rows = [agent for bus in snap["buses"] for agent in bus["agents"]]
        saved = next(agent for agent in rows if agent["id"] == first["id"])
        self.assertEqual(saved["kind"], "grok")
        self.assertEqual(saved["status"], "queueable")
        self.assertEqual(saved["name"], "alpha-2")

    def test_grok_register_rejects_missing_mismatched_and_live_claims(self):
        device = self.join()
        secret = "e" * 32
        before = self.call("snapshot", token=device["token"])
        missing = self.refused("register", token=device["token"], code="forbidden",
                               session_key="grok:" + digest(secret), name="bare", kind="grok", status="queueable")
        self.assertNotIn(secret, missing["error"])
        mismatched = self.refused("register", token=device["token"], code="forbidden",
                                  **self.grok(secret, "bare", session_key="grok:" + digest("f" * 32)))
        self.assertNotIn(secret, mismatched["error"])
        live = self.refused("register", token=device["token"], **self.grok(secret, "bare", status="live"))
        self.assertIn("queueable", live["error"])
        self.refused("register", token=device["token"], session_key="codex:" + "ab" * 16,
                     name="nope", kind="grok", status="queueable", agent_key=secret, code="forbidden")
        omitted = self.grok(secret, "defaults")
        omitted.pop("status")
        self.assertEqual(self.call("register", token=device["token"], **omitted)["status"], "queueable")
        unknown = self.refused("register", token=device["token"], session_key="shell:1", name="nope", kind="shell")
        self.assertIn("grok", unknown["error"])
        after = self.call("snapshot", token=device["token"])
        self.assertEqual([agent["name"] for bus in before["buses"] for agent in bus["agents"]], [])
        self.assertEqual([agent["name"] for bus in after["buses"] for agent in bus["agents"]], ["defaults"])

    def test_device_token_cannot_act_as_grok_and_device_poll_leases_nothing(self):
        device = self.join(device="shared-box")
        secret = "g" * 32
        other_secret = "h" * 32
        grok = self.call("register", token=device["token"], **self.grok(secret, "grok-a"))["id"]
        sibling = self.call("register", token=device["token"], **self.grok(other_secret, "grok-b"))["id"]
        claude = self.call("register", token=device["token"], session_key="claude:session", name="claude-a")["id"]
        sender = self.join(device="sender")
        sender_agent = self.call("register", token=sender["token"], session_key="claude:sender", name="sender")["id"]
        body = "private grok payload"
        mid = self.call("send", token=sender["token"], sender=sender_agent, target=grok, message=body)["id"]
        claude_mid = self.call("send", token=sender["token"], sender=sender_agent, target=claude, message="claude payload")["id"]
        self.refused("send", token=device["token"], code="forbidden", sender=grok, target=sibling, message="impersonated")
        self.refused("send", token=device["token"], code="forbidden", sender=grok, target=sibling,
                     message="hash presented as key", agent_key=digest(secret))
        self.refused("send", token=device["token"], code="forbidden", sender=grok, target=sibling,
                     message="sibling key", agent_key=other_secret)
        polled = self.call("poll_device", token=device["token"], agents=[grok, claude])
        self.assertEqual([item["id"] for item in polled["messages"]], [claude_mid])
        self.assertNotIn(body, json.dumps(polled))
        self.assertEqual(polled["refusals"], [{"agent": grok, "code": "grok_key_required"}])
        stuffed = self.call("poll_device", token=device["token"], agents=[grok], agent_key=secret)
        self.assertEqual(stuffed["messages"], [])
        self.assertEqual(self.call("receipt", token=sender["token"], id=mid)["status"], "accepted")
        self.refused("poll", token=device["token"], code="forbidden", agent=grok)
        self.refused("poll", token=device["token"], code="forbidden", agent=grok, agent_key=other_secret)
        leased = self.call("poll", token=device["token"], agent=grok, agent_key=secret)
        self.assertEqual(leased["messages"][0]["message"], body)
        self.assertNotIn("agent_key", json.dumps(leased))
        self.refused("ack", token=device["token"], code="forbidden", agent=grok, id=mid,
                     status="queued", detail="no key", lease=leased["messages"][0]["lease"])
        self.assertEqual(self.call("receipt", token=sender["token"], id=mid)["status"], "leased")
        self.call("ack", token=device["token"], agent=grok, agent_key=secret, id=mid,
                  status="queued", detail="stored in the grok inbox", lease=leased["messages"][0]["lease"])
        self.assertEqual(self.call("receipt", token=sender["token"], id=mid)["status"], "queued")
        self.refused("reply", token=device["token"], code="forbidden", sender=grok, id=mid, message="nope")
        reply = self.call("reply", token=device["token"], sender=grok, agent_key=secret, id=mid, message="answer")
        self.assertEqual(reply["status"], "accepted")
        self.refused("leave", token=device["token"], code="forbidden", agent=grok, bus="general")
        still = [agent["id"] for bus in self.call("snapshot", token=device["token"])["buses"]
                 if bus["name"] == "general" for agent in bus["agents"]]
        self.assertIn(grok, still)
        self.call("leave", token=device["token"], agent=grok, agent_key=secret, bus="general")
        left = [agent["id"] for bus in self.call("snapshot", token=device["token"])["buses"]
                if bus["name"] == "general" for agent in bus["agents"]]
        self.assertNotIn(grok, left)
        self.call("register", token=device["token"], **self.grok(secret, "grok-a", bus="general"))
        self.refused("retire", token=device["token"], code="forbidden", agent=grok)
        self.call("retire", token=device["token"], agent=grok, agent_key=secret)
        retired = [agent["id"] for bus in self.call("snapshot", token=device["token"])["buses"] for agent in bus["agents"]]
        self.assertNotIn(grok, retired)
        # A normal Claude send on the same device still needs no agent key.
        self.call("send", token=device["token"], sender=claude, target=sender_agent, message="claude still sends")
        self.refused("send", token=device["token"], sender=claude, target=sender_agent,
                     message="unexpected key", agent_key=secret)

    def test_private_bus_grant_still_gates_grok_registration(self):
        self.call("create", bus="photonics")
        device = self.join(bus="photonics", device="guest")
        secret = "i" * 32
        self.refused("register", token=device["token"], code="forbidden",
                     **self.grok(secret, "guest-agent", bus="general"))
        joined = self.call("register", token=device["token"], **self.grok(secret, "guest-agent", bus="photonics"))
        self.assertEqual(joined["buses"], ["photonics"])
        outsider = self.join(device="outsider")
        published = self.call("register", token=outsider["token"], session_key="claude:out", name="outsider")["id"]
        self.refused("send", token=device["token"], sender=joined["id"], target=published,
                     bus="general", message="not a member", agent_key=secret)


class GrokClientContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = Path(tempfile.mkdtemp(prefix="grok-cli-", dir="/tmp"))
        cls.broker = Broker(cls.temp / "hub")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(cls.broker))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port
        cls.envs = []

    @classmethod
    def tearDownClass(cls):
        for env in cls.envs:
            subprocess.run([str(ROOT / "bin/communicate"), "bus", "stop"], env=env,
                           capture_output=True, timeout=15)
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def env(self, label):
        home = self.temp / (label + "-" + uuid.uuid4().hex[:6])
        home.mkdir()
        env = dict(os.environ, HOME=str(home), COMM_STATE=str(home / "state"),
                   CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
                   COMM_BUS_PORT="0")
        for key in ("CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                    "COMM_CODEX_INDEX", "COM8_BUS_KEY_FILE"):
            env.pop(key, None)
        self.envs.append(env)
        return env

    def cli(self, env, *args, ok=True):
        proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", *args],
                              env=env, text=True, capture_output=True, timeout=30)
        self.last = proc
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return proc.stdout.strip() if ok else proc.stderr

    def code(self, name="general"):
        if name != "general":
            self.assertTrue(self.broker.handle(self.broker.admin_token, {"op": "create", "bus": name})["ok"])
        invite = self.broker.handle(self.broker.admin_token, {"op": "invite", "bus": name, "ttl": 60})
        raw = json.dumps({"url": self.url, "invite": invite["invite"]}).encode()
        return "commbus1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def token(self, env):
        cfg = json.loads((Path(env["COMM_STATE"]) / "bus/client.json").read_text())
        return cfg["connections"][self.url]["token"]

    def test_help_describes_the_grok_key(self):
        proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", "register", "--help"],
                              text=True, capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("grok", proc.stdout)
        self.assertIn("COM8_BUS_KEY_FILE", proc.stdout)
        inbox = subprocess.run([str(ROOT / "bin/communicate"), "bus", "inbox", "--help"],
                               text=True, capture_output=True, timeout=15)
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertIn("--key-file", inbox.stdout)

    def test_plain_shell_registers_grok_without_a_claude_or_codex_session(self):
        env = self.env("plain")
        self.cli(env, "connect", self.code(), "--device", "grok-box")
        refused = self.cli(env, "register", "--name", "not-a-session", ok=False)
        self.assertIn("cannot identify", refused)
        self.assertIn("grok", refused)
        missing_name = self.cli(env, "register", "--kind", "grok", "--key-file", str(Path(env["HOME"]) / "missing-name.key"), ok=False)
        self.assertIn("name", missing_name.lower())
        missing_key = self.cli(env, "register", "--kind", "grok", "--name", "needs-a-key", ok=False)
        self.assertIn("key", missing_key.lower())
        key = Path(env["HOME"]) / "plain.key"
        registered = self.cli(env, "register", "--kind", "grok", "--name", "plain-grok",
                              "--key-file", str(key), "--description", "Grok agent on a shell")
        secret = key.read_text().strip()
        self.assertGreaterEqual(len(secret), 32)
        self.assertNotIn(secret, self.last.stdout)
        self.assertNotIn(secret, self.last.stderr)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(key.stat().st_nlink, 1)
        stored = (Path(env["COMM_STATE"]) / "bus/registrations.json").read_text()
        self.assertNotIn(secret, stored)
        self.assertNotIn("agent_key", stored)
        self.assertEqual(registered["kind"], "grok")
        self.assertEqual(registered["status"], "queueable")
        self.assertEqual(registered["name"], "plain-grok")
        self.assertEqual(registered["key_file"], str(key))
        again = self.cli(env, "register", "--kind", "grok", "--name", "plain-grok",
                         "--key-file", str(key), "--description", "Grok agent on a shell")
        self.assertEqual(again["id"], registered["id"])
        self.assertNotIn(secret, self.last.stdout)

    def test_unsafe_key_files_are_refused(self):
        env = self.env("unsafe")
        self.cli(env, "connect", self.code())
        real = Path(env["HOME"]) / "real.key"
        self.cli(env, "register", "--kind", "grok", "--name", "holder", "--key-file", str(real))
        link = Path(env["HOME"]) / "linked.key"
        link.symlink_to(real)
        linked = self.cli(env, "send", "a_" + "0" * 32, "--key-file", str(link), "--", "nope", ok=False)
        self.assertIn("symlink", linked.lower())
        weak = Path(env["HOME"]) / "weak.key"
        weak.write_text("w" * 32 + "\n")
        os.chmod(weak, 0o644)
        readable = self.cli(env, "register", "--kind", "grok", "--name", "weak", "--key-file", str(weak), ok=False)
        self.assertIn("private", readable.lower())
        hard = Path(env["HOME"]) / "hard.key"
        os.link(real, hard)
        shared = self.cli(env, "send", "a_" + "1" * 32, "--key-file", str(hard), "--", "nope", ok=False)
        self.assertIn("link", shared.lower())
        hard.unlink()

    def test_two_agents_on_one_device_send_and_read_only_with_their_own_key(self):
        env = self.env("pair")
        self.cli(env, "connect", self.code(), "--device", "shared-box")
        key_a = Path(env["HOME"]) / "a.key"
        key_b = Path(env["HOME"]) / "b.key"
        agent_a = self.cli(env, "register", "--kind", "grok", "--name", "grok-a",
                           "--key-file", str(key_a), "--description", "agent A")
        agent_b = self.cli(env, "register", "--kind", "grok", "--name", "grok-b",
                           "--key-file", str(key_b), "--description", "agent B")
        self.assertNotEqual(agent_a["id"], agent_b["id"])
        duplicate = self.cli(env, "register", "--kind", "grok", "--name", "grok-a",
                             "--key-file", str(Path(env["HOME"]) / "c.key"), ok=False)
        self.assertIn("name", duplicate.lower())
        stolen = self.cli(env, "send", agent_b["id"], "--from", "grok-a", "--", "stolen", ok=False)
        self.assertIn("key", stolen.lower())
        crossed = self.cli(env, "send", agent_b["id"], "--from", "grok-a", "--key-file", str(key_b),
                           "--", "stolen", ok=False)
        self.assertIn("key", crossed.lower())
        body = "hello $HOME `uname`"
        mid = self.cli(env, "send", agent_b["id"], "--key-file", str(key_a), "--", body)["id"]
        self.assertNotIn(key_a.read_text().strip(), self.last.stdout)
        time.sleep(3)
        self.assertEqual(self.cli(env, "receipt", mid)["status"], "accepted")
        polled = self.broker.handle(self.token(env), {"op": "poll_device", "agents": [agent_a["id"], agent_b["id"]]})
        self.assertTrue(polled.get("ok"), polled)
        self.assertEqual(polled["messages"], [])
        self.assertNotIn(body, json.dumps(polled))
        inbox = self.cli(env, "inbox", "--key-file", str(key_b))
        self.assertEqual([item["message"] for item in inbox["messages"]], [body])
        self.assertNotIn("lease", inbox["messages"][0])
        self.assertNotIn(key_b.read_text().strip(), self.last.stdout)
        spool = Path(env["COMM_STATE"]) / "bus/grok-inbox.sqlite"
        self.assertEqual(stat.S_IMODE(spool.stat().st_mode) & 0o077, 0)
        again = self.cli(env, "inbox", "--key-file", str(key_b))
        self.assertEqual([item["id"] for item in again["messages"]], [inbox["messages"][0]["id"]])
        self.assertEqual(self.cli(env, "inbox", "--key-file", str(key_a))["messages"], [])
        self.assertEqual(self.cli(env, "receipt", mid)["status"], "queued")
        reply = self.cli(env, "reply", mid, "--key-file", str(key_b), "--", "pong")
        self.assertEqual(reply["status"], "accepted")
        found = self.cli(env, "inbox", "--key-file", str(key_a))
        self.assertEqual([item["message"] for item in found["messages"]], ["pong"])
        self.cli(env, "inbox", "ack", found["messages"][0]["id"], "--key-file", str(key_a))
        self.assertEqual(self.cli(env, "inbox", "--key-file", str(key_a))["messages"], [])
        foreign = self.cli(env, "inbox", "ack", found["messages"][0]["id"], "--key-file", str(key_b), ok=False)
        self.assertNotEqual(foreign, "")
        self.cli(env, "leave", agent_a["id"], ok=False)
        self.cli(env, "leave", agent_a["id"], "--key-file", str(key_a))
        visible = [agent["id"] for bus in self.cli(env, "agents", "--json")["buses"] for agent in bus["agents"]]
        self.assertNotIn(agent_a["id"], visible)
        self.assertIn(agent_b["id"], visible)

    def test_environment_key_is_scoped_and_does_not_replace_a_native_session(self):
        env = self.env("scoped")
        self.cli(env, "connect", self.code())
        key_a = Path(env["HOME"]) / "a.key"
        key_b = Path(env["HOME"]) / "b.key"
        agent_a = self.cli(env, "register", "--kind", "grok", "--name", "env-a", "--key-file", str(key_a))
        agent_b = self.cli(env, "register", "--kind", "grok", "--name", "env-b", "--key-file", str(key_b))
        env["COM8_BUS_KEY_FILE"] = str(key_a)
        sent = self.cli(env, "send", agent_b["id"], "--", "from the environment")
        self.assertEqual(sent["status"], "accepted")
        explicit = self.cli(env, "send", agent_a["id"], "--key-file", str(key_b), "--", "explicit other key")
        self.assertEqual(explicit["status"], "accepted")
        env["CODEX_THREAD_ID"] = "not-a-native-session"
        blocked = self.cli(env, "send", agent_b["id"], "--", "native session wins", ok=False)
        self.assertIn("CODEX_THREAD_ID", blocked)
        overridden = self.cli(env, "send", agent_b["id"], "--key-file", str(key_a), "--", "explicit key wins")
        self.assertEqual(overridden["status"], "accepted")
        del env["CODEX_THREAD_ID"]
        pulled = self.cli(env, "inbox", "--key-file", str(key_b))
        self.assertEqual(sorted(item["message"] for item in pulled["messages"]),
                         ["explicit key wins", "from the environment"])

    def test_private_bus_membership_is_unchanged(self):
        env = self.env("private")
        self.cli(env, "connect", self.code("photonics"), "--device", "workshop")
        key = Path(env["HOME"]) / "guest.key"
        refused_general = self.cli(env, "register", "--kind", "grok", "--name", "guest",
                                   "--key-file", str(key), ok=False)
        self.assertIn("membership", refused_general.lower())
        joined = self.cli(env, "register", "--kind", "grok", "--name", "guest",
                          "--bus", "photonics", "--key-file", str(key), "--description", "workshop agent")
        self.assertEqual(joined["bus"], "photonics")
        buses = [bus["name"] for bus in self.cli(env, "agents", "--json")["buses"]]
        self.assertEqual(buses, ["photonics"])
        refused = self.cli(env, "send", "a_" + "2" * 32, "--bus", "general", "--key-file", str(key),
                           "--", "outside", ok=False)
        self.assertNotEqual(refused, "")

    def test_old_hub_names_the_required_broker_upgrade(self):
        class OldHub(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = json.dumps({"ok": False, "error": "kind must be claude, codex or service",
                                   "code": "invalid_request"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OldHub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        url = "http://127.0.0.1:%d" % server.server_port
        env = self.env("old-hub")
        state = Path(env["COMM_STATE"]) / "bus"
        state.mkdir(parents=True)
        os.chmod(state, 0o700)
        (state / "client.json").write_text(json.dumps({
            "default": url,
            "connections": {url: {"url": url, "token": "t" * 32, "principal": "p_old"}}}))
        os.chmod(state / "client.json", 0o600)
        key = Path(env["HOME"]) / "old.key"
        error = self.cli(env, "register", "--kind", "grok", "--name", "needs-hub",
                         "--key-file", str(key), "--description", "pending broker", ok=False)
        self.assertIn("does not accept grok", error)
        self.assertIn("deploy", error)
        self.assertFalse((state / "registrations.json").exists())


if __name__ == "__main__":
    unittest.main()
