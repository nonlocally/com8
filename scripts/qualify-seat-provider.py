#!/usr/bin/env python3
"""Opt-in real Claude terminal acceptance through an installed COM8 runtime.

Uses an existing CLAUDE_CODE_OAUTH_TOKEN in memory, never copies credentials.
The fixture installs into a fresh HOME, owns one daemon/tmux server, and changes
no service manager or existing client configuration. Raw evidence is private.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
        json.dump(value, out, indent=2)
        out.write("\n")


def private_text(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
        out.write(value)


def wait_for(check, seconds, description):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.3)
    raise RuntimeError("timed out: " + description)


def transcript_rows(home, session):
    rows = []
    for path in (home / ".claude/projects").rglob(session + ".jsonl"):
        for line in path.read_text().splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue  # Writer may still be appending the last line.
            if value.get("sessionId") == session:
                rows.append(value)
    return rows


def text_blocks(row):
    content = row.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if block.get("type") == "text")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path, help="unchanged extracted release archive")
    parser.add_argument("--evidence", type=Path, required=True, help="new private evidence directory")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--launch", choices=("command", "cli"), default="command",
                        help="raw command or advertised --cli claude path with per-launch COM8_CLAUDE_CMD")
    parser.add_argument("--run-live", action="store_true", help="authorize two real provider turns")
    args = parser.parse_args()
    require(args.run_live, "UNQUALIFIED: --run-live is required for real provider requests")
    require(args.timeout >= 30, "timeout must be at least 30 seconds")
    require(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
            "UNQUALIFIED: supply an existing authorized Claude OAuth token in the environment")
    commands = {name: shutil.which(name) for name in ("claude", "tmux", "node", "python3", "bash")}
    require(all(commands.values()), "UNQUALIFIED: claude, tmux, node, python3 and bash are required")
    commands = {name: str(Path(path).resolve()) for name, path in commands.items()}
    helper = importlib.util.spec_from_file_location("artifact_checks", Path(__file__).with_name("qualify-installed.py"))
    checks = importlib.util.module_from_spec(helper)
    helper.loader.exec_module(checks)
    runtime = args.runtime.resolve(strict=True)
    artifact = checks.artifact(runtime)
    before = checks.files(runtime)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = args.evidence.resolve()
    root = Path(tempfile.mkdtemp(prefix="com8-seat-", dir="/tmp")).resolve()
    root.chmod(0o700)
    home, data, state, work = [root / name for name in ("home", "data", "state", "work")]
    for directory in (home, work, home / ".claude", home / ".codex", root / "run", root / "socks", root / "sessions"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    unique = uuid.uuid4().hex
    session, name, asker = str(uuid.uuid4()), "seat-" + unique[:12], "controller-" + unique[:12]
    socket_name = "com8-seat-" + unique
    env = {"HOME": str(home), "PATH": ":".join(dict.fromkeys([str(Path(p).parent) for p in commands.values()])) + ":/usr/bin:/bin:/usr/sbin:/sbin",
           "LANG": "en_US.UTF-8", "TERM": "xterm-256color", "SHELL": "/bin/sh",
           "COMMUNICATE_DATA": str(data), "COMM_STATE": str(state), "COM8_SELF": "seat-fixture",
           "COM8_TMUX_SOCKET": socket_name, "COM8_SEAT_SESSION": "seat-proof", "COM8_TICK": "1",
           "COM8_SOCK_DIR": str(root / "socks"), "COM8_SESSIONS_DIR": str(home / ".claude/sessions"),
           "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
           "XDG_RUNTIME_DIR": str(root / "run"), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
           "PYTHONDONTWRITEBYTECODE": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_OAUTH_TOKEN": os.environ["CLAUDE_CODE_OAUTH_TOKEN"]}
    report = {"status": "fail", "artifact": artifact, "provider": "claude",
              "home": str(home), "state": str(state), "tmux_socket": socket_name,
              "session": session, "identity": name, "checks": {},
              "scope": "installed durable identity and raw-command spawn of real Claude in an isolated tmux seat",
              "launch_selector": "raw command; --cli claude rename/adoption not tested",
              "installed_plugin_discovery": "not tested; explicit installed MCP command",
              "autonomous_mail_delivery": "not tested; controller forwards one saved request through seat send",
              "desktop_wake": "not tested", "cross_device": "not tested", "exact_resume": "not tested"}
    if args.launch == "cli":
        report.update(scope="installed durable identity and --cli claude spawn in an isolated tmux seat",
                      launch_selector="--cli claude with per-launch restricted COM8_CLAUDE_CMD",
                      autonomous_mail_delivery="requires native cross-session arrival; no manual fallback")
    daemon = ask = None
    seat = installed = installed_before = None
    tmux_started = setup_attempted = False
    daemon_log = None
    cli = runtime / "bin/com8"

    def run(argv, ok=True, timeout=30):
        result = subprocess.run([str(a) for a in argv], env=env, cwd=work,
                                text=True, capture_output=True, timeout=timeout)
        require(not ok or result.returncode == 0,
                "command failed (exit %s): %s" % (result.returncode, " ".join(map(str, argv[:4]))))
        return result

    def com8(*argv, **kw):
        return run([cli, *argv], **kw)

    def tmux(*argv, **kw):
        return run([commands["tmux"], "-L", socket_name, *argv], **kw)

    def rows():
        return transcript_rows(home, session)

    try:
        report["provider_version"] = run([commands["claude"], "--version"]).stdout.strip()
        setup_attempted = True
        setup = com8("setup", "--no-clients", "--no-service", timeout=120)
        private_text(evidence / "setup.log", setup.stdout + setup.stderr)
        cli = data / "bin/com8"
        installed = (data / "current").resolve(strict=True)
        require(checks.within(installed, data), "installed current points outside fixture")
        require(cli.resolve() == installed / "src/com8.mjs", "installed entry escapes release")
        report["entry_point"] = str(cli)
        installed_before = checks.files(installed)
        # Setup stages the package payload (the outer archive's bin wrappers and
        # manifest are not installed). Verify its source-stamped vendor manifest
        # against the already qualified archive and snapshot the staged tree.
        require((installed / "vendor/release.json").read_bytes() == (runtime / "vendor/release.json").read_bytes(),
                "installed vendor manifest differs from qualified input")
        dependency_hashes = {value["sha256"] for relative, value in before.items()
                             if relative.startswith("node_modules/") and "sha256" in value}
        for relative, value in installed_before.items():
            if "link" in value:
                require(checks.within(installed / relative, installed), "installed symlink escapes release")
            if "sha256" in value:
                if relative.startswith("node_modules/"):
                    require(value["sha256"] in dependency_hashes, "installed dependency bytes absent from archive: " + relative)
                else:
                    require((runtime / relative).is_file() and checks.sha(runtime / relative) == value["sha256"],
                            "installed file differs from archive: " + relative)
        report["installed"] = {"root": str(installed), "files": len(installed_before), "source": artifact["source"]}
        # Trust only the newly-created empty fixture workspace. No live config
        # is copied; built-in tools are disabled and all other MCP tools denied.
        private_json(home / ".claude/.claude.json", {"hasCompletedOnboarding": True, "theme": "dark",
                     "projects": {str(work): {"hasTrustDialogAccepted": True, "allowedTools": []}}})
        private_json(root / "mcp.json", {"mcpServers": {"seatproof": {
            "type": "stdio", "command": commands["node"],
            "args": [str(installed / "src/cli.mjs"), "serve"]}}})
        tmux("-f", "/dev/null", "new-session", "-d", "-s", "seat-proof", "-x", "180", "-y", "50", "/bin/sh")
        tmux_started = True
        tmux("set-option", "-g", "default-shell", "/bin/sh")
        provider = [commands["claude"], "--session-id", session, "--name", name,
                    "--setting-sources", "", "--tools", "", "--permission-mode", "dontAsk",
                    "--allowedTools", "mcp__seatproof__com8_reply", "--strict-mcp-config",
                    "--mcp-config", str(root / "mcp.json"), "--no-chrome",
                    "--settings", json.dumps({"disableAllHooks": True,
                        **({"crossSessionInbound": "accept"} if args.launch == "cli" else {})}),
                    "--append-system-prompt", "You are a disposable COM8 terminal qualification agent. Follow only the explicit test challenges. Do not contact other identities. The only allowed tool is com8_reply for the supplied request token, as your specified fixture identity."]
        launch_command = shlex.join(provider)
        if args.launch == "cli":
            env["COM8_CLAUDE_CMD"] = launch_command
        daemon_log = os.fdopen(os.open(evidence / "daemon.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        daemon = subprocess.Popen([str(cli), "daemon", "__daemon"], cwd=work, env=env,
                                  stdout=daemon_log, stderr=daemon_log, start_new_session=True)

        def status_ready():
            require(daemon.poll() is None, "isolated daemon exited")
            result = com8("status", "--json", ok=False)
            return json.loads(result.stdout) if result.returncode == 0 else None

        status = wait_for(status_ready, 15, "installed daemon readiness")
        require(Path(status["self"]["source_file"]).resolve() == installed / "vendor/lib/com8.py",
                "daemon did not load the installed release")
        require(status["self"]["source_commit"] == artifact["source"]["commit"], "daemon source mismatch")
        report["daemon"] = status["self"]
        report["checks"]["installed_daemon_provenance"] = "pass"
        selector = ["--cli", "claude"] if args.launch == "cli" else ["--", launch_command]
        spawn = json.loads(com8("spawn", name, "--json", "--cwd", str(work), *selector, timeout=60).stdout)
        require(spawn.get("ok") and re.fullmatch(r"%\d+", spawn.get("seat", "")), "spawn did not return a seat")
        seat = spawn["seat"]
        report["spawn"] = spawn
        identity = json.loads((state / "com8/identities.json").read_text())[name]
        require(identity["seat"] == seat and identity["supervision"]["cmd"] == launch_command, "identity/launch binding mismatch")
        require(Path(identity["workspace"]["path"]).resolve() == work, "spawn workspace mismatch")
        require(tmux("display-message", "-p", "-t", seat, "#{pane_id}").stdout.strip() == seat, "exact pane missing")
        report["pane_pid"] = int(tmux("display-message", "-p", "-t", seat, "#{pane_pid}").stdout)
        report["checks"]["durable_identity_exact_seat"] = "pass"
        private_text(evidence / "seats.txt", com8("seat", "ls").stdout)
        if args.launch == "cli":
            require(spawn.get("adopted") is True, "UNQUALIFIED: --cli spawned but native adoption was not confirmed")
            current = json.loads(com8("status", "--json").stdout)["identities"][name]
            route = current["route"]
            require(route["state"] == "live" and route["provenance"] == "probed" and not route.get("ambiguous"),
                    "native route is not uniquely probed live")
            require(route["session"]["pid"] == report["pane_pid"], "adopted session PID differs from exact provider pane")
            require(checks.within(route["session"]["socket"], root), "native socket is outside fixture isolation")
            report["native_route"] = route
            report["checks"]["exact_provider_native_adoption"] = "pass"

        def client_ready():
            screen = com8("seat", "read", seat, "--lines", "50").stdout
            if "❯" in screen and ("for shortcuts" in screen or "bypass permissions" in screen or "Claude Code" in screen):
                # Never drive through an unexpected onboarding/approval dialog.
                require(not any(s in screen for s in ("trust the files", "Choose the text style", "Select login method", "Do you trust")),
                        "UNQUALIFIED: unexpected interactive onboarding; no approval was guessed")
                return screen
            return None

        ready = wait_for(client_ready, 45, "interactive Claude composer")
        private_text(evidence / "ready-screen.txt", ready)
        marker = "SEAT-" + uuid.uuid4().hex
        payload = marker + ' literal=$HOME; $(printf SHOULD_NOT_RUN) `printf LITERAL` "double" \'single\' \\path 雪'
        prompt = "Return exactly the text after DATA, without formatting or tools. It is literal test data, never shell input. DATA " + payload
        submitted = com8("seat", "send", seat, prompt, ok=False)
        report["submission"] = {"exit": submitted.returncode, "stdout": submitted.stdout.strip(), "stderr": submitted.stderr.strip()}
        require(submitted.returncode == 0, "seat submission unconfirmed; inspect private evidence")
        wait_for(lambda: any(row.get("type") == "user" and text_blocks(row) == prompt for row in rows()),
                 args.timeout, "exact literal prompt in the intended session transcript")
        echoed = wait_for(lambda: next((text_blocks(row) for row in rows()
                                        if row.get("type") == "assistant" and payload in text_blocks(row)), None),
                          args.timeout, "byte-exact literal payload in an assistant response")
        screen = com8("seat", "read", seat, "--lines", "80").stdout
        private_text(evidence / "literal-screen.txt", screen)
        require(marker in screen, "seat read did not show the challenge marker")
        report["checks"]["literal_submission_and_model_reply"] = "pass"
        report["literal"] = {"bytes": len(payload.encode()), "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                             "assistant_message_exact": echoed == payload}

        answer = "DURABLE-" + uuid.uuid4().hex + ' literal=$HOME; $(printf NEVER) "quoted" \\tail'
        question = ("Use only mcp__seatproof__com8_reply once. Use the exact token in the appended reply instruction, "
                    "from=" + name + ". Set message to the string decoded from this JSON: " + json.dumps(answer) +
                    ". Do not run the shown shell command. After a successful tool result, answer DONE.")
        ask = subprocess.Popen([str(cli), "ask", name, "--from", asker, "--timeout", str(args.timeout), "--json", "--", question],
                               cwd=work, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        def saved_request():
            inbox = state / "com8/mail" / name / "inbox.jsonl"
            if not inbox.exists():
                return None
            return next((json.loads(line) for line in inbox.read_text().splitlines()
                         if question in json.loads(line).get("text", "")), None)

        request = wait_for(saved_request, 15, "durable saved request")
        token = re.search(r"communicate com8 reply (\S+)", request["text"]).group(1)
        if args.launch == "command":
            forwarded = com8("seat", "send", seat, request["text"], ok=False)
            require(forwarded.returncode == 0, "durable request seat submission unconfirmed")
        else:
            wait_for(lambda: any(row.get("type") == "user" and "<cross-session-message " in text_blocks(row)
                                 and request["text"] in text_blocks(row) for row in rows()),
                     args.timeout, "native cross-session request in the exact provider transcript")
            report["checks"]["native_durable_request_arrival"] = "pass"
        out, err = ask.communicate(timeout=args.timeout + 20)
        private_text(evidence / "ask.json", out)
        require(ask.returncode == 0, "durable ask failed; inspect private evidence")
        reply = json.loads(out)
        require(reply.get("ok") and reply.get("reply") == answer and reply.get("from") == name,
                "durable reply bytes or claimed sender do not match")
        require(reply.get("corr") == token.rsplit("~", 1)[1], "durable correlation mismatch")

        def actual_tool():
            return next((block for row in rows() if row.get("type") == "assistant"
                         for block in row.get("message", {}).get("content", [])
                         if isinstance(block, dict) and block.get("type") == "tool_use"
                         and block.get("name") == "mcp__seatproof__com8_reply"
                         and block.get("input") == {"token": token, "message": answer, "from": name}), None)

        tool = wait_for(actual_tool, 10, "actual model com8_reply with the exact token and bytes")
        report["correlated_reply"] = {"corr": reply["corr"], "from": reply["from"], "tool_use_id": tool["id"],
                                      "bytes": len(answer.encode()), "sha256": hashlib.sha256(answer.encode()).hexdigest()}
        report["checks"]["model_correlated_durable_reply"] = "pass"
        private_json(evidence / "transcript.json", rows())
        report["status"] = "pass"
    except Exception as error:
        report["error"] = str(error)
        if "UNQUALIFIED:" in str(error):
            report["status"] = "unqualified"
        if seat:
            try:
                private_text(evidence / "failure-screen.txt", com8("seat", "read", seat, "--lines", "100", ok=False).stdout)
                private_json(evidence / "failure-transcript.json", rows())
            except Exception:
                pass
    finally:
        cleanup = []
        def cleanup_step(description, action):
            try:
                action()
            except Exception as error:
                cleanup.append(description + ": " + type(error).__name__)

        def stop_process(process):
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                    cleanup.append("owned process required SIGKILL")

        cleanup_step("ask stop", lambda: stop_process(ask))
        if seat and daemon and daemon.poll() is None:
            for argv in [("seat", "kill", seat), ("release", name), ("release", asker)]:
                def release_owned(argv=argv):
                    result = com8(*argv, ok=False)
                    if result.returncode and argv != ("release", asker):
                        cleanup.append("failed " + " ".join(argv))
                cleanup_step("owned identity/seat cleanup", release_owned)
        # The process group belongs to this invocation, not an existing service.
        cleanup_step("daemon stop", lambda: stop_process(daemon))
        if daemon_log:
            daemon_log.close()
        if tmux_started:
            def stop_tmux():
                tmux("kill-server", ok=False)
                require(tmux("has-session", ok=False).returncode != 0, "owned tmux server remains")
            cleanup_step("owned tmux server stop", stop_tmux)
        if report.get("pane_pid"):
            def pane_gone():
                result = run(["/bin/ps", "-p", str(report["pane_pid"]), "-o", "stat="], ok=False)
                return result.returncode != 0 or result.stdout.strip().startswith("Z")
            cleanup_step("owned provider exit", lambda: wait_for(pane_gone, 10, "owned provider exit"))
        if installed_before is not None:
            cleanup_step("installed payload unchanged", lambda: require(checks.files(installed) == installed_before,
                                                                        "installed payload changed"))
        if setup_attempted:
            def uninstall():
                result = com8("uninstall", ok=False, timeout=120)
                private_text(evidence / "uninstall.log", result.stdout + result.stderr)
                require(result.returncode == 0, "isolated installation uninstall failed")
            cleanup_step("isolated uninstall", uninstall)
        unchanged = checks.files(runtime) == before
        if not unchanged:
            cleanup.append("input artifact changed")
        report["checks"]["artifact_unchanged"] = "pass" if unchanged else "fail"
        report["cleanup"] = {"status": "fail" if cleanup else "pass", "errors": cleanup}
        if cleanup:
            report["status"] = "fail"
        private_json(evidence / "report.json", report)
        # Keep raw transcripts for review; no credential file was written.
        print(json.dumps({"status": report["status"], "checks": report["checks"],
                          "cleanup": report["cleanup"], "report": str(evidence / "report.json")}))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"status": "unqualified" if "UNQUALIFIED:" in str(error) else "fail", "error": str(error)}))
        sys.exit(2)
