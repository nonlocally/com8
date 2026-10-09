#!/usr/bin/env node
// Setup/uninstall boundaries a person can observe: uninstall stops the bus it
// would otherwise leave running and says what it keeps, a replaced plugin
// marketplace is named before its plugins stop resolving, and non-interactive
// plain setup shows its plan before applying it.
// Isolated homes, fixture clients and a sandboxed loopback broker only.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const pkg = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const com8 = path.join(pkg, "src/com8.mjs");
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "com8-setup-safety-"));
const fixtureBuses = [];

const FAKE_CLAUDE = `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path');
const a=process.argv.slice(2), home=process.env.HOME;
const installed=path.join(home,'claude-installed');
if (a[0]==='--version') { console.log('fixture'); process.exit(0); }
if(a[0]==='plugin'&&['update','install'].includes(a[1]))fs.writeFileSync(installed,'user');
if(a[0]==='plugin'&&a[1]==='uninstall')fs.rmSync(installed,{force:true});
if (a[0]==='plugin' && a[1]==='list') {
 if(!fs.existsSync(installed)){console.log('[]');process.exit(0);}
 const market=JSON.parse(fs.readFileSync(path.join(home,'.claude/settings.json'))).extraKnownMarketplaces.communicate.source.path;
 const p=path.join(market,'communicate/.claude-plugin/plugin.json');
 console.log(JSON.stringify([{id:'communicate@communicate',scope:'user',enabled:true,version:JSON.parse(fs.readFileSync(p)).version}]));
}
`;
// Enough of Codex for an existing-session queue adapter; nothing is ever queued.
const FAKE_CODEX = `#!/usr/bin/env python3
import sys
if sys.argv[1:] == ["queue", "--help"]:
    raise SystemExit(0)
raise SystemExit("fixture codex only answers queue --help")
`;

