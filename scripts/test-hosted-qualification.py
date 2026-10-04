#!/usr/bin/env python3
"""Standalone hosted-gate fixtures. No network, providers, or real bus state."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("hosted_gate", Path(__file__).with_name("qualify-hosted.py"))
hosted = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hosted)


class HostedTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture_database(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            yield db

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-hosted-unit-")
        self.root = Path(self.temp.name)
        self.db = self.root / "bus.sqlite3"
        with self.fixture_database() as db:
            db.executescript("""
                CREATE TABLE meta(key TEXT,value TEXT);
                INSERT INTO meta VALUES('server_id','reviewed-server');
                CREATE TABLE buses(name TEXT,visibility TEXT);
                INSERT INTO buses VALUES('general','open'),('approved-fixture','private');
                CREATE TABLE principals(id TEXT,device TEXT,is_admin INTEGER,revoked INTEGER);
                INSERT INTO principals VALUES('owned','com8-hosted-abcdefghijkl-codex',0,0),('peer','com8-hosted-abcdefghijkl-claude',0,0),('foreign','real-person',0,0);
                CREATE TABLE invites(digest TEXT,principal TEXT,redeemed_at REAL,expires_at REAL);
                CREATE TABLE agents(id TEXT,principal TEXT,session_key TEXT);
                INSERT INTO agents VALUES('own-agent','owned','codex:verified'),('peer-agent','peer','claude:verified'),('other-agent','foreign','private-session');
                CREATE TABLE memberships(agent TEXT,bus TEXT);
                INSERT INTO memberships VALUES('peer-agent','general'),('other-agent','general');
                CREATE TABLE messages(id TEXT,sender TEXT,target TEXT,message TEXT);
                INSERT INTO messages VALUES('ours','own-agent','peer-agent','generated-fixture'),
                    ('theirs','other-agent','other-agent','PRIVATE-FOREIGN-TEXT'),
                    ('unexpected','other-agent','own-agent','PRIVATE-INCOMING-TEXT');
            """)
        self.db.chmod(0o600)
        self.broker = hosted.HostedBroker.__new__(hosted.HostedBroker)
        self.broker.database = self.db
        self.broker.name, self.broker.run_id = "general", "abcdefghijkl000000"
        self.broker.token, self.broker.accounts = "private-fixture-token-not-for-output-000", {"fixture-owner"}
        self.broker.invites, self.broker.principals = ["invite-own", "invite-peer"], set()
        self.broker.labels = {"com8-hosted-abcdefghijkl-codex", "com8-hosted-abcdefghijkl-claude"}
        self.broker.uncertain_invite_until = None
        with self.fixture_database() as db:
            for secret, principal in zip(self.broker.invites, ("owned", "peer")):
                db.execute("INSERT INTO invites VALUES(?,?,1,9999999999)", (hosted.hashlib.sha256(secret.encode()).hexdigest(), principal))

    def tearDown(self):
        self.temp.cleanup()

    def test_evidence_never_selects_foreign_message_text(self):
        self.assertEqual([row["id"] for row in self.broker.rows()], ["ours"])
        output = json.dumps(self.broker.rows())
        self.assertNotIn("PRIVATE", output)
        self.assertEqual(self.broker.unexpected_traffic(), 1)
        self.assertEqual({r["principal"] for r in self.broker.rows("agents")}, {"owned", "peer"})
        self.assertNotIn("other-agent", json.dumps(self.broker.rows("memberships")))

    def test_sqlite_is_opened_read_only(self):
        with self.broker.database_connection() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM principals")

    def test_lost_redeem_response_recovered_from_own_invite_digest(self):
        self.assertEqual(self.broker.principals, set())
        self.assertEqual(self.broker.discover_principals(), {"owned", "peer"})

    def test_label_alone_cannot_claim_foreign_principal(self):
        with self.fixture_database() as db:
            db.execute("UPDATE invites SET principal='foreign' WHERE principal='owned'")
        with self.assertRaisesRegex(RuntimeError, "owned fixture"):
            self.broker.discover_principals()

    def test_admin_never_uses_model_operations(self):
        for operation in ("send", "reply", "poll", "poll_device", "ack", "create", "register", "identify"):
            with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "outside hosted"):
                self.broker.owner(operation)

    def test_foreign_revocation_and_invitation_scope_refused(self):
        for op, values in [("revoke", {"principal": "foreign"}), ("invite_revoke", {"invite": "foreign-secret"}),
                           ("invite", {"bus": "other-private", "user": "fixture-owner", "ttl": 600}),
                           ("invite", {"bus": "general", "user": "other-owner", "ttl": 600})]:
            with self.subTest(operation=op), self.assertRaises(RuntimeError):
                self.broker.owner(op, **values)

    def test_cleanup_only_revokes_invitation_owned_principals(self):
        called = []
        def owner(op, **values):
            self.assertEqual(op, "revoke")
            called.append(values["principal"])
            with self.fixture_database() as db:
                db.execute("UPDATE principals SET revoked=1 WHERE id=?", (values["principal"],))
            return {"ok": True}
        self.broker.owner = owner
        result = self.broker.revoke_owned()
        self.assertEqual(set(called), {"owned", "peer"})
        self.assertEqual(result["principals_revoked"], 2)
        with self.fixture_database() as db:
            self.assertEqual(db.execute("SELECT revoked FROM principals WHERE id='foreign'").fetchone()[0], 0)

    def test_failed_operator_request_does_not_echo_credentials(self):
        def fail(*args, **kwargs):
            raise ValueError(self.broker.token + " private-response")
        self.broker.opener = SimpleNamespace(open=fail)
        with self.assertRaises(RuntimeError) as error:
            self.broker.owner("snapshot")
        self.assertNotIn(self.broker.token, str(error.exception))
        self.assertNotIn("private-response", str(error.exception))

    def test_failed_invite_does_not_claim_no_possible_residue(self):
        self.broker.opener = SimpleNamespace(open=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
        with patch.object(hosted.time, "time", side_effect=[100, 125]), self.assertRaises(RuntimeError):
            self.broker.owner("invite", bus="general", user="fixture-owner", ttl=600)
        self.assertEqual(self.broker.uncertain_invite_until, 725)

    def test_private_inputs_refuse_public_modes_and_symlinks(self):
        path = self.root / "credential"
        path.write_text("synthetic")
        path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            hosted.private_input(path)
        path.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(RuntimeError):
            hosted.private_input(link)
        self.assertEqual(hosted.private_input(path), path)

    def test_wrong_broker_identity_fails_before_network(self):
        token = self.root / "token"
        token.write_text("synthetic-private-owner-token-00000000000000")
        token.chmod(0o600)
        config = {"bus": "general", "broker_database": str(self.db), "admin_token_file": str(token), "server_id": "wrong-server"}
        with patch.object(hosted.HostedBroker, "owner") as owner, self.assertRaisesRegex(RuntimeError, "reviewed broker"):
            hosted.HostedBroker(config, "run")
        owner.assert_not_called()

    def test_bootstrap_preserves_immediate_configure_request(self):
        files = {name: base64.b64encode(Path(__file__).with_name(name).read_bytes()).decode() for name in hosted.FILES}
        message = {"id": 19, "method": "configure", "params": {"spec": {"origin": hosted.ORIGIN, "provider": "invalid", "timeout": 30}}}
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-u", "-c", hosted.BOOTSTRAP], input=json.dumps(files) + "\n" + json.dumps(message) + "\n",
                                text=True, capture_output=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 1)
        response = json.loads(result.stdout)
        self.assertEqual(response["id"], 19)
        self.assertFalse(response["ok"])
        self.assertIn("invalid provider", response["error"])

    def test_worker_refuses_alternate_origin_before_leasing_home(self):
        worker = hosted.HostedWorker(self.root)
        with patch.object(hosted.fleet.DeviceWorker, "configure") as parent, self.assertRaisesRegex(RuntimeError, "reviewed hosted"):
            worker.configure({"origin": "https://unrelated.invalid"})
        parent.assert_not_called()

    def test_started_and_completed_events_count_once(self):
        item = {"id": "call1", "type": "mcpToolCall", "tool": "bus_register", "arguments": {"name": "fixture"}}
        events = [{"method": kind, "params": {"item": item}} for kind in ("item/started", "item/completed", "item/completed")]
        self.assertEqual(hosted.completed_calls(events), [("bus_register", {"name": "fixture"})])

    def test_hosted_approvals_preserve_exact_origin_and_session(self):
        params = {"threadId": "verified", "serverName": "communicate", "mode": "form",
                  "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {"hub": hosted.ORIGIN}}}
        self.assertTrue(hosted.authorize_hosted_tool(params, "verified", "fixture", tool="bus_status", bus="general"))
        params["_meta"]["tool_params"]["hub"] = "https://other.invalid"
        self.assertFalse(hosted.authorize_hosted_tool(params, "verified", "fixture", tool="bus_status", bus="general"))
        params["_meta"]["tool_params"] = {"kind": "codex", "session": "verified", "name": "fixture", "hub": hosted.ORIGIN}
        self.assertTrue(hosted.authorize_hosted_tool(params, "verified", "fixture", tool="bus_register", bus="general"))
        self.assertFalse(hosted.authorize_hosted_tool(params, "verified", "fixture", tool="bus_register", bus="private-fixture"))
        params["_meta"]["tool_params"]["bus"] = "private-fixture"
        self.assertTrue(hosted.authorize_hosted_tool(params, "verified", "fixture", tool="bus_register", bus="private-fixture"))
        self.assertFalse(hosted.authorize_hosted_tool(params, "different-session", "fixture", tool="bus_register", bus="private-fixture"))
        params["_meta"]["tool_params"] = {"name": "fixture", "bus": "private-fixture", "hub": hosted.ORIGIN}
        self.assertTrue(hosted.authorize_hosted_tool(params, "verified", "fixture", tool="bus_register", bus="private-fixture"))
        params["_meta"]["tool_params"]["target"] = "some-other-session"
        self.assertFalse(hosted.authorize_hosted_tool(params, "verified", "fixture", tool="bus_register", bus="private-fixture"))

    def model_worker(self, bus="approved-fixture"):
        worker = hosted.HostedWorker(self.root)
        worker.kind, worker.timeout, worker.name = "codex", 1, "fixture"
        worker.bus_name, worker.origin, worker.principal = bus, hosted.ORIGIN, "owned"
        worker.enrolled = True
        records, models = {}, []
        worker.bus = SimpleNamespace(registrations=lambda: records)
        def codex(label, session=None):
            class Model:
                def __init__(self):
                    self.session, self.events, self.prompts, self.approvals = "verified", [], [], []
                    self.process = SimpleNamespace(poll=lambda: 0)
                def prompt(self, prompt, **kwargs):
                    self.prompts.append(prompt)
                    calls = []
                    if "Check which COM8" in prompt:
                        calls = [("bus_status", {})]
                    if "Make this existing agent" in prompt:
                        calls = [("bus_status", {}), ("bus_register", {"bus": bus, "session": "verified"})]
                        records["session"] = {"id": "own-agent", "session_key": "codex:verified", "url": hosted.ORIGIN, "buses": [bus]}
                    for index, (name, args) in enumerate(calls):
                        self.events.append({"method": "item/completed", "params": {"item": {"id": str(index), "type": "mcpToolCall", "tool": name, "arguments": args}}})
                def close(self):
                    pass
                def wait_turn(self):
                    pass
            model = Model()
            worker.current = model
            worker.active.append(model)
            models.append(model)
            return model
        worker.codex = codex
        return worker, models

    def test_natural_registration_uses_installed_workflow_without_tool_recipe(self):
        worker, models = self.model_worker()
        record = worker.register()
        self.assertEqual(record["session_key"], "codex:verified")
        self.assertIn("ordinary-language", record["instruction"])
        for model in models:
            for prompt in model.prompts:
                self.assertNotIn("bus_register", prompt)
                self.assertNotIn("bus_status", prompt)

    def test_general_hidden_seed_never_registers(self):
        worker, models = self.model_worker("general")
        record = worker.register(hidden=True)
        self.assertIsNone(record["id"])
        self.assertEqual(len(models), 1)
        self.assertFalse(any(name.endswith("bus_register") for name, _ in hosted.completed_calls(models[0].events)))

    def context_worker(self):
        worker = hosted.HostedWorker.__new__(hosted.HostedWorker)
        worker.home, worker.evidence = self.root / "home", self.root / "evidence"
        worker.evidence.mkdir()
        worker.session = "b4b11bbd-a8ce-4227-9817-20366e7b3b9f"
        worker.active = [SimpleNamespace(process=SimpleNamespace(poll=lambda: 0))]
        path = worker.home / ".codex/plugins/cache/communicate/communicate/fixture/.mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mcpServers": {"communicate": {"command": "node", "args": ["installed.mjs", "serve"],
            "env": {"HOME": str(worker.home), "COMM_STATE": str(worker.home / "state")}}}}))
        return worker, path

    def test_verified_fixture_context_changes_only_cache_env_and_restores_exact_bytes(self):
        worker, path = self.context_worker()
        original = path.read_bytes()
        worker.codex_mcp_context(worker.session)
        expected = json.loads(original)
        expected["mcpServers"]["communicate"]["env"]["CODEX_THREAD_ID"] = worker.session
        self.assertEqual(json.loads(path.read_bytes()), expected)
        worker.codex_mcp_context(worker.session)
        worker.restore_codex_mcp_context()
        self.assertEqual(path.read_bytes(), original)
        worker.restore_codex_mcp_context()

    def test_fixture_context_refuses_wrong_session_home_or_live_provider(self):
        worker, path = self.context_worker()
        original = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unverified"):
            worker.codex_mcp_context("b4b11bbd-a8ce-4227-9817-20366e7b3b9a")
        worker.active = [SimpleNamespace(process=SimpleNamespace(poll=lambda: None))]
        with self.assertRaisesRegex(RuntimeError, "close prior"):
            worker.codex_mcp_context(worker.session)
        worker.active = []
        descriptor = json.loads(original)
        descriptor["mcpServers"]["communicate"]["env"]["HOME"] = "/another-home"
        path.write_text(json.dumps(descriptor))
        with self.assertRaisesRegex(RuntimeError, "another HOME"):
            worker.codex_mcp_context(worker.session)

    def test_fixture_context_never_overwrites_an_external_cache_edit(self):
        worker, path = self.context_worker()
        worker.codex_mcp_context(worker.session)
        path.write_text("external-edit")
        with self.assertRaisesRegex(RuntimeError, "changed unexpectedly"):
            worker.restore_codex_mcp_context()
        self.assertEqual(path.read_text(), "external-edit")

    def test_hidden_send_reports_failed_tool_before_adapter_lookup(self):
        worker, _ = self.model_worker("general")
        worker.session, worker.registration = "verified", {"id": None}
        events = [{"method": "item/completed", "params": {"item": {"type": "mcpToolCall", "tool": "bus_send",
            "arguments": {"target": "peer", "message": "fixture"}, "status": "failed", "error": None}}}]
        model = SimpleNamespace(events=events, prompt=lambda *a, **kw: None, wait_turn=lambda: None)
        worker.codex = lambda *a: model
        with patch.object(worker, "own_registration") as lookup, self.assertRaisesRegex(RuntimeError, "tool call failed"):
            worker.send_hidden("peer", "fixture")
        lookup.assert_not_called()

    def test_command_preserves_parse_contract_and_restores_only_at_uninstall(self):
        worker = hosted.HostedWorker.__new__(hosted.HostedWorker)
        with patch.object(hosted.fleet.DeviceWorker, "command", return_value={"configured": False}) as command, \
                patch.object(worker, "restore_codex_mcp_context") as restore:
            self.assertEqual(worker.command(["bus", "status"], "status-before-enrollment", parse=True), {"configured": False})
            command.assert_called_once_with(["bus", "status"], "status-before-enrollment", parse=True)
            restore.assert_not_called()
            worker.command(["uninstall", "--purge"], "uninstall")
            restore.assert_called_once_with()
            command.assert_called_with(["uninstall", "--purge"], "uninstall", parse=False)

    def test_extra_rejected_send_still_invalidates_model_proof(self):
        def calls(send, reply, register):
            return {"calls": [(tool, {}) for tool, count in (("bus_send", send), ("bus_reply", reply), ("bus_register", register))
                              for _ in range(count)]}
        snapshots = {"claude": calls(1, 1, 2), "codex": calls(2, 1, 1)}
        hosted.verify_model_counts("private", snapshots)
        snapshots["claude"]["calls"].append(("bus_send", {"target": "unknown-rejected-target"}))
        with self.assertRaisesRegex(RuntimeError, "unexpected model"):
            hosted.verify_model_counts("private", snapshots)
        hosted.verify_model_counts("general", {"claude": calls(0, 1, 1), "codex": calls(2, 0, 0)})

    def test_failed_constructor_confirms_owned_ssh_group_shutdown(self):
        class BrokenInput(io.StringIO):
            def write(self, value):
                raise BrokenPipeError()
        process = SimpleNamespace(stdin=BrokenInput(), stdout=io.StringIO(""))
        spec = {"provider": "claude", "ssh": "fixture-never-contacted"}
        with patch.object(hosted.subprocess, "Popen", return_value=process), \
                patch.object(hosted.fleet, "stop_owned_group", return_value=True) as stop:
            with self.assertRaises(BrokenPipeError):
                hosted.HostedRemote(spec, self.root, 1, {})
            stop.assert_called_once_with(process)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_transport_selection_and_local_agent_socket_removal(self):
        for transport in (None, "ssh", "local"):
            with self.subTest(transport=transport):
                spec = {"provider": "codex", "ssh": "fixture-never-contacted", "python": sys.executable}
                if transport is not None:
                    spec["transport"] = transport
                env = {"HOME": str(self.root), "PATH": "/usr/bin:/bin", "SSH_AUTH_SOCK": "private-agent", "SSH_AGENT_PID": "999"}
                process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(""))
                evidence = self.root / str(transport)
                evidence.mkdir()
                with patch.object(hosted.subprocess, "Popen", return_value=process) as launch:
                    worker = hosted.HostedRemote(spec, evidence, 1, env)
                worker.reader.join(timeout=2)
                args, kwargs = launch.call_args
                self.assertTrue(kwargs["start_new_session"])
                if transport == "local":
                    self.assertEqual(args[0], [sys.executable, "-u", "-c", hosted.BOOTSTRAP])
                    self.assertNotIn("SSH_AUTH_SOCK", kwargs["env"])
                    self.assertNotIn("SSH_AGENT_PID", kwargs["env"])
                else:
                    self.assertEqual(args[0][:1 + len(hosted.fleet.SSH_OPTIONS)], ["ssh", *hosted.fleet.SSH_OPTIONS])
                    self.assertEqual(args[0][-2], spec["ssh"])
                    self.assertEqual(kwargs["env"], env)
                self.assertEqual(set(json.loads(process.stdin.getvalue())), set(hosted.FILES))
                process.stdin.close(); process.stdout.close(); worker.error_file.close()

    def test_local_worker_real_bootstrap_eof_cleanup(self):
        spec = {"provider": "codex", "transport": "local", "python": sys.executable, "ssh": "unused-placeholder"}
        env = {"HOME": str(self.root), "PATH": "/usr/bin:/bin", "TMPDIR": str(self.root),
               "SSH_AUTH_SOCK": "must-not-be-inherited", "SSH_AGENT_PID": "999", "PYTHONDONTWRITEBYTECODE": "1"}
        worker = hosted.HostedRemote(spec, self.root, 1, env)
        # No configure/authentication/provider action; worker starts and exits
        # through the exact production EOF/finally path.
        worker.close()
        self.assertTrue(worker.fleet_group_stopped)
        self.assertEqual(worker.process.returncode, 0)
        self.assertFalse(list(self.root.glob("com8-fleet-*")))

    def test_invalid_local_transport_refused_before_process(self):
        for spec in ({"transport": "automatic"}, {"transport": "local", "python": "python3"}):
            with self.subTest(spec=spec), patch.object(hosted.subprocess, "Popen") as launch, self.assertRaises(RuntimeError):
                hosted.HostedRemote(spec, self.root, 1, {})
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
