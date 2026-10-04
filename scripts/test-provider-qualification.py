#!/usr/bin/env python3
"""Validate release-proof parsing without contacting a provider."""
import hashlib
import importlib.util
import contextlib
import io
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("provider_gate", Path(__file__).with_name("qualify-provider.py"))
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class ProofTests(unittest.TestCase):
    def test_installed_codex_stops_before_delivery_and_authorizes_resume(self):
        """A live recipient auto-dequeues on turn completion; closing loses it.

        Exercise the complete harness with a stateful queue fixture. The fake
        endpoint refuses that race and dispatches its reply during resume,
        before thread/resume returns, as the actual native client can do.
        """
        clients = {}
        session = "12345678-1234-4234-8234-123456789abc"
        hub = "http://127.0.0.1:1234"
        state = {}

        def call_event(tool, arguments=None):
            return {"method": "item/completed", "params": {"item": {
                "type": "mcpToolCall", "tool": tool, "status": "completed",
                "arguments": arguments or {}}}}

        class Client:
            def __init__(self, executable, env, cwd, evidence, label, *args):
                clients[label] = self
                self.cwd, self.label = cwd, label
                self.events, self.approvals, self.reply = [], [], None
                self.closed, self.completed = False, False
                self.process = SimpleNamespace(poll=lambda: 0 if self.closed else None)

            def thread(self, existing=None):
                if self.label == "resume":
                    self_test.assertEqual(existing, session)
                    self_test.assertEqual(self.reply, state["reply_context"])
                    # Native queued execution is permitted before resume returns.
                    self.events.append(call_event("bus_reply", {"id": "message"}))
                    state["answer"] = {"id": "reply", "sender": {"id": "recipient"},
                                       "target": "sender", "message": self.reply["payload"], "lease": "lease"}
                return session

            def prompt(self, text, streaming=False):
                self.events.append(call_event("bus_status"))
                if self.label == "initial":
                    folder = self.cwd / "state/bus"
                    folder.mkdir(parents=True)
                    (folder / "registrations.json").write_text(json.dumps({"recipient": {
                        "id": "recipient", "name": state["name"], "session_key": "codex:" + session}}))
                    (folder / "client.json").write_text(json.dumps({"default": "owner", "connections": {
                        "owner": {"url": hub, "local": True}}}))
                    self.events.append(call_event("bus_register"))

            def wait_turn(self):
                self.completed = True

            def close(self):
                self.closed = True

            def finished(self):
                return self.completed

        def request(connection, operation, **fields):
            if operation == "invite":
                return {"invite": "fixture"}
            if operation == "redeem":
                return {"token": "fixture"}
            if operation == "identify":
                return {"id": "sender"}
            if operation == "send":
                recipient = clients["initial"]
                if not recipient.closed or not recipient.completed:
                    raise RuntimeError("live recipient would auto-dequeue before harness shutdown")
                payload = fields["message"].split("COM8_PAYLOAD_BEGIN\n", 1)[1].rsplit("\nCOM8_PAYLOAD_END", 1)[0]
                state["reply_context"] = {"id": "message", "payload": payload, "recipient": "recipient", "hub": hub}
                with sqlite3.connect(recipient.cwd / "state/bus/bus.sqlite3") as db:
                    db.execute("CREATE TABLE messages(id TEXT, conversation TEXT)")
                    db.executemany("INSERT INTO messages VALUES (?, 'conversation')", [("message",), ("reply",)])
                return {"id": "message"}
            if operation == "receipt":
                return {"status": "queued"}
            if operation == "poll":
                return {"messages": [state["answer"]] if "answer" in state else []}
            if operation == "ack":
                return {}
            raise AssertionError(operation)

        self_test = self
        def create_client(executable, env, cwd, evidence, label, name, *args):
            state["name"] = name
            return Client(executable, env, cwd, evidence, label, *args)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            argv = [str(SPEC.origin), str(root), "--provider", "codex", "--client-home", str(home),
                    "--evidence", str(root / "evidence"), "--installed", "--run-live"]
            with patch.object(sys, "argv", argv), patch.object(gate, "artifact", return_value={"source": "fixture", "version": "0.3.0"}), \
                    patch.object(gate.shutil, "which", return_value="/fixture/codex"), \
                    patch.object(gate, "CodexAppServer", side_effect=create_client), \
                    patch.object(gate, "import_artifact_bus", return_value=SimpleNamespace(request=request)), \
                    patch.object(gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "codex fixture", "")), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = gate.main()
            report = json.loads((root / "evidence/report.json").read_text())
            self.assertEqual(result, 0, report)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["exact_session"], session)
            self.assertTrue(all(client.closed for client in clients.values()))

    def test_resume_identity_is_available_to_immediate_native_approvals(self):
        client = object.__new__(gate.CodexAppServer)
        client.cwd, client.session = "/fixture", None

        def resume(method, params):
            self.assertEqual(method, "thread/resume")
            self.assertEqual(client.session, "verified-thread")
            return {"thread": {"id": "verified-thread"}}

        client.call = resume
        self.assertEqual(client.thread("verified-thread"), "verified-thread")

    def approval(self, tool, arguments):
        params = {"threadId": "thread-fixture", "turnId": "turn-fixture", "serverName": "communicate", "mode": "form",
                  "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": arguments}}
        events = [{"method": "item/started", "params": {"threadId": params["threadId"], "turnId": params["turnId"],
                  "item": {"id": "call-fixture", "type": "mcpToolCall", "server": "communicate",
                           "pluginId": "communicate@communicate", "tool": tool, "arguments": arguments}}}]
        return params, events

    def test_approval_requires_unique_pending_installed_tool(self):
        params, events = self.approval("bus_status", {})
        self.assertEqual(gate.pending_approval_tool(events, params), "bus_status")
        self.assertTrue(gate.authorize_fixture_tool(params,"thread-fixture","fixture",tool="bus_status"))
        self.assertIsNone(gate.pending_approval_tool([], params))
        duplicate = json.loads(json.dumps(events[0])); duplicate["params"]["item"]["id"] = "other-call"
        self.assertIsNone(gate.pending_approval_tool(events + [duplicate], params))
        completed = json.loads(json.dumps(events[0])); completed["method"] = "item/completed"
        self.assertIsNone(gate.pending_approval_tool(events + [completed], params))
        events[0]["params"]["item"]["pluginId"] = "unrelated@plugin"
        self.assertIsNone(gate.pending_approval_tool(events,params))
        self.assertFalse(gate.authorize_fixture_tool(params,"different-thread","fixture",tool="bus_status"))
        params["mode"] = "url"
        self.assertFalse(gate.authorize_fixture_tool(params,"thread-fixture","fixture",tool="bus_status"))

    def test_registration_and_reply_authorization_are_exact(self):
        arguments = {"kind":"codex","session":"thread-fixture","name":"fixture"}
        params, _ = self.approval("bus_register", arguments)
        self.assertTrue(gate.authorize_fixture_tool(params,"thread-fixture","fixture",tool="bus_register"))
        arguments["session"] = "unrelated"
        self.assertFalse(gate.authorize_fixture_tool(params,"thread-fixture","fixture",tool="bus_register"))
        reply = {"id":"message-fixture","recipient":"agent-fixture","payload":"literal\nbytes","hub":"http://127.0.0.1:1234"}
        params, _ = self.approval("bus_reply", {"id":reply["id"],"from":reply["recipient"],"message":reply["payload"]})
        self.assertTrue(gate.authorize_fixture_tool(params,"thread-fixture","fixture",reply,tool="bus_reply"))
        params["_meta"]["tool_params"]["hub"] = reply["hub"]
        self.assertTrue(gate.authorize_fixture_tool(params,"thread-fixture","fixture",reply,tool="bus_reply"))
        params["_meta"]["tool_params"]["hub"] = "https://unrelated.invalid"
        self.assertFalse(gate.authorize_fixture_tool(params,"thread-fixture","fixture",reply,tool="bus_reply"))
        params["_meta"]["tool_params"]["hub"] = reply["hub"]
        params["_meta"]["tool_params"]["message"] += " changed"
        self.assertFalse(gate.authorize_fixture_tool(params,"thread-fixture","fixture",reply,tool="bus_reply"))
        self.assertFalse(gate.authorize_fixture_tool(params,"thread-fixture","fixture",reply,tool="bus_send"))

    def test_app_server_calls_use_wire_items(self):
        _, events = self.approval("bus_reply", {"id":"message-fixture"})
        self.assertEqual(gate.tool_calls(events),[("bus_reply",{"id":"message-fixture"})])

    def test_outbound_approval_is_disabled_without_exact_peer_challenge(self):
        outgoing = {"sender":"agent-self","target":"agent-peer","message":"nonce-fixture\nliteral $HOME", "hub":"http://127.0.0.1:1234"}
        arguments = {"from":outgoing["sender"],"target":outgoing["target"],"message":outgoing["message"],"hub":outgoing["hub"]}
        params, _ = self.approval("bus_send", arguments)
        self.assertFalse(gate.authorize_fixture_tool(params,"thread-fixture","fixture",tool="bus_send"))
        self.assertTrue(gate.authorize_fixture_tool(params,"thread-fixture","fixture",tool="bus_send",outgoing=outgoing))
        for key, changed in [("target","unrelated"),("from","another-agent"),("message","changed"),("hub","https://unrelated.invalid"),("bus","private")]:
            modified = json.loads(json.dumps(params));modified["_meta"]["tool_params"][key] = changed
            self.assertFalse(gate.authorize_fixture_tool(modified,"thread-fixture","fixture",tool="bus_send",outgoing=outgoing))

    def test_assistant_claim_is_not_a_tool_call(self):
        self.assertEqual(gate.tool_calls([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I called bus_register and bus_reply successfully"}]}}]), [])

    def test_claude_tool_call_keeps_original_reply_id(self):
        calls = gate.tool_calls([{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__plugin_communicate_communicate__bus_reply",
             "input": {"id": "m_original", "message": "literal\n--from"}}]}}])
        self.assertEqual(calls[0][1], {"id": "m_original", "message": "literal\n--from"})

    def test_codex_structured_and_serialized_arguments(self):
        for arguments in ({"id": "m_original"}, '{"id":"m_original"}'):
            calls = gate.tool_calls([{"type": "item.completed", "item": {
                "type": "mcp_tool_call", "tool": "bus_reply", "arguments": arguments}}])
            self.assertEqual(calls, [("bus_reply", {"id": "m_original"})])

    def test_malformed_arguments_do_not_supply_a_reply_id(self):
        self.assertEqual(gate.tool_calls([{"item": {"type": "mcp_tool_call", "tool": "bus_reply",
                                                    "arguments": "not JSON"}}]), [("bus_reply", {})])

    def test_artifact_hash_change_and_checkout_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "runtime.txt"
            path.write_text("reviewed")
            manifest = {"source": "fixture", "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}}
            (root / "release.json").write_text(json.dumps(manifest))
            self.assertEqual(gate.artifact(root), manifest)
            path.write_text("changed")
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                gate.artifact(root)
            (root / ".git").mkdir()
            with self.assertRaisesRegex(RuntimeError, "not a checkout"):
                gate.artifact(root)

    def test_artifact_import_does_not_create_bytecode_or_change_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "vendor/lib/bus.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 'fixture'\n")
            manifest = {"source": "fixture", "files": {
                "vendor/lib/bus.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
            (root / "release.json").write_text(json.dumps(manifest))
            before = gate.artifact(root)
            self.assertEqual(gate.import_artifact_bus(root).VALUE, "fixture")
            self.assertEqual(gate.artifact(root), before)
            self.assertFalse((source.parent / "__pycache__").exists())
            link = root / "internal-link"
            link.symlink_to("vendor/lib/bus.py")
            self.assertEqual(gate.artifact(root), before)
            link.unlink()
            link.symlink_to("../outside-artifact")
            with self.assertRaisesRegex(RuntimeError, "unsafe artifact symlink"):
                gate.artifact(root)
            link.unlink()
            (source.parent / "unexpected.pyc").write_bytes(b"unexpected")
            with self.assertRaisesRegex(RuntimeError, "inventory mismatch"):
                gate.artifact(root)


if __name__ == "__main__":
    unittest.main()
