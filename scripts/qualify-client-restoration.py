#!/usr/bin/env python3
"""Opt-in actual-client registry restoration checks for an extracted COM8 artifact.

Requires Python 3.11+, Node, and selected client CLIs. Uses fresh private homes,
local inert plugins and metadata/configuration commands only. No credentials or
existing client configuration are supplied; no model, thread or service requests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib


PLUGIN = "communicate@communicate"
SCENARIOS = ("enabled", "disabled", "policy", "marketplace-only", "none")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def private_json(file, value):
    with file.open("x", encoding="utf-8") as out:
        os.chmod(file, 0o600)
        json.dump(value, out, indent=2, ensure_ascii=False)
        out.write("\n")


def normalized(value):
    # Only these root registry-parent tables are optional when empty. Preserve
    # every semantic leaf, including explicit empty lists and nested dictionaries.
    parents = {"enabledPlugins", "extraKnownMarketplaces", "plugins", "marketplaces"}
    return {key: raw for key, raw in value.items() if not (key in parents and raw == {})}


def run_owned(arguments, *, env, cwd, timeout):
    """Reap this command and stop its owned process group before HOME cleanup."""
    process = subprocess.Popen(arguments, cwd=cwd, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

    def group_signal(value):
        try:
            os.killpg(process.pid, value)
            return True
        except ProcessLookupError:
            return False

    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            group_signal(signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                group_signal(signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=2)
            raise subprocess.TimeoutExpired(arguments, timeout, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
    finally:
        # A CLI can exit before its configuration helper. Never leave that child
        # running against a temporary HOME about to be removed.
        if group_signal(signal.SIGTERM):
            deadline = time.monotonic() + 0.5
            while group_signal(0) and time.monotonic() < deadline:
                time.sleep(0.02)
            group_signal(signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


def artifact(root):
    require((root / "bin/com8").is_file() and (root / "src/com8.mjs").is_file(),
            "Use an extracted release containing bin/com8 and src/com8.mjs")
    require(not (root / ".git").exists(), "Use an extracted artifact, not a checkout")
    manifest = json.loads((root / "vendor/release.json").read_text())
    release = json.loads((root / "release.json").read_text())
    for prefix, files in (("vendor", manifest["files"]), ("", manifest["packageFiles"]), ("", release["files"])):
        for name, expected in files.items():
            file = (root / prefix / name).resolve(strict=True)
            require(file.is_relative_to(root), "Artifact manifest escapes the runtime")
            require(hashlib.sha256(file.read_bytes()).hexdigest() == expected,
                    "Artifact hash mismatch: " + prefix + "/" + name)
    require((root / "node_modules").is_dir(), "Artifact dependencies are missing")
    return manifest


def make_market(root):
    plugin = root / "plugins/communicate"
    for folder in (plugin / ".claude-plugin", plugin / ".codex-plugin",
                   root / "plugins/.claude-plugin", root / ".agents/plugins"):
        folder.mkdir(parents=True)
    descriptor = {"name": "communicate", "version": "0.0.7",
                  "description": "Inert isolated installation restoration fixture"}
    for kind in (".claude-plugin", ".codex-plugin"):
        private_json(plugin / kind / "plugin.json", descriptor)
    private_json(root / "plugins/.claude-plugin/marketplace.json", {
        "name": "communicate", "owner": {"name": "Qualification fixture"},
        "plugins": [{"name": "communicate", "source": "./communicate"}]})
    private_json(root / ".agents/plugins/marketplace.json", {
        "name": "communicate", "interface": {"displayName": "Restoration fixture"},
        "plugins": [{"name": "communicate", "source": {"source": "local", "path": "./plugins/communicate"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}]})


class Scenario:
    def __init__(self, runtime, root, provider, scenario, providers, node, timeout):
        self.runtime, self.root = runtime, root
        self.provider, self.scenario = provider, scenario
        self.home, self.bin = root / "home", root / "tools"
        self.home.mkdir(mode=0o700)
        self.bin.mkdir(mode=0o700)
        self.market = root / "original-market"
        make_market(self.market)
        self.timeout, self.commands = timeout, []
        self.report = {"provider": provider, "scenario": scenario, "status": "fail", "checks": [],
                       "temporary_home": str(self.home), "commands": self.commands}
        self.forbidden = root / "forbidden-calls"
        for name in ("launchctl", "systemctl", "ssh", "gh", "tailscale", "tmux", "pane"):
            script = self.bin / name
            script.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$COM8_QUALIFY_FORBIDDEN"\nexit 93\n')
            script.chmod(0o700)
        for name, binary in {**providers, "node": node}.items():
            if binary:
                (self.bin / name).symlink_to(binary)
        # Explicit allowlist: credentials, inherited socket/session locators,
        # agent hooks and HOME overrides from the calling agent are not copied.
        self.env = {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                    "CODEX_HOME": str(self.home / ".codex"), "COMMUNICATE_DATA": str(self.home / "data"),
                    "COMM_STATE": str(self.home / "state"), "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "XDG_RUNTIME_DIR": str(root / "run"), "TMPDIR": str(root / "tmp"),
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
                    "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C", "COM8_QUALIFY_FORBIDDEN": str(self.forbidden)}
        for folder in (root / "run", root / "tmp", self.home / ".claude", self.home / ".codex", self.home / "state"):
            folder.mkdir(mode=0o700)
        self.claude_settings = self.home / ".claude/settings.json"
        private_json(self.claude_settings, {"env": {"COM8_RESTORATION_SENTINEL": "preserve-unrelated"},
                                            "permissions": {"allow": []}})
        self.codex_config = self.home / ".codex/config.toml"
        self.codex_base = '# fixture-only unrelated settings\n[sandbox_workspace_write]\nwritable_roots = []\n\n[profiles.restoration_sentinel]\nmodel_reasoning_effort = "low"\n'
        self.codex_config.write_text(self.codex_base)
        self.codex_config.chmod(0o600)
        self.sentinel = self.home / "state/literal-preserved-state"
        self.sentinel.write_bytes(b'identity/mail fixture: "$HOME" `literal`\n\x00\xff')

    def run(self, label, args):
        started = time.monotonic()
        try:
            result = run_owned([str(item) for item in args], cwd=self.root, env=self.env, timeout=self.timeout)
        except subprocess.TimeoutExpired as error:
            self.commands.append({"label": label, "argv": [str(item) for item in args], "timeout": self.timeout,
                                  "stdout": error.output, "stderr": error.stderr, "owned_process_group_stopped": True})
            raise
        self.commands.append({"label": label, "argv": [str(item) for item in args],
                              "exit": result.returncode, "seconds": round(time.monotonic() - started, 3),
                              "stdout": result.stdout, "stderr": result.stderr})
        require(not self.forbidden.exists(), "A forbidden service/network/terminal command was attempted")
        require(result.returncode == 0, label + " failed; inspect commands in private evidence")
        return result.stdout

    def client(self, *args):
        return self.run(self.provider + " " + " ".join(args), [self.bin / self.provider, *args])

    def com8(self, *args):
        return self.run("com8 " + " ".join(args), [self.runtime / "bin/com8", *args])

    def config(self, provider=None):
        if (provider or self.provider) == "claude":
            return normalized(json.loads(self.claude_settings.read_text()))
        return normalized(tomllib.loads(self.codex_config.read_text()))

    def snapshot(self):
        if self.provider == "claude":
            listed = json.loads(self.client("plugin", "list", "--json"))
            require(isinstance(listed, list), "Unsupported Claude plugin list schema")
            selected = [row for row in listed if row.get("id") == PLUGIN and row.get("scope") == "user"]
            plugins = [{key: row.get(key) for key in ("id", "version", "scope", "enabled")} for row in selected]
            markets = self.config().get("extraKnownMarketplaces", {})
            source = markets.get("communicate", {}).get("source")
        else:
            listed = json.loads(self.client("plugin", "list", "--marketplace", "communicate", "--json"))
            require(isinstance(listed, dict) and isinstance(listed.get("installed"), list),
                    "Unsupported Codex plugin list schema")
            selected = [row for row in listed["installed"] if row.get("pluginId") == PLUGIN]
            plugins = [{key: row.get(key) for key in ("pluginId", "version", "installed", "enabled")} for row in selected]
            markets = json.loads(self.client("plugin", "marketplace", "list", "--json"))
            if isinstance(markets, dict):
                markets = markets.get("marketplaces")
            require(isinstance(markets, list), "Unsupported Codex marketplace list schema")
            market = next((row for row in markets if row.get("name") == "communicate"), None)
            source = market.get("marketplaceSource") or {"root": market.get("root")} if market else None
        return {"plugins": plugins, "marketplace": source, "config": self.config()}

    def check(self, label, condition):
        self.report["checks"].append({"name": label, "status": "pass" if condition else "fail"})
        require(condition, label)

    def execute(self):
        self.report["client_version"] = self.client("--version").strip()
        self.report["resolved_client"] = str((self.bin / self.provider).resolve())
        if self.scenario != "none":
            market = self.market / "plugins" if self.provider == "claude" else self.market
            self.client("plugin", "marketplace", "add", str(market))
        installed = self.scenario in ("enabled", "disabled", "policy")
        if installed:
            if self.provider == "claude":
                self.client("plugin", "install", PLUGIN, "--scope", "user")
                if self.scenario == "disabled":
                    self.client("plugin", "disable", PLUGIN, "--scope", "user")
            else:
                self.client("plugin", "add", PLUGIN)
                if self.scenario in ("disabled", "policy"):
                    # Codex has no plugin-disable CLI. This fresh test home owns
                    # its entire tiny user config; use the supported TOML fields
                    # and confirm the effective result through the actual CLI.
                    config = self.codex_config.read_text()
                    header = '[plugins."communicate@communicate"]'
                    require(header in config, "Unsupported fixture plugin configuration syntax")
                    prefix, section = config.split(header, 1)
                    section, separator, tail = section.partition("\n[")
                    section, replacements = re.subn(r"(?m)^enabled\s*=\s*true\s*$", "enabled = false", section)
                    require(replacements == 1, "Fixture plugin has no single enabled field")
                    config = prefix + header + section + separator + tail
                    if self.scenario == "policy":
                        config += '\n[plugins."communicate@communicate".mcp_servers.communicate.tools.bus_send]\napproval_mode = "prompt"\n'
                    self.codex_config.write_text(config)
        before = self.snapshot()
        self.report["original"] = before
        self.check("fixture installed state", bool(before["plugins"]) == installed)
        if installed:
            self.check("fixture enabled state", before["plugins"][0]["enabled"] == (self.scenario == "enabled"))
        other = "codex" if self.provider == "claude" else "claude"
        unrelated = self.config(other)
        preserved = self.sentinel.read_bytes()
        for removal in ("selective", "full"):
            self.com8("setup", "--" + self.provider, "--no-service")
            if removal == "selective":
                self.com8("update", "--" + self.provider, "--no-service")
            active = self.snapshot()
            self.report[removal + "_active"] = active
            self.check(removal + " setup installed plugin", len(active["plugins"]) == 1)
            self.check(removal + " setup enabled plugin", active["plugins"][0]["enabled"] is True)
            active_path = (self.home / "data/current").resolve(strict=True)
            self.check(removal + " selected runtime remains private", active_path.is_relative_to(self.home / "data"))
            self.report[removal + "_runtime"] = str(active_path)
            ledger = json.loads((self.home / "data/install.json").read_text())
            self.check(removal + " no managed service", not ledger.get("service"))
            self.com8("uninstall", *(["--" + self.provider] if removal == "selective" else []))
            restored = self.snapshot()
            self.report[removal + "_restored"] = restored
            self.check(removal + " restores original registry and settings", restored == before)
            self.check(removal + " preserves other client configuration", self.config(other) == unrelated)
            self.check(removal + " preserves literal runtime state", self.sentinel.read_bytes() == preserved)
        self.report["status"] = "pass"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--evidence", required=True, type=Path, help="New private directory; must not exist")
    parser.add_argument("--provider", choices=("claude", "codex"), action="append")
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--timeout", type=int, default=60, help="Per-command timeout in seconds")
    parser.add_argument("--run-clients", action="store_true", help="Opt in to actual CLI registry operations in disposable homes")
    args = parser.parse_args()
    if not args.run_clients:
        parser.error("--run-clients is required; operations use fresh private homes only")
    require(args.timeout >= 5, "timeout must be at least 5 seconds")
    runtime = args.runtime.resolve(strict=True)
    manifest = artifact(runtime)
    node = shutil.which("node")
    require(node, "Node is required")
    providers = {name: shutil.which(name) for name in ("claude", "codex")}
    selected = list(dict.fromkeys(args.provider or providers.keys()))
    scenarios = list(dict.fromkeys(args.scenario or SCENARIOS))
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = args.evidence.resolve()
    report = {"status": "fail", "runtime": str(runtime), "source": manifest["source"],
              "version": manifest["version"], "node": str(Path(node).resolve()), "cases": [],
              "auth_supplied": False, "model_calls": 0, "threads_started": 0, "services_started": 0,
              "scope": "actual client registry/settings restoration with inert local originals; no running model activation",
              "unqualified": ["replacement by a different product after setup", "real historical plugin content", "running agent activation"]}
    prior_umask = os.umask(0o077)
    try:
        for provider in selected:
            if not providers[provider]:
                report["cases"].append({"provider": provider, "status": "unqualified", "reason": "CLI unavailable"})
                continue
            for scenario in scenarios:
                if scenario == "policy" and provider != "codex":
                    continue
                with tempfile.TemporaryDirectory(prefix="com8-client-restore-") as temp:
                    case = Scenario(runtime, Path(temp).resolve(), provider, scenario, providers, node, args.timeout)
                    try:
                        case.execute()
                    except Exception as error:
                        case.report["error"] = str(error)
                    result = case.report
                result["temporary_home_removed"] = not Path(temp).exists()
                private_json(evidence / (provider + "-" + scenario + ".json"), result)
                report["cases"].append({key: value for key, value in result.items()
                                        if key in ("provider", "scenario", "status", "client_version", "error", "temporary_home_removed")})
                print(provider + "/" + scenario + ": " + result["status"], file=sys.stderr, flush=True)
        statuses = [case["status"] for case in report["cases"]]
        report["status"] = "fail" if "fail" in statuses else "unqualified" if "unqualified" in statuses or not statuses else "pass"
        private_json(evidence / "report.json", report)
    finally:
        os.umask(prior_umask)
    print(json.dumps(report, indent=2))
    return {"pass": 0, "fail": 1, "unqualified": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
