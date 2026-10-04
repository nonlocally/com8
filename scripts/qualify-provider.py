#!/usr/bin/env python3
"""Opt-in real-provider proof against an extracted COM8 artifact.

Uses existing provider authentication without copying or printing credentials.
Creates disposable broker state and a fresh provider conversation. By default,
Claude loads the artifact plugin and Codex receives explicit artifact MCP flags.
With --installed, setup registers the plugin in the isolated client HOME and
the client must discover that installation without an MCP executable override.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.dont_write_bytecode = True


def require(value, message):
    if not value:
        raise RuntimeError(message)


def artifact(root):
    require(not (root / ".git").exists(), "use an extracted release, not a checkout")
    manifest = json.loads((root / "release.json").read_text())
    for path in root.rglob("*"):
        if path.is_symlink():
            require(path.resolve().is_relative_to(root) and path.exists(), "unsafe artifact symlink")
    actual = {str(path.relative_to(root)) for path in root.rglob("*")
              if path.is_file() and not path.is_symlink() and path != root / "release.json"}
    require(actual == set(manifest["files"]), "artifact file inventory mismatch")
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        require(path.is_relative_to(root), "artifact path escapes runtime")
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                "artifact checksum mismatch: " + name)
    require(manifest.get("source"), "artifact source revision missing")
    return manifest


def import_artifact_bus(runtime):
    # Child environment variables do not affect this controller interpreter.
    # Never write __pycache__ into the checksum-qualified runtime.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(runtime / "vendor/lib"))
    spec = importlib.util.spec_from_file_location("com8_artifact_bus", runtime / "vendor/lib/bus.py")
    bus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bus)
    return bus


def private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")


class Provider:
    def __init__(self, command, env, cwd, evidence, label):
        self.events = []
        self.outputs = [private_file(evidence / (label + suffix))
                        for suffix in (".events.jsonl", ".stderr.log")]
        self.process = subprocess.Popen(command, env=env, cwd=cwd, text=True,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)

        def collect(stream, output, events=False):
            for line in stream:
                output.write(line)
                output.flush()
                if events:
                    try:
                        self.events.append(json.loads(line))
                    except ValueError:
                        pass

        self.readers = [threading.Thread(target=collect, args=(self.process.stdout, self.outputs[0], True), daemon=True),
                        threading.Thread(target=collect, args=(self.process.stderr, self.outputs[1]), daemon=True)]
        for reader in self.readers:
            reader.start()

    def prompt(self, text, streaming=False):
        data = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) if streaming else text
        self.process.stdin.write(data + "\n")
        self.process.stdin.flush()
        if not streaming:
            self.process.stdin.close()

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)
        for stream in [self.process.stdin, self.process.stdout, self.process.stderr, *self.outputs]:
            if not stream.closed:
                stream.close()


def tool_calls(events):
    """Read actual tool-call records, never an assistant's prose claim."""
    found = []
    for event in events:
        for block in event.get("message", {}).get("content", []) if isinstance(event.get("message"), dict) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found.append((block.get("name", ""), block.get("input", {})))
        item = event.get("item", event.get("params", {}).get("item", {}))
        if isinstance(item, dict) and item.get("type") in ("mcp_tool_call", "mcpToolCall"):
            arguments = item.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            found.append((item.get("tool", ""), arguments))
    return found


def pending_approval_tool(events, params):
    pending = {}
    for event in events:
        context = event.get("params", {})
        item = context.get("item", {})
        if (context.get("threadId") != params.get("threadId") or context.get("turnId") != params.get("turnId")
                or item.get("type") != "mcpToolCall"):
            continue
        if event.get("method") == "item/started":
            pending[item["id"]] = item
        elif event.get("method") == "item/completed":
            pending.pop(item["id"], None)
    matches = [item for item in pending.values() if item.get("server") == "communicate"
               and item.get("pluginId") == "communicate@communicate"
               and item.get("arguments") == (params.get("_meta") or {}).get("tool_params", {})]
    return matches[0].get("tool") if len(matches) == 1 else None


