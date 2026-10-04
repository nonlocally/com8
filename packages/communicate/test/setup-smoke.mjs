#!/usr/bin/env node
// Sandboxed setup test: fake $HOME + $COMMUNICATE_DATA, assert merge-not-clobber,
// backup, payload+symlink, uninstall restore, and dry-run writes nothing.
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, readdirSync, lstatSync, rmSync, cpSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";

const pkgDir = fileURLToPath(new URL("..", import.meta.url));
const cli = process.env.COMM_SETUP_TEST_ENTRY || path.join(pkgDir, "src", "cli.mjs");
const fakeHome = mkdtempSync(path.join(os.tmpdir(), "comm-setup-home-"));
const data = path.join(fakeHome, "data");
mkdirSync(path.join(fakeHome, ".claude"), { recursive: true });
mkdirSync(path.join(fakeHome, "bin"));
const pluginVersion = JSON.parse(readFileSync(path.join(pkgDir, "vendor/plugins/communicate/.claude-plugin/plugin.json"), "utf8")).version;
writeFileSync(path.join(fakeHome, "bin", "claude"), `#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['HOME'])
with (root / 'claude.log').open('a') as f: f.write(json.dumps(args) + '\\n')
cached = root / 'claude-cached-version'
if args[:2] == ['plugin', 'uninstall']: cached.unlink(missing_ok=True)
if args[:2] == ['plugin', 'update'] and not cached.exists(): sys.exit(1)
if args[:2] in (['plugin', 'update'], ['plugin', 'install']):
    if (root / 'claude-fail-refresh').exists(): sys.exit(1)
    if args[1] == 'update' and (root / 'claude-skip-update').exists(): sys.exit(0)
    if not (root / 'claude-keep-stale').exists(): cached.write_text(json.loads((pathlib.Path(json.loads((root / ".claude/settings.json").read_text())["extraKnownMarketplaces"]["communicate"]["source"]["path"]) / "communicate/.claude-plugin/plugin.json").read_text())["version"])
if args == ['plugin', 'list', '--json']:
    print(json.dumps([{'id': 'communicate@communicate', 'scope': 'user', 'enabled': True, 'version': cached.read_text()}] if cached.exists() else []))
`, {mode: 0o755});
const sp = path.join(fakeHome, ".claude", "settings.json");
writeFileSync(sp, JSON.stringify({ sentinel: "keep-me", enabledPlugins: { "existing@mkt": true }, permissions: { allow: ["Read"] } }, null, 2));

const env = { ...process.env, HOME: fakeHome, CLAUDE_CONFIG_DIR: path.join(fakeHome, ".claude"),
  CLAUDE_TEST_VERSION: pluginVersion, COMMUNICATE_DATA: data, COMM_STATE: path.join(fakeHome, "state"),
  CODEX_HOME: path.join(fakeHome, ".codex"), COM8_SOCK_DIR: path.join(fakeHome, "sockets"),
  COM8_SESSIONS_DIR: path.join(fakeHome, "sessions"), XDG_RUNTIME_DIR: path.join(fakeHome, "runtime"), COMM_BUS_PORT: "0",
  PATH: path.join(fakeHome, "bin") + path.delimiter + process.env.PATH };
