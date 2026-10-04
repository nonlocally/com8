#!/usr/bin/env python3
"""Qualify an extracted COM8 runtime without a checkout or provider credentials.

Usage: python3 qualify-installed.py /path/to/com8-0.3.0 [--previous-runtime PATH]
Only disposable copies/homes are changed. No service manager, provider, network,
package manager, Git checkout, or existing tmux server is used. JSON is stdout.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def within(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def files(root):
    """Contents, modes and link targets; excludes access/modified timestamps."""
    return {str(p.relative_to(root)): {"link": os.readlink(p)} if p.is_symlink()
            else {"sha256": sha(p), "mode": p.stat().st_mode & 0o777} if p.is_file()
            else {"directory": True, "mode": p.stat().st_mode & 0o777}
            for p in sorted(root.rglob("*"))}


def artifact(root):
    require(root.is_dir(), f"runtime directory missing: {root}")
    require(not (root / ".git").exists(), "supply an extracted release, not a Git checkout")
    required = ["package.json", "package-lock.json", "LICENSE", "release.json",
                "src/com8.mjs", "src/cli.mjs", "src/paths.mjs", "node_modules",
                "vendor/release.json", "vendor/bin/com8", "vendor/lib/com8.py",
                "vendor/profiles/manage.py", "bin/com8", "bin/communicate"]
    for name in required:
        require((root / name).exists(), f"release component missing: {name}")
    for path in root.rglob("*"):
        if path.is_symlink():
            require(within(path, root), f"release symlink escapes artifact: {path}")
            require(path.exists(), f"broken release symlink: {path}")
    manifest = json.loads((root / "release.json").read_text())
    vendor = json.loads((root / "vendor/release.json").read_text())
    version = json.loads((root / "package.json").read_text())["version"]
    require(manifest.get("version") == vendor.get("version") == version, "release versions disagree")
    require(manifest.get("product") == vendor.get("product") == "COM8", "unexpected release product")
    require(manifest.get("source") == vendor.get("source", {}).get("commit"), "outer/vendor source revision mismatch")
    require(manifest.get("files"), "outer release has no file hashes")
    for base, mapping in [(root, manifest["files"]), (root / "vendor", vendor["files"]),
                          (root, vendor.get("packageFiles", {}))]:
        for name, expected in mapping.items():
            path = base / name
            require(within(path, base) and path.is_file(), f"unsafe/missing manifest path: {name}")
            require(sha(path) == expected, f"release checksum mismatch: {name}")
    unlisted = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                and not p.is_symlink() and p != root / "release.json"
                and str(p.relative_to(root)) not in manifest["files"]]
    require(not unlisted, f"unhashed release files: {unlisted[:5]}")
    return {"version": version, "source": vendor.get("source"), "runtime": str(root),
            "manifest_sha256": sha(root / "release.json"), "vendor_manifest_sha256": sha(root / "vendor/release.json"),
            "hashed_files": len(manifest["files"])}


class MCP:
    def __init__(self, argv, env, cwd):
        self.process = subprocess.Popen(argv, env=env, cwd=cwd, text=True,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
        self.lines, self.errors, self.sequence = queue.Queue(), [], 0
        def read():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)
        def errors():
            for line in self.process.stderr:
                self.errors.append(line)
        self.readers = [threading.Thread(target=read, daemon=True), threading.Thread(target=errors, daemon=True)]
        for reader in self.readers:
            reader.start()

    def rpc(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError("MCP response timed out: " + "".join(self.errors)[-1500:])
            require(line is not None, "MCP exited: " + "".join(self.errors)[-1500:])
            response = json.loads(line)
            if response.get("id") != self.sequence:
                continue
            require(not response.get("error"), f"MCP {method}: {response.get('error')}")
            return response["result"]
        raise RuntimeError("MCP response timed out")

    def initialize(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "com8-artifact-qualification", "version": "1"}})
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.process.stdin.flush()
        require("COM8" in result.get("instructions", ""), "MCP startup guidance missing")
        return result

    def call(self, name, arguments=None):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        output = "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
        require(not result.get("isError"), f"MCP {name}: {output}")
        return output

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=1)
        for stream in [self.process.stdin, self.process.stdout, self.process.stderr]:
            stream.close()


class Qualification:
    def __init__(self, base, report):
        self.base, self.report = base, report
        self.home = base / "home with spaces"
        self.home.mkdir()
        self.data, self.state = base / "data", base / "state"
        self.daemon, self.log = None, None
        tools = base / "tools"
        tools.mkdir()
        resolved = {}
        for name in ["node", "python3", "bash"]:
            candidates = (["/opt/homebrew/bin/bash", "/usr/local/bin/bash"] if name == "bash" else []) + [shutil.which(name)]
            path = next((Path(p).resolve() for p in candidates if p and Path(p).is_file()), None)
            require(path is not None, f"required executable unavailable: {name}")
            (tools / name).symlink_to(path)
            resolved[name] = str(path)
        self.blocked = base / "blocked.jsonl"
        blocker = '#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ["COM8_QUALIFY_BLOCKED"],"a") as f: f.write(json.dumps(sys.argv)+"\\n")\nsys.exit(97)\n'
        for name in ["claude", "codex", "opencode", "pi", "tmux", "ssh", "tailscale", "launchctl", "systemctl", "npm", "npx", "git", "curl", "wget"]:
            (tools / name).write_text(blocker)
            (tools / name).chmod(0o755)
        (base / "tmp").mkdir()
        self.env = {"HOME": str(self.home), "PATH": str(tools) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMPDIR": str(base / "tmp"), "LANG": "en_US.UTF-8", "USER": "com8-qualification", "LOGNAME": "com8-qualification",
                    "COMMUNICATE_DATA": str(self.data), "COMM_STATE": str(self.state),
                    "COM8_SELF": "artifact-fixture", "COM8_SOCK_DIR": str(base / "socks"),
                    "COM8_SESSIONS_DIR": str(base / "sessions"), "COM8_TMUX_SOCKET": str(base / "tmux.sock"),
                    "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "CODEX_HOME": str(self.home / ".codex"),
                    "XDG_RUNTIME_DIR": str(base / "run"), "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "COM8_QUALIFY_BLOCKED": str(self.blocked)}
        self.report["paths"] = {"temporary_home": str(self.home), "state": str(self.state), "installation": str(self.data), "executables": resolved}

    @contextlib.contextmanager
    def check(self, name):
        row = {"name": name, "status": "pass"}
        self.report["checks"].append(row)
        try:
            yield row
        except Exception as error:
            row.update(status="fail", error=str(error) or type(error).__name__)
            raise

    def run(self, *args, ok=True):
        result = subprocess.run([str(a) for a in args], cwd=self.home, env=self.env,
                                text=True, capture_output=True, timeout=30)
        require(not ok or result.returncode == 0, f"{' '.join(map(str, args[:4]))}: exit {result.returncode}: {(result.stderr + result.stdout)[-2000:]}")
        return result

    def cli(self, root, *args, ok=True):
        return self.run(root / "bin/com8", *args, ok=ok)

    def paths(self, root):
        script = 'import {pathToFileURL} from "node:url"; const p=await import(pathToFileURL(process.argv[1]+"/src/paths.mjs")); console.log(JSON.stringify({pkgDir:p.pkgDir,com8Cli:p.com8Cli,communicateCli:p.communicateCli}));'
        paths = json.loads(self.run("node", "--input-type=module", "-e", script, root).stdout)
        for key in ["pkgDir", "com8Cli", "communicateCli"]:
            require(within(paths[key], root), f"checkout/external fallback in {key}: {paths[key]}")
        require(Path(paths["com8Cli"]).resolve() == (root / "vendor/bin/com8").resolve(), "public CLI bypasses installed vendor")
        return paths

    def installed(self):
        root = (self.data / "current").resolve(strict=True)
        require(within(root, self.data), f"current escapes installation: {root}")
        require((self.data / "bin/com8").resolve() == root / "src/com8.mjs", "installed executable points outside current release")
        self.paths(root)
        return root

    def start(self, root):
        require(self.daemon is None, "qualification daemon is already running")
        self.log = (self.base / "daemon.log").open("w")
        self.daemon = subprocess.Popen([str(root / "vendor/bin/com8"), "daemon", "__daemon"],
                                       cwd=self.home, env=self.env, stdout=self.log, stderr=self.log, start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            require(self.daemon.poll() is None, "isolated daemon exited: " + (self.base / "daemon.log").read_text()[-2000:])
            response = self.run(self.data / "bin/com8", "status", "--json", ok=False)
            if response.returncode == 0:
                status = json.loads(response.stdout)
                require(Path(status["self"]["source_file"]).resolve() == (root / "vendor/lib/com8.py").resolve(), "daemon source is not the installed release")
                require(status["self"]["pid"] == self.daemon.pid, "status answered by another process")
                return status["self"]
            time.sleep(0.05)
        raise RuntimeError("isolated daemon did not become ready")

    def stop(self):
        if self.daemon is not None:
            if self.daemon.poll() is None:
                os.killpg(self.daemon.pid, signal.SIGTERM)
                try:
                    self.daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.daemon.pid, signal.SIGKILL)
                    self.daemon.wait(timeout=5)
            self.daemon = None
        if self.log:
            self.log.close()
            self.log = None

    def mcp(self, root, mail=False):
        # Exercise the packed plugin launcher, not a repository test helper.
        client = MCP([str(root / "vendor/plugins/communicate/bin/communicate-mcp")], self.env, self.home)
        try:
            info = client.initialize()
            names = {tool["name"] for tool in client.rpc("tools/list", {})["tools"]}
            require({"com8_claim", "com8_send", "com8_inbox", "com8_status", "bus_status", "route"} <= names, "combined MCP tools missing")
            if (root / "vendor/lib/model_connections.py").exists():
                require({"com8_model_list", "com8_model_doctor"} <= names, "model MCP tools missing")
                connections = json.loads(client.call("com8_model_list", {}))
                require(connections.get("ok") is True, "model MCP discovery failed")
                if (self.home / ".config/com8/models/qualification-model").exists():
                    require([item["name"] for item in connections["connections"]] == ["qualification-model"], "model MCP roster differs from installed configuration")
                    require("NOT_A_REAL_KEY" not in json.dumps(connections), "model MCP exposed fixture key")
            if mail:
                for name in ["qualification-sender", "qualification-receiver"]:
                    client.call("com8_claim", {"name": name})
                text = "literal --from '$value'\nartifact-only mailbox proof"
                client.call("com8_send", {"from": "qualification-sender", "target": "qualification-receiver", "message": text})
                inbox = [json.loads(line) for line in client.call("com8_inbox", {"name": "qualification-receiver"}).splitlines() if line]
                require(any(item.get("text") == text for item in inbox), "MCP message bytes were not retained")
            return {"tools": len(names), "server": info.get("serverInfo"), "launcher": str(root / "vendor/plugins/communicate/bin/communicate-mcp")}
        finally:
            client.close()

    def profile(self):
        cli = self.data / "bin/com8"
        selected = ["--terminal", "--mesh", "--accounts", "--box", "--snapshots"]
        rc = self.home / ".bashrc"
        original = b"# original shell config without newline"
        rc.write_bytes(original)
        with self.check("profile preview is inert"):
            before = files(self.home)
            result = json.loads(self.run(cli, "profile", "preview", *selected).stdout)
            require(result["read_only"] and result["actions"], "profile preview missing")
            require(files(self.home) == before, "profile preview changed HOME")
        with self.check("profile install/repeat/uninstall/reinstall") as row:
            self.run(cli, "profile", "install", *selected)
            ledger = self.home / ".local/state/com8/profiles/ownership.json"
            record = json.loads(ledger.read_text())
            backup = record["entries"][str(rc)]["original"]["backup"]
            private = self.home / ".config/com8/profiles/local.sh"
            private.write_text("# private overlay retained\n")
            rc.write_bytes(rc.read_bytes() + b"# later user edit\n")
            self.run(cli, "profile", "install", *selected)
            require(json.loads(ledger.read_text())["entries"][str(rc)]["original"]["backup"] == backup, "repeat install discarded first backup")
            require(rc.read_text().count("# >>> COM8 profile") == 1, "duplicate managed profile block")
            require("COM8 mesh" in self.run(self.home / ".local/bin/com8-mesh", "help").stdout, "installed mesh helper fails")
            self.run(cli, "profile", "uninstall")
            require(rc.read_bytes() == original + b"\n# later user edit\n", "profile uninstall lost unrelated content")
            require(private.read_text() == "# private overlay retained\n", "profile uninstall changed private overlay")
            self.run(cli, "profile", "install", *selected)
            require(rc.read_text().count("# >>> COM8 profile") == 1, "profile reinstall duplicated block")
            row["payload"] = json.loads(ledger.read_text())["payload"]
        with self.check("installed optional modules are self-contained and inert") as row:
            row.update(self.profile_helpers())
        with self.check("fresh interactive Bash startup") as row:
            command = 'for fn in t cx cxx cxc cdx cdxx cdxxs mesh; do declare -F "$fn" >/dev/null || exit 9; done; printf "READY\\n%s\\n%s\\n" "$HOME" "$COM8_PROFILE_RUNTIME"; command -v com8-workstation'
            observations = {}
            for label, args in [("nonlogin", ["--noprofile", "-ic"]), ("login", ["-lic"])]:
                result = self.run("bash", *args, command)
                lines = result.stdout.splitlines()
                require(lines[0] == "READY" and Path(lines[1]) == self.home, f"{label} Bash did not use its temporary profile")
                require(within(lines[2], self.home / ".local/share/com8/profiles"), f"{label} Bash loaded a legacy profile")
                require(Path(lines[3]) == self.home / ".local/bin/com8-workstation", f"{label} Bash launcher is shadowed")
                observations[label] = {"runtime": lines[2], "launcher": lines[3]}
            row["shells"] = observations
        if Path("/bin/zsh").exists():
            with self.check("fresh zsh discovers executable shortcuts without loading Bash functions") as row:
                command = ('[[ -n $ZSH_VERSION ]] || exit 8; '
                           'for fn in t cx mesh; do (( $+functions[$fn] )) && exit 9; done; '
                           'print -r -- "$HOME"; command -v t cx cxx cdx cdxx mesh')
                result = self.run("/bin/zsh", "-d", "-lic", command)
                expected = [str(self.home)] + [str(self.home / ".local/bin" / name)
                                              for name in ("t", "cx", "cxx", "cdx", "cdxx", "mesh")]
                require(result.stdout.splitlines() == expected, "zsh did not discover the installed executable shortcuts")
                rc = (self.home / ".zshrc").read_text()
                require("# >>> COM8 profile" in rc and "COM8_PROFILE_RUNTIME" not in rc,
                        "zsh startup does not contain the intended PATH-only include")
                require(not (self.home / ".zprofile").exists(), "profile unexpectedly replaced zsh login configuration")
                row["support"] = "zsh keeps its interpreter and uses executable shortcuts; Bash functions stay inside their wrappers."

    def profile_helpers(self):
        record = json.loads((self.home / ".local/state/com8/profiles/ownership.json").read_text())
        payload = Path(record["payload"])
        before = files(payload)
        commands = self.home / ".local/bin"
        require("com8-account" in self.run(commands / "com8-account", "help").stdout, "installed account help fails")
        require("com8-box" in self.run(commands / "com8-box", "help").stdout, "installed box help fails")
        preview = json.loads(self.run(commands / "com8-snapshot", "schedule", "preview").stdout)
        require(preview["read_only"] and preview["scope"] == "scoped", "snapshot preview did not use isolated ownership")
        require(not any(e.get("owner") == "snapshots-schedule" for e in record["entries"].values()), "profile selection activated a schedule")
        message = "artifact reply 'literal' $HOME; preserved bytes"
        self.run(commands / "com8-account-pane", "reply", "artifact-correlation", message)
        reply = self.home / ".local/state/com8/pane/replies/artifact-correlation"
        require(reply.read_text().rstrip("\n") == message, "installed file reply changed message bytes")
        require(reply.stat().st_mode & 0o077 == 0, "installed file reply is not private")
        require(files(payload) == before, "optional helpers changed immutable profile payload")
        return {"payload": str(payload), "schedule_scope": preview["scope"],
                "loaded_schedule": False, "file_reply": "literal private correlated file"}

    def execute(self, runtime, previous=None):
        with self.check("archive manifest and complete payload") as row:
            row.update(artifact(runtime))
            if tuple(map(int, row["version"].split(".")[:2])) >= (0, 5):
                require((runtime / "vendor/lib/model_connections.py").is_file(), "model connections missing from v0.5 payload")
        copy = self.base / "runtime with spaces"
        shutil.copytree(runtime, copy, symlinks=True)
        with self.check("artifact CLI and source resolution") as row:
            row["resolved"] = self.paths(copy)
            row["version"] = self.cli(copy, "version").stdout.strip()
            require("bus" in self.cli(copy, "--help").stdout, "public CLI help failed")
        with self.check("fresh setup dry run is inert"):
            before = files(self.home)
            self.cli(copy, "setup", "--no-clients", "--no-service", "--dry-run")
            require(files(self.home) == before and not self.data.exists(), "dry run created installation/configuration")
        with self.check("fresh setup and repeated update") as row:
            self.cli(copy, "setup", "--no-clients", "--no-service")
            installed = self.installed()
            self.cli(copy, "update", "--no-clients", "--no-service")
            require(self.installed() == installed, "same artifact update changed immutable release location")
            row["installed"] = str(installed)
            self.report["paths"]["installed"] = str(installed)
        model_state = None
        if (installed / "vendor/lib/model_connections.py").exists():
            with self.check("installed private model configuration without inference") as row:
                immutable_before = files(installed)
                key_file = self.home / "qualification-model-key"
                key_file.write_text("nlm_NOT_A_REAL_KEY_artifact_fixture\n")
                key_file.chmod(0o600)
                cli = self.data / "bin/com8"
                args = ["model", "add", "qualification-model", "--base-url", "https://model-qualification.invalid/v1",
                        "--anthropic-base-url", "https://model-qualification.invalid", "--model", "glm", "--key-file", str(key_file), "--json"]
                before = files(self.home)
                dry = json.loads(self.run(cli, *args, "--dry-run").stdout)
                require(dry.get("credential_read") is False and files(self.home) == before, "model preview changed files or read key")
                result = self.run(cli, *args)
                require(json.loads(result.stdout).get("ok") is True, "model connection not created")
                listing = self.run(cli, "model", "list", "--json").stdout
                require("NOT_A_REAL_KEY" not in listing + result.stdout + result.stderr, "model output exposed fixture key")
                require([x["name"] for x in json.loads(listing)["connections"]] == ["qualification-model"], "model connection absent from roster")
                model_root = self.home / ".config/com8/models"
                model_state = files(model_root)
                for path in [model_root, model_root / "qualification-model", *(model_root / "qualification-model").iterdir()]:
                    require(path.stat().st_mode & 0o077 == 0, "model configuration is not private")
                duplicate = self.run(cli, *args, ok=False)
                require(duplicate.returncode != 0 and files(model_root) == model_state, "duplicate model overwrote saved connection")
                key_file.unlink()
                require(files(installed) == immutable_before, "model configuration changed immutable payload")
                row.update(configured=True, credentials_private=True, inference_tested=False)
        with self.check("installed daemon and actual MCP mailbox") as row:
            payload_before = files(installed)
            row["daemon"] = self.start(installed)
            row["mcp"] = self.mcp(installed, mail=True)
            self.stop()
            require(files(installed) == payload_before, "daemon or MCP modified the installed payload")
        mail = self.state / "com8/mail/qualification-receiver/inbox.jsonl"
        mail_before = mail.read_bytes()
        self.profile()
        with self.check("installed runtime survives artifact relocation") as row:
            hidden = self.base / "relocated artifact"
            copy.rename(hidden)
            self.run(self.data / "bin/com8", "version")
            row["mcp"] = self.mcp(installed)
            self.run(self.home / ".local/bin/com8-mesh", "help")
            copy = hidden
        with self.check("core uninstall/reinstall preserves mailbox and profiles"):
            self.run(self.data / "bin/com8", "uninstall", "--no-clients", "--no-service")
            if model_state:
                require(files(model_root) == model_state, "uninstall changed model configuration, modes or inventory")
            require(not (self.data / "bin/com8").exists(), "uninstall retained owned executable")
            require(mail.read_bytes() == mail_before, "uninstall changed durable mailbox")
            self.run(self.home / ".local/bin/com8-mesh", "help")
            self.profile_helpers()
            self.cli(copy, "setup", "--no-clients", "--no-service")
            installed = self.installed()
            require(mail.read_bytes() == mail_before, "reinstall changed durable mailbox")
            require("artifact-only mailbox proof" in self.run(self.data / "bin/com8", "inbox", "qualification-receiver").stdout, "reinstalled CLI cannot read prior mail")
            if model_state:
                require(files(model_root) == model_state, "uninstall/reinstall changed model connection")
        if previous:
            with self.check("real previous release upgrade and rollback") as row:
                row["previous"] = artifact(previous)
                require(sha(previous / "vendor/release.json") != sha(runtime / "vendor/release.json"), "upgrade requires two distinct release manifests")
                old_copy = self.base / "previous artifact"
                shutil.copytree(previous, old_copy, symlinks=True)
                self.cli(old_copy, "setup", "--no-clients", "--no-service")
                old = self.installed()
                # Create state through the old installed CLI, not just through
                # the candidate before switching payloads.
                self.start(old)
                self.run(self.data / "bin/com8", "claim", "qualification-upgrade")
                self.run(self.data / "bin/com8", "send", "qualification-upgrade", "--",
                         "saved by the previous release: '$HOME' and literal --from")
                self.stop()
                bus = self.state / "bus"
                bus.mkdir(mode=0o700, exist_ok=True)
                hub = "https://qualification.invalid"
                thread = "11111111-1111-4111-8111-111111111111"
                session = "codex:" + thread
                # Inert saved enrollment/adapter fixtures only. No provider,
                # broker, worker, enrollment or network command is invoked.
                fixtures = {
                    "client.json": {"default": hub, "connections": {hub: {
                        "url": hub, "principal": "fixture-device", "token": "not-a-real-credential", "local": False}}},
                    "registrations.json": {hub + "|" + session: {
                        "id": "fixture-agent", "url": hub, "session_key": session, "kind": "codex",
                        "thread": thread, "name": "saved-adapter", "status": "offline",
                        "binary": str(self.base / "tools/codex"), "codex_home": self.env["CODEX_HOME"],
                        "description": "inert upgrade fixture", "buses": [], "reply_until": 0}},
                }
                for name, value in fixtures.items():
                    target = bus / name
                    target.write_text(json.dumps(value, indent=2) + "\n")
                    target.chmod(0o600)
                preserved = [mail, self.state / "com8/mail/qualification-upgrade/inbox.jsonl",
                             bus / "client.json", bus / "registrations.json"]
                before = {path: path.read_bytes() for path in preserved}
                identities = self.state / "com8/identities.json"
                identities_before = json.loads(identities.read_text())
                def preserved_state(stage):
                    for path, content in before.items():
                        require(path.read_bytes() == content, f"{stage} changed saved state: {path.relative_to(self.state)}")
                    require(json.loads(identities.read_text()) == identities_before, f"{stage} changed saved identities")
                    require(not (bus / "worker.json").exists() and not (bus / "server.json").exists(),
                            f"{stage} unexpectedly started a bus worker or broker")
                    if model_state:
                        require(files(model_root) == model_state, f"{stage} changed model connection")
                row["saved_state_sha256"] = {str(path.relative_to(self.state)): sha(path) for path in preserved}
                self.cli(copy, "update", "--no-clients", "--no-service")
                require(self.installed() == installed and old != installed, "upgrade did not select candidate release")
                preserved_state("upgrade")
                row["upgraded_daemon"] = self.start(installed)
                self.stop()
                preserved_state("upgraded daemon restart")
                self.run(self.data / "bin/com8", "rollback")
                require(self.installed() == old, "rollback did not select previous release")
                preserved_state("rollback")
                self.cli(copy, "update", "--no-clients", "--no-service")
                require(self.installed() == installed, "second upgrade did not reselect candidate release")
                row["final_mcp"] = self.mcp(installed)
                preserved_state("second upgrade")
        else:
            self.report["checks"].append({"name": "different-release upgrade and rollback", "status": "unqualified",
                                          "reason": "Pass --previous-runtime with a distinct real release; same-artifact update is tested separately."})
        with self.check("profile configuration and ownership are private") as row:
            paths = [self.home / ".config/com8/profiles", self.home / ".local/state/com8/profiles"]
            row["directories"] = [{"path": str(path), "mode": oct(path.stat().st_mode & 0o777)} for path in paths]
            require(all(path.stat().st_mode & 0o077 == 0 for path in paths), "profile configuration/backups permit access by other users")
        with self.check("no external tools, credentials, services, or source fallback"):
            require(not self.blocked.exists(), "forbidden external command attempted: " + (self.blocked.read_text() if self.blocked.exists() else ""))
            require(not (self.home / "Library/LaunchAgents").exists(), "unexpected launchd configuration")
            require(not (self.home / ".config/systemd/user").exists(), "unexpected systemd configuration")
            require(not (self.home / ".claude/settings.json").exists(), "unexpected Claude registration")
            require(not (self.home / ".codex/config.toml").exists(), "unexpected Codex registration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--previous-runtime", type=Path)
    parser.add_argument("--keep-temp", action="store_true", help="retain only this run's disposable evidence for debugging")
    args = parser.parse_args()
    report = {"schema": 1, "ok": False, "checks": [], "unqualified": [
        "Real Claude/Codex plugin activation, model wake/resume and provider credentials",
        "Service-manager activation, restart after reboot, and existing-installation cutover",
        "Ghostty rendering, tmux interactive behavior, VNC and remote/Tailscale access",
        "Package publication, Homebrew download/formula execution and npm registry installation"]}
    base = Path(tempfile.mkdtemp(prefix="com8-q-", dir="/tmp")).resolve()
    runner = None
    try:
        runner = Qualification(base, report)
        runner.execute(args.runtime.expanduser().resolve(), args.previous_runtime.expanduser().resolve() if args.previous_runtime else None)
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error) or type(error).__name__
    finally:
        if runner:
            runner.stop()
        report["temporary_files_retained"] = args.keep_temp
        if not args.keep_temp:
            shutil.rmtree(base)
        print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