def authorize_fixture_tool(params, session, name, reply=None, tool=None, outgoing=None):
    """One-call authorization, restricted to this fixture's exact participants."""
    meta = params.get("_meta") or {}
    if (params.get("threadId") != session or params.get("serverName") != "communicate"
            or params.get("mode") not in ("form", "openai/form")
            or meta.get("codex_approval_kind") != "mcp_tool_call"):
        return False
    if not tool or (meta.get("tool_name") and meta["tool_name"] != tool):
        return False
    arguments = meta.get("tool_params", {})
    if not isinstance(arguments, dict):
        return False
    if tool == "bus_status":
        return not arguments
    if tool == "bus_register":
        return (arguments.get("session") == session and arguments.get("kind") == "codex"
                and arguments.get("name") == name and arguments.get("bus", "general") == "general"
                and arguments.get("target", "self") == "self"
                and set(arguments) <= {"session", "kind", "name", "bus", "target", "description"})
    if tool == "bus_reply" and reply:
        return (arguments.get("id") == reply["id"] and arguments.get("message") == reply["payload"]
                and arguments.get("from") == reply["recipient"] and arguments.get("hub") in (None, "", reply.get("hub"))
                and set(arguments) <= {"id", "message", "from", "hub"})
    if tool == "bus_send" and outgoing:
        return (arguments.get("target") == outgoing["target"] and arguments.get("from") == outgoing["sender"]
                and arguments.get("message") == outgoing["message"] and arguments.get("bus", "general") == "general"
                and arguments.get("hub") in (None, "", outgoing["hub"])
                and set(arguments) <= {"target", "message", "from", "hub", "bus"})
    return False


class CodexAppServer:
    """Real installed plugin discovery with a narrowly scoped approval client."""
    def __init__(self, executable, env, cwd, evidence, label, name, timeout, profile=None, allow_send=False):
        self.events, self.approvals, self.pending = [], [], {}
        self.sequence, self.session, self.turn, self.reply = 0, None, None, None
        self.outgoing = None
        self.name, self.timeout, self.cwd = name, timeout, str(cwd)
        self.write_lock = threading.Lock()
        self.outputs = [private_file(evidence / (label + suffix))
                        for suffix in (".events.jsonl", ".stderr.log", ".requests.jsonl")]
        prefix = 'plugins."communicate@communicate".mcp_servers.communicate'
        allowed = ["bus_status", "bus_register", "bus_reply"] + (["bus_send"] if allow_send else [])
        command = [executable, "app-server", "-c", prefix + '.enabled_tools=' + json.dumps(allowed),
                   "-c", "features.shell_tool=false", "-c", "web_search=\"disabled\""]
        if profile:
            command += ["-c", "profile=" + json.dumps(profile)]
        self.process = subprocess.Popen(command, env=env, cwd=cwd, text=True, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        def collect():
            for line in self.process.stdout:
                self.outputs[0].write(line); self.outputs[0].flush()
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self.events.append(message)
                if "method" in message and "id" in message:
                    params = message.get("params", {})
                    tool = pending_approval_tool(self.events, params)
                    accepted = (message["method"] == "mcpServer/elicitation/request"
                                and authorize_fixture_tool(params, self.session, self.name, self.reply, tool, self.outgoing))
                    self.approvals.append({"method": message["method"], "tool": tool,
                                           "accepted": accepted})
                    if message["method"] == "mcpServer/elicitation/request":
                        self.send({"id": message["id"], "result": {"action": "accept" if accepted else "decline", "content": None}})
                    else:
                        self.send({"id": message["id"], "error": {"code": -32601, "message": "Request outside fixture authorization"}})
                elif "id" in message and message["id"] in self.pending:
                    self.pending[message["id"]].put(message)

        def errors():
            for line in self.process.stderr:
                self.outputs[1].write(line); self.outputs[1].flush()

        self.readers = [threading.Thread(target=collect, daemon=True), threading.Thread(target=errors, daemon=True)]
        for reader in self.readers:
            reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "com8_qualification", "version": "0.3.0"},
                                     "capabilities": {"experimentalApi": True, "mcpServerOpenaiFormElicitation": True}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def send(self, message):
        with self.write_lock:
            line = json.dumps(message) + "\n"
            self.outputs[2].write(line); self.outputs[2].flush()
            self.process.stdin.write(line); self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        identifier = "com8-" + str(self.sequence)
        waiter = self.pending[identifier] = queue.Queue()
        self.send({"id": identifier, "method": method, "params": params})
        try:
            response = waiter.get(timeout=self.timeout)
            require("error" not in response, "Codex app-server " + method + " failed; inspect private evidence")
            return response["result"]
        finally:
            self.pending.pop(identifier, None)

    def thread(self, session=None):
        params = {"cwd": self.cwd, "sandbox": "read-only", "approvalsReviewer": "user",
                  "approvalPolicy": {"granular": {"mcp_elicitations": True, "rules": False,
                                                   "sandbox_approval": False, "request_permissions": False, "skill_approval": False}}}
        if session:
            params["threadId"] = session
            # Resume may immediately dispatch a queued turn before the response
            # arrives. Its approval context is the already verified exact ID.
            self.session = session
        result = self.call("thread/resume" if session else "thread/start", params)
        self.session = result["thread"]["id"]
        require(not session or self.session == session, "Codex app-server resumed a different session")
        return self.session

    def prompt(self, text, streaming=False):
        self.turn = self.call("turn/start", {"threadId": self.session, "input": [{"type": "text", "text": text}]})["turn"]["id"]

    def wait_turn(self):
        def completed():
            require(self.process.poll() is None, "Codex app-server exited during turn")
            return next((event["params"]["turn"] for event in self.events if event.get("method") == "turn/completed"
                         and event.get("params", {}).get("turn", {}).get("id") == self.turn), None)
        turn = wait_for(completed, self.timeout, "Codex app-server turn completion")
        require(turn.get("status") == "completed", "Codex app-server turn failed; inspect private evidence")

    def finished(self):
        return any(event.get("method") == "turn/completed" and event.get("params", {}).get("turn", {}).get("id") == self.turn
                   for event in self.events)

    def close(self):
        Provider.close(self)