for (const key of ["CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "COM8_SOCK", "COMMUNICATE_HOME"]) delete env[key];
const runCli = (...args) => spawnSync("node", [cli, ...args], { encoding: "utf8", env });
const die = (m) => { console.error("FAIL: " + m); rmSync(fakeHome, { recursive: true, force: true }); process.exit(1); };

// 1. dry-run writes nothing
let r = runCli("setup", "--claude", "--dry-run");
if (r.status !== 0) die("dry-run exited " + r.status + "\n" + r.stderr);
if (existsSync(data)) die("dry-run created the data dir");
if (JSON.parse(readFileSync(sp, "utf8")).extraKnownMarketplaces) die("dry-run edited settings");
if (!/would copy payload/.test(r.stdout)) die("dry-run did not narrate");
if (existsSync(path.join(fakeHome, "claude.log"))) die("dry-run invoked Claude CLI");

// 2. real --claude install
r = runCli("setup", "--claude");
if (r.status !== 0) die("setup exited " + r.status + "\n" + r.stderr + r.stdout);
const s = JSON.parse(readFileSync(sp, "utf8"));
if (s.sentinel !== "keep-me" || !s.enabledPlugins["existing@mkt"] || !s.permissions) die("merge clobbered existing keys");
if (s.extraKnownMarketplaces?.communicate?.source?.source !== "directory") die("marketplace key wrong: " + JSON.stringify(s.extraKnownMarketplaces));
const marketPath = s.extraKnownMarketplaces.communicate.source.path;
const ledger = JSON.parse(readFileSync(path.join(data,"install.json"),"utf8"));
if (ledger.integration.environment.COMM_BUS_PORT !== "0" || ledger.integration.environment.XDG_RUNTIME_DIR !== env.XDG_RUNTIME_DIR)
  die("projection omitted explicit port/runtime isolation");
if (marketPath !== path.join(ledger.integration.root,"plugins")) die("marketplace path wrong: " + marketPath);
const installedVersion = JSON.parse(readFileSync(path.join(marketPath,"communicate/.claude-plugin/plugin.json"))).version;
if (s.enabledPlugins["communicate@communicate"] !== true) die("plugin not enabled");
if (readFileSync(path.join(fakeHome, "claude-cached-version"), "utf8") !== installedVersion) die("Claude cache was not installed via CLI");
if (!readdirSync(path.join(fakeHome, ".claude")).some((f) => f.startsWith("settings.json.communicate-backup-"))) die("no backup written");
if (!lstatSync(path.join(data, "current")).isSymbolicLink()) die("current is not a symlink");
if (!existsSync(path.join(data, "current", "vendor", "bin", "communicate"))) die("payload CLI missing");
for (const file of ["bus.py", "bus_broker.py", "bus_ui.html", "assets/bus-graph.js", "assets/bus-graph.css", "assets/bus-graph.LICENSES.txt"])
  if (!existsSync(path.join(data, "current", "vendor", "lib", file))) die(`bus payload missing: ${file}`);
if (!existsSync(path.join(data, "current", "vendor", "plugins", "communicate", "skills", "communicate-bus", "SKILL.md"))) die("bus registration skill missing");
if (!existsSync(path.join(data, "current", "vendor", "plugins", ".claude-plugin", "marketplace.json"))) die("marketplace file missing in payload");
// The packaged MCP launcher must never depend on a registry or old checkout.
const mcp = JSON.parse(readFileSync(path.join(data, "current", "vendor", "plugins", "communicate", ".mcp.json"), "utf8"));
if (JSON.stringify(mcp).includes("npx") || JSON.stringify(mcp).includes("repo-path")) die("packaged MCP can escape its installed artifact");
// payload CLI actually runs
const agents = spawnSync(path.join(data, "current", "vendor", "bin", "communicate"), ["agents"], { encoding: "utf8", env });
if (agents.status !== 0) die("payload CLI failed: " + agents.stderr);
// Exercise the stabilized copy, whose dependency graph must be self-contained
// even when npm hoisted the original package's dependencies to another root.
const stabilized = spawnSync("node", [path.join(pkgDir, "test", "mcp-smoke.mjs")], {
  encoding: "utf8", env: { ...env, COMM_MCP_TEST_ENTRY: path.join(data, "current", "src", "cli.mjs") }, timeout: 45000,
});
if (stabilized.status !== 0) die("stabilized MCP/bus failed: " + stabilized.stdout + stabilized.stderr);
// Agent CLIs copy plugins into caches, outside the release directory. Their
// launchers must still use the stabilized artifact even with a stale repo hint.
const cached = path.join(fakeHome, "plugin cache", "communicate");
cpSync(path.join(marketPath, "communicate"), cached, { recursive: true });
writeFileSync(path.join(data, "repo-path"), "/missing/legacy-checkout\n");
const cachedMcp = spawnSync("node", [path.join(pkgDir, "test/mcp-smoke.mjs")], {
  encoding: "utf8", env: { ...env, COMM_MCP_TEST_DESCRIPTOR: path.join(cached, ".mcp.json"), COMM_MCP_TEST_DATA: data, COMM_MCP_TEST_PINNED_HOME: fakeHome }, timeout: 45000,
});
if (cachedMcp.status !== 0) die("cached plugin MCP escaped release: " + cachedMcp.stdout + cachedMcp.stderr);
const cachedCli = spawnSync("bash", [path.join(cached, "bin/communicate"), "agents"], { env, encoding: "utf8", timeout: 15000 });
if (cachedCli.status !== 0) die("cached plugin CLI escaped release: " + cachedCli.stderr);

// A versioned cache can remain stale even when enabledPlugins is true.
writeFileSync(path.join(fakeHome, "claude-cached-version"), "0.1.0");
r = runCli("setup", "--claude");
if (r.status !== 0 || readFileSync(path.join(fakeHome, "claude-cached-version"), "utf8") !== installedVersion)
  die("setup did not refresh stale Claude cache");
writeFileSync(path.join(fakeHome, "claude-cached-version"), "0.1.0");
writeFileSync(path.join(fakeHome, "claude-skip-update"), "");
r = runCli("setup", "--claude");
if (r.status !== 0 || readFileSync(path.join(fakeHome, "claude-cached-version"), "utf8") !== installedVersion)
  die("setup did not reinstall an unchanged cache after a successful update");
rmSync(path.join(fakeHome, "claude-skip-update"));
writeFileSync(path.join(fakeHome, "claude-fail-refresh"), "");
if (runCli("setup", "--claude").status === 0) die("failed Claude refresh reported success");
rmSync(path.join(fakeHome, "claude-fail-refresh"));
writeFileSync(path.join(fakeHome, "claude-cached-version"), "0.1.0");
writeFileSync(path.join(fakeHome, "claude-keep-stale"), "");
if (runCli("setup", "--claude").status === 0) die("stale Claude cache reported a successful refresh");
rmSync(path.join(fakeHome, "claude-keep-stale"));
// The persistent failure fixture also prevented compensating cache installation.
// Restore that external prerequisite before exercising ordinary removal.
writeFileSync(path.join(fakeHome, "claude-cached-version"), installedVersion);

// 3. uninstall restores
r = runCli("setup", "--claude", "--uninstall");
if (r.status !== 0) die("uninstall exited " + r.status);
const s2 = JSON.parse(readFileSync(sp, "utf8"));
if (s2.extraKnownMarketplaces?.communicate || s2.enabledPlugins?.["communicate@communicate"]) die("uninstall left keys");
if (s2.sentinel !== "keep-me" || !s2.enabledPlugins["existing@mkt"]) die("uninstall damaged unrelated keys");
if (!existsSync(path.join(data, "current"))) die("uninstall should keep payload without --purge");

rmSync(fakeHome, { recursive: true, force: true });
console.log("PASS: setup-smoke — dry-run inert, settings merge/backup, standalone dependency graph, stabilized MCP/bus, uninstall restores");