const onPath = (name) => process.env.PATH.split(path.delimiter).map((dir) => path.join(dir, name)).find((file) => {
  try { fs.accessSync(file, fs.constants.X_OK); return true; } catch { return false; }
});
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const gone = async (pid) => {
  for (let i = 0; i < 60; i++) {
    if (!alive(pid)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return false;
};

function sandbox(name) {
  const home = path.join(temp, name), bin = path.join(home, "bin");
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.join(home, ".claude"), { recursive: true });
  // Only the runtimes setup needs. Real provider CLIs stay off this PATH.
  fs.symlinkSync(process.execPath, path.join(bin, "node"));
  for (const tool of ["python3", "bash"]) fs.symlinkSync(onPath(tool), path.join(bin, tool));
  fs.writeFileSync(path.join(bin, "claude"), FAKE_CLAUDE, { mode: 0o755 });
  const env = { HOME: home, PATH: [bin, "/usr/bin", "/bin"].join(path.delimiter), TMPDIR: os.tmpdir(),
    COMMUNICATE_DATA: path.join(home, "data"), COMM_STATE: path.join(home, "state"),
    CLAUDE_CONFIG_DIR: path.join(home, ".claude"), XDG_CONFIG_HOME: path.join(home, ".config"),
    XDG_RUNTIME_DIR: path.join(home, "runtime"), COM8_SOCK_DIR: path.join(home, "sockets"),
    COM8_SESSIONS_DIR: path.join(home, "sessions"), COMM_BUS_PORT: "0" };
  const run = (args, ok = true) => {
    const result = spawnSync(process.execPath, [com8, ...args], { env, encoding: "utf8", timeout: 90000 });
    assert.equal(result.status === 0, ok, `com8 ${args.join(" ")}\n${result.stdout}${result.stderr}`);
    return result;
  };
  return { home, bin, env, run, settings: path.join(home, ".claude/settings.json"), busDir: path.join(home, "state/bus") };
}

try {
  // 1. Uninstall stops the bus worker and owned local broker; dry-run only says so.
  {
    const box = sandbox("bus-running");
    fs.writeFileSync(path.join(box.bin, "codex"), FAKE_CODEX, { mode: 0o755 });
    const thread = "33333333-3333-4333-8333-333333333333";
    fs.mkdirSync(path.join(box.home, ".codex"));
    fs.writeFileSync(path.join(box.home, ".codex/session_index.jsonl"), JSON.stringify({ id: thread, thread_name: "uninstall-fixture" }) + "\n");
    box.run(["setup", "--no-clients"]);
    const cli = path.join(box.env.COMMUNICATE_DATA, "current/vendor/bin/communicate");
    const bus = (...args) => {
      const result = spawnSync(cli, ["bus", ...args], { env: box.env, encoding: "utf8", timeout: 60000 });
      assert.equal(result.status, 0, `bus ${args.join(" ")}\n${result.stdout}${result.stderr}`);
    };
    const record = { env: box.env, cli, pids: [] };
    fixtureBuses.push(record);
    bus("--hub", "local", "create", "photonics");
    bus("--hub", "local", "register", "self", "--bus", "photonics", "--kind", "codex", "--session", thread);
    const worker = JSON.parse(fs.readFileSync(path.join(box.busDir, "worker.json"))).pid;
    const broker = JSON.parse(fs.readFileSync(path.join(box.busDir, "server.json"))).pid;
    record.pids.push(worker, broker);
    assert(alive(worker) && alive(broker), "fixture bus did not start");

    const preview = box.run(["uninstall", "--dry-run"]);
    assert.match(preview.stdout, /would stop the bus worker/, "dry-run did not preview stopping the bus");
    assert(alive(worker) && alive(broker), "dry-run uninstall stopped the bus");

    const removed = box.run(["uninstall"]);
    assert(await gone(worker), "uninstall left the bus worker running\n" + removed.stdout);
    assert(await gone(broker), "uninstall left the local broker running\n" + removed.stdout);
    assert(removed.stdout.includes(box.busDir), "uninstall did not say where bus registrations remain\n" + removed.stdout);
  }

  // 2. A machine that never used the bus gets no bus state from uninstall.
  {
    const box = sandbox("never-used-bus");
    box.run(["setup", "--no-clients"]);
    box.run(["uninstall"]);
    assert(!fs.existsSync(box.busDir), "uninstall created bus state on a machine that never used the bus");
  }

  // 3. A remote enrollment survives uninstall; say where, never print its credential.
  {
    const box = sandbox("enrolled");
    box.run(["setup", "--no-clients"]);
    fs.mkdirSync(box.busDir, { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(box.busDir, "client.json"), JSON.stringify({ default: "https://hub.example",
      connections: { "https://hub.example": { url: "https://hub.example", principal: "p_fixture", token: "fixture-device-token" } } }), { mode: 0o600 });
    const removed = box.run(["uninstall"]);
    assert(removed.stdout.includes("https://hub.example"), "uninstall did not say this device remains enrolled at its hub\n" + removed.stdout);
    assert(!`${removed.stdout}${removed.stderr}`.includes("fixture-device-token"), "uninstall printed the device credential");
  }

  // 4. Replacing someone's own "communicate" marketplace names the enabled
  // plugins that stop resolving, in the preview and when applied; nothing is removed.
  {
    const box = sandbox("old-marketplace");
    const oldMarket = path.join(box.home, "old checkout/plugins");
    fs.mkdirSync(oldMarket, { recursive: true });
    const original = { extraKnownMarketplaces: { communicate: { source: { source: "directory", path: oldMarket } } },
      enabledPlugins: { "phone@communicate": true, "retired@communicate": false, "other@other": true } };
    fs.writeFileSync(box.settings, JSON.stringify(original));
    const preview = box.run(["setup", "--claude", "--dry-run"]);
    assert(preview.stdout.includes("phone@communicate"), "dry-run hid the enabled plugin that stops resolving\n" + preview.stdout);
    assert(!preview.stdout.includes("retired@communicate"), "dry-run listed a disabled plugin");
    assert.deepEqual(JSON.parse(fs.readFileSync(box.settings)), original, "dry-run changed settings");
    const applied = box.run(["setup", "--claude"]);
    assert(applied.stdout.includes("phone@communicate") && applied.stdout.includes(oldMarket),
      "setup replaced the marketplace without naming it and the plugin that stops resolving\n" + applied.stdout);
    const after = JSON.parse(fs.readFileSync(box.settings));
    assert.equal(after.enabledPlugins["phone@communicate"], true, "setup removed the user's plugin enablement");
    assert.equal(after.enabledPlugins["other@other"], true, "setup changed an unrelated plugin");
  }

  // A fresh machine has no marketplace of its own to replace and gets no warning.
  {
    const box = sandbox("fresh-marketplace");
    const { stdout } = box.run(["setup", "--claude"]);
    assert(!/warning/i.test(stdout), "fresh setup warned about replacing a marketplace\n" + stdout);
  }

  // 5. Plain non-interactive setup still applies its default, but shows the plan first.
  {
    const box = sandbox("plain-noninteractive");
    const { stdout } = box.run(["setup"]);
    const applied = stdout.indexOf("payload staged");
    const plan = stdout.slice(0, applied < 0 ? undefined : applied);
    assert(applied > 0 && /plan/i.test(plan), "non-interactive setup changed the installation before showing a plan\n" + stdout);
    assert(plan.includes("Claude Code"), "plan omitted the Claude Code registration\n" + stdout);
    assert(plan.includes("Codex"), "plan omitted the Codex decision\n" + stdout);
    assert(plan.includes("--dry-run"), "plan did not offer a preview without changes\n" + stdout);
    assert.equal(JSON.parse(fs.readFileSync(box.settings)).enabledPlugins?.["communicate@communicate"], true,
      "plain setup no longer applies its default selection");
    // Explicit selections and updates are already a decision; they keep their output.
    const update = box.run(["update"]).stdout;
    assert(!/plan/i.test(update.slice(0, Math.max(0, update.indexOf("current ->")))), "update started narrating a plan\n" + update);
  }

  console.log("PASS: setup-safety — uninstall stops the bus and reports what remains, replaced marketplaces are named, non-interactive setup shows its plan");
} finally {
  for (const { env, cli, pids } of fixtureBuses) {
    spawnSync(cli, ["bus", "stop"], { env, encoding: "utf8", timeout: 15000 });
    for (const pid of pids) if (alive(pid)) process.kill(pid, "SIGTERM");
  }
  fs.rmSync(temp, { recursive: true, force: true });
}