def wait_for(check, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.2)
    raise RuntimeError("timed out: " + description)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    parser.add_argument("--client-home", type=Path, required=True,
                        help="preconfigured isolated HOME with private provider auth; never the real account home")
    parser.add_argument("--evidence", type=Path, required=True, help="new private directory; never publish raw logs")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--codex-profile", help="existing authenticated provider profile; no credentials are copied")
    parser.add_argument("--run-live", action="store_true", help="explicitly permit real model requests")
    parser.add_argument("--installed", action="store_true",
                        help="qualify real client discovery after setup in the isolated HOME")
    args = parser.parse_args()
    require(args.run_live, "UNQUALIFIED: --run-live is required; this test makes real provider requests")
    require(args.timeout >= 30, "timeout must be at least 30 seconds")
    runtime = args.runtime.resolve(strict=True)
    manifest = artifact(runtime)
    executable = shutil.which(args.provider)
    require(executable, "UNQUALIFIED: provider executable missing")
    client_home = args.client_home.resolve(strict=True)
    require(client_home.is_dir() and client_home != Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(),
            "a prepared isolated client home is required; live account configuration is never used")
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = args.evidence.resolve()
    report = {"status": "fail", "provider": args.provider, "source": manifest["source"],
              "version": manifest["version"], "runtime": str(runtime), "evidence": str(evidence),
              "scope": "fresh Claude artifact plugin" if args.provider == "claude" else "fresh Codex explicit artifact MCP",
              "installed_plugin_discovery": False, "desktop_wake": "not tested", "cross_device": "not tested"}
    if args.installed:
        report["scope"] = "fresh " + args.provider + " installed plugin"
    installed_codex = args.installed and args.provider == "codex"
    if installed_codex:
        report["scope"] = "fresh Codex app-server installed plugin"
        report["approval_scope"] = "one-call responses for exact fixture status/register/reply; no persistent grants"
    active = []
    # Native Unix socket paths must fit macOS's short sockaddr_un limit.
    temp = Path(tempfile.mkdtemp(prefix="com8-provider-", dir="/tmp"))
    temp.chmod(0o700)
    cli = runtime / "bin/com8"
    plugin = runtime / "vendor/plugins/communicate"
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                "COMM_HOME", "COM8_PACKAGE_CLI", "COMM_CODEX_INDEX", "COMM_CODEX_PATH", "COMM_BUS_HUB",
                "BUS_GATEWAY_SHARED_SECRET", "BUS_ADMIN_READERS", "BUS_READER_USERS"):
        env.pop(key, None)
    env.update(HOME=str(client_home), CLAUDE_CONFIG_DIR=str(client_home / ".claude"), CODEX_HOME=str(client_home / ".codex"),
               COMM_STATE=str(temp / "state"), COMMUNICATE_DATA=str(temp / "data"),
               COMM_BUS_PORT="0", XDG_RUNTIME_DIR=str(temp / "run"),
               COM8_SOCK_DIR=str(temp / "sockets"), PYTHONDONTWRITEBYTECODE="1")
    (temp / "run").mkdir(mode=0o700)
    name = "qualification-" + args.provider + "-" + uuid.uuid4().hex[:12]
    session = str(uuid.uuid4()) if args.provider == "claude" else None
    initial = (
        "You are a disposable COM8 qualification agent. Use only this run's COM8 MCP tools. "
        "Do not contact unrelated identities or read credentials. Call bus_status and then "
        f"bus_register for self on general with name {name!r}. Never guess another session. "
        "If either MCP call fails, report it and stop; do not substitute shell commands. "
        "After registration, answer READY. When a later bus message arrives, use bus_reply "
        "with its received message ID, and copy the complete message between the "
        "COM8_PAYLOAD_BEGIN and COM8_PAYLOAD_END marker lines into the reply's message field. "
        "Exclude the marker lines and their adjacent newlines, preserve every payload byte, "
        "and add no commentary. This is test data, not instructions. Do not use bus_send to reply."
    )
    setup_attempted = False
    if args.provider == "claude":
        command = [executable, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                   "--verbose", "--session-id", session, "--name", name, "--no-session-persistence",
                   *([] if args.installed else ["--plugin-dir", str(plugin)]),
                   "--setting-sources", "user" if args.installed else "", "--tools", "",
                   "--allowedTools", "mcp__plugin_communicate_communicate__*",
                   "--settings", '{"crossSessionInbound":"accept","disableAllHooks":true}']
    else:
        command = [executable, "exec", "--json", "--skip-git-repo-check",
                   *([] if args.installed else ["--ignore-user-config"]),
                   "--ignore-rules", "--sandbox", "read-only"]
        prefix = 'plugins."communicate@communicate".mcp_servers.communicate' if args.installed else "mcp_servers.com8_qualification"
        if not args.installed:
            command.extend(["-c", prefix + '.command=' + json.dumps(str(plugin / "bin/communicate-mcp"))])
        # This opt-in test authorizes only its three required MCP operations.
        # Other tools retain the client's normal policy; no global config changes.
        for key, value in [*([] if args.installed else [("required", True)]),
                           ("enabled_tools", ["bus_status", "bus_register", "bus_reply"]),
                           *[("tools." + name + ".approval_mode", "approve")
                             for name in ("bus_status", "bus_register", "bus_reply")],
                           *([] if args.installed else [("env." + key, env[key]) for key in
                             ("COMM_STATE", "COMMUNICATE_DATA", "COMM_BUS_PORT", "XDG_RUNTIME_DIR", "COM8_SOCK_DIR",
                              "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PYTHONDONTWRITEBYTECODE")])]:
            command.extend(["-c", prefix + "." + key + "=" + json.dumps(value)])
        if args.codex_profile:
            command.extend(["--profile", args.codex_profile])
        command.append("-")
    try:
        version = subprocess.run([executable, "--version"], env=env, text=True, capture_output=True, timeout=15)
        require(version.returncode == 0, "provider version inspection failed")
        report["provider_version"] = version.stdout.strip().splitlines()[0]
        if args.installed:
            setup_attempted = True
            setup = subprocess.run([str(cli), "setup", "--" + args.provider, "--no-service"],
                                   env=env, cwd=temp, text=True, capture_output=True, timeout=args.timeout)
            with private_file(evidence / "setup.log") as out:
                out.write(setup.stdout + setup.stderr)
            require(setup.returncode == 0, "installed plugin setup failed; inspect private evidence")
        if installed_codex:
            seed = CodexAppServer(executable, env, temp, evidence, "seed", name, args.timeout, args.codex_profile)
            active.append(seed)
            session = seed.thread()
            seed.prompt("You are a disposable COM8 qualification agent. Call only bus_status, then answer READY. "
                        "Do not register yet, run shell commands, or contact another identity.")
            seed.wait_turn()
            require(any(event.get("method") == "item/completed" and event.get("params", {}).get("item", {}).get("tool") == "bus_status"
                        and event["params"]["item"].get("status") == "completed" for event in seed.events),
                    "Codex seed did not successfully call the installed MCP")
            seed.close()
            process = CodexAppServer(executable, env, temp, evidence, "initial", name, args.timeout, args.codex_profile)
            active.append(process)
            process.thread(session)
            initial += (" Your exact existing Codex session, verified from thread/start, is " + session +
                        ". Pass kind=codex and session=" + session + " to bus_register; do not infer self. "
                        "When replying, pass the received recipient registration ID explicitly as from.")
            report["identity_context"] = "verified app-server thread/start ID, exact-session thread/resume"
        elif args.provider == "codex":
            # Codex does not expose its new thread ID to a stdio MCP child at
            # startup. Obtain the actual ID from the CLI event, then resume
            # that same conversation with explicit, verified identity context.
            seed = Provider(command, env, temp, evidence, "seed")
            active.append(seed)
            seed.prompt("You are a disposable COM8 qualification agent. Call only bus_status, then answer READY. "
                        "Do not register yet, run shell commands, or contact another identity.")
            seed.process.wait(timeout=args.timeout)
            require(seed.process.returncode == 0, "Codex identity seed failed; inspect private evidence")
            session = wait_for(lambda: next((e.get("thread_id") for e in seed.events
                                            if e.get("type") == "thread.started"), None), 10, "Codex thread ID")
            require(any(n.endswith("bus_status") for n, _ in tool_calls(seed.events)),
                    "Codex seed did not call the artifact MCP")
            command = command[:-1] + ([] if args.installed else
                                     ["-c", "mcp_servers.com8_qualification.env.CODEX_THREAD_ID=" + json.dumps(session)]) + ["resume", session, "-"]
            if args.installed:
                initial += (" Your exact existing Codex session, verified from thread.started, is " + session +
                            ". Pass kind=codex and session=" + session + " to bus_register; do not infer self.")
            report["identity_context"] = "verified CLI thread.started ID, exact-session resume"
        if not installed_codex:
            process = Provider(command, env, temp, evidence, "initial")
            active.append(process)
        process.prompt(initial, streaming=args.provider == "claude")
        registrations = temp / "state/bus/registrations.json"

        def registered():
            if not registrations.is_file():
                require(process.process.poll() is None, "provider exited before exact registration; inspect private evidence")
                require(not installed_codex or not process.finished(), "provider turn ended before exact registration; inspect private evidence")
                return None
            records = json.loads(registrations.read_text())
            found = next((r for r in records.values() if r.get("name") == name), None)
            require(found or not installed_codex or not process.finished(), "provider turn ended without exact registration; inspect private evidence")
            return found

        registration = wait_for(registered, args.timeout, "MCP exact-session registration")
        if args.provider == "codex" and not installed_codex:
            resumed_session = wait_for(lambda: next((e.get("thread_id") for e in process.events
                                                     if e.get("type") == "thread.started"), None), 10, "resumed Codex thread ID")
            require(resumed_session == session, "Codex registration ran in a different conversation")
        require(registration.get("session_key") == args.provider + ":" + session,
                "registration does not belong to the fresh provider session")
        wait_for(lambda: any(n.endswith("bus_register") for n, _ in tool_calls(process.events)), 10,
                 "actual bus_register MCP tool call")
        cfg = json.loads((temp / "state/bus/client.json").read_text())
        owner = cfg["connections"][cfg["default"]]
        require(owner.get("local") and owner["url"].startswith("http://127.0.0.1:"), "fixture selected a nonlocal broker")
        bus = import_artifact_bus(runtime)
        invitation = bus.request(owner, "invite", bus="general", ttl=600)["invite"]
        device = bus.request({"url": owner["url"]}, "redeem", invite=invitation, device="qualification-controller-fixture")
        control = {"url": owner["url"], "token": device["token"]}
        sender = bus.request(control, "identify", session_key="fixture:" + uuid.uuid4().hex,
                             name="controller-fixture-not-a-model", kind="claude", status="offline")
        nonce = uuid.uuid4().hex
        payload = "nonce=" + nonce + "\n" + "\n".join(f"{i:03d}|{uuid.uuid4().hex}|literal $HOME `id` --from \\\" '" for i in range(112))
        challenge = "Reply with the complete enclosed payload, preserving every byte.\nCOM8_PAYLOAD_BEGIN\n" + payload + "\nCOM8_PAYLOAD_END"
        if installed_codex:
            # This phase proves dormant-session delivery. If we enqueue while
            # registration is still running, Codex can start the queued turn
            # as soon as registration ends; closing then interrupts delivery.
            process.wait_turn()
            process.close()
            require(process.process.poll() is not None, "Codex registration process is still running before queueing")
        sent = bus.request(control, "send", sender=sender["id"], target=registration["id"], bus="general", message=challenge)
        terminal = "delivered" if args.provider == "claude" else "queued"

        def delivered():
            receipt = bus.request(control, "receipt", id=sent["id"])
            require(receipt["status"] not in ("failed", "cancelled", "expired"), "delivery failed; no model-consumption claim")
            return receipt if receipt["status"] == terminal else None

        receipt = wait_for(delivered, args.timeout, "provider endpoint delivery")
        if installed_codex:
            process = CodexAppServer(executable, env, temp, evidence, "resume", name, args.timeout, args.codex_profile)
            active.append(process)
            process.reply = {"id": sent["id"], "payload": payload, "recipient": registration["id"], "hub": owner["url"]}
            process.thread(session)
            process.prompt("Consume the queued COM8 message and reply through bus_reply exactly as previously instructed. "
                           "Do not start a new conversation. Set from=" + registration["id"] + ".")
        elif args.provider == "codex":
            process.process.wait(timeout=args.timeout)
            require(process.process.returncode == 0, "initial Codex turn failed; inspect private evidence")
            process = Provider(command, env, temp, evidence, "resume")
            active.append(process)
            process.prompt("Consume the queued COM8 message and reply through bus_reply exactly as previously instructed. Do not start a new conversation.")

        def answered():
            messages = bus.request(control, "poll", agent=sender["id"])["messages"]
            require(messages or not installed_codex or not process.finished(),
                    "provider turn ended without a correlated reply; inspect private evidence")
            return messages[0] if messages else None

        answer = wait_for(answered, args.timeout, "correlated model reply")
        require(answer["sender"]["id"] == registration["id"] and answer["target"] == sender["id"], "reply participants changed")
        require(answer["message"] == payload, "reply changed or truncated the long payload")
        calls = [call for p in active for call in tool_calls(p.events)]
        require(any(n.endswith("bus_reply") and a.get("id") == sent["id"] for n, a in calls if isinstance(a, dict)),
                "model did not call bus_reply with the original received ID")
        databases = list((temp / "state/bus").rglob("bus.sqlite3"))
        require(len(databases) == 1, "isolated broker database not uniquely identified")
        with sqlite3.connect("file:" + str(databases[0]) + "?mode=ro", uri=True) as db:
            conversations = db.execute("SELECT conversation FROM messages WHERE id IN (?,?)", (sent["id"], answer["id"])).fetchall()
        require(len(conversations) == 2 and conversations[0] == conversations[1], "reply started a different conversation")
        bus.request(control, "ack", agent=sender["id"], id=answer["id"], lease=answer["lease"], status="delivered")
        artifact(runtime)
        report.update(status="pass", installed_plugin_discovery=args.installed,
                      exact_session=session, registration=registration["id"],
                      message=sent["id"], reply=answer["id"], endpoint_receipt=receipt["status"],
                      payload_bytes=len(payload.encode()), payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                      model_consumption="byte-exact correlated reply", controller="API fixture, not another model")
        if installed_codex:
            report["approvals"] = [approval for provider in active for approval in provider.approvals]
            require(all(item["accepted"] for item in report["approvals"]), "provider requested an operation outside the fixture authorization")
    except Exception as error:
        report["status"] = "fail"
        report["error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    finally:
        for process in active:
            process.close()
        try:
            stopped = subprocess.run([str(cli), "bus", "stop"], env=env, cwd=temp, capture_output=True, timeout=30)
            report["cleanup"] = "isolated broker stopped" if stopped.returncode == 0 else "stop failed"
            if stopped.returncode:
                report["status"] = "fail"
        except Exception:
            report.update(status="fail", cleanup="stop failed")
        if setup_attempted:
            try:
                removed = subprocess.run([str(cli), "uninstall", "--purge"],
                                         env=env, cwd=temp, text=True, capture_output=True, timeout=90)
                with private_file(evidence / "uninstall.log") as out:
                    out.write(removed.stdout + removed.stderr)
                report["integration_cleanup"] = "restored" if removed.returncode == 0 else "failed"
                if removed.returncode:
                    report["status"] = "fail"
            except Exception:
                report.update(status="fail", integration_cleanup="failed")
        shutil.rmtree(temp)
        with private_file(evidence / "report.json") as out:
            json.dump(report, out, indent=2)
            out.write("\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "unqualified", "error": str(error) if isinstance(error, RuntimeError) else type(error).__name__}))
        raise SystemExit(2)
