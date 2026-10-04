#!/usr/bin/env node
// Changed installation boundaries only: immutable releases, rollback, owned
// settings/links, package tamper detection and preserved state. No live clients.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { serviceDefinition } from "../src/lifecycle.mjs";

const pkg = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "com8-lifecycle-"));
const home = path.join(temp, "home with spaces"), data = path.join(home, "data"), state = path.join(home, "state");
fs.mkdirSync(path.join(home, "bin"), { recursive: true });
fs.mkdirSync(path.join(home, ".claude"));
const settings = path.join(home, ".claude/settings.json");
fs.writeFileSync(settings, JSON.stringify({ sentinel: "keep", enabledPlugins: { "other@other": true } }));
fs.writeFileSync(path.join(home, "bin/claude"), `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path');
const a=process.argv.slice(2), home=process.env.HOME;
const installed=path.join(home,'claude-installed');
if (a[0]==='--version') { console.log('fixture'); process.exit(0); }
if (fs.existsSync(path.join(home,'fail-client'))) process.exit(9);
if(a[0]==='plugin'&&['update','install'].includes(a[1]))fs.writeFileSync(installed,'user');
if(a[0]==='plugin'&&a[1]==='uninstall')fs.rmSync(installed,{force:true});
if (a[0]==='plugin' && a[1]==='list') {
 if(!fs.existsSync(installed)){console.log('[]');process.exit(0);}
 const market=JSON.parse(fs.readFileSync(path.join(home,'.claude/settings.json'))).extraKnownMarketplaces.communicate.source.path;
 const p=path.join(market,'communicate/.claude-plugin/plugin.json');
 console.log(JSON.stringify([{id:'communicate@communicate',scope:'user',enabled:true,version:JSON.parse(fs.readFileSync(p)).version}]));
}
`, { mode: 0o755 });
const env = { ...process.env, HOME: home, COMMUNICATE_DATA: data, COMM_STATE: state,
  CLAUDE_CONFIG_DIR: path.join(home, ".claude"), CODEX_HOME: path.join(home, ".codex"),
  XDG_CONFIG_HOME: path.join(home, ".config"), PATH: path.join(home, "bin") + path.delimiter + process.env.PATH };
for (const key of ["CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "COM8_SOCK", "COM8_SOCK_DIR", "COM8_SESSIONS_DIR"]) delete env[key];
const assert = (condition, text) => { if (!condition) throw new Error(text); };
const run = (entry, args, ok = true) => {
  const r = spawnSync(process.execPath, [entry, ...args], { env, encoding: "utf8", timeout: 45000 });
  assert((r.status === 0) === ok, `${args.join(" ")}: ${r.stdout}\n${r.stderr}`);
  return r;
};
const cli = path.join(pkg, "src/com8.mjs");
const installed = path.join(data, "current/src/com8.mjs");
const target = () => fs.realpathSync(path.join(data, "current"));
const hash = (file) => createHash("sha256").update(fs.readFileSync(file)).digest("hex");
try {
  run(cli, ["setup", "--no-clients", "--dry-run"]);
  assert(!fs.existsSync(data), "dry-run created installation state");
  run(cli, ["setup", "--claude"]);
  const first = target();
  assert(fs.existsSync(path.join(first, "vendor/lib/com8.py")), "durable kernel absent");
  assert(fs.existsSync(path.join(first, "vendor/lib/bus_broker.py")), "bus absent");
  assert(!fs.existsSync(path.join(first, "vendor/lib/phone")), "phone leaked into core");
  assert(fs.readlinkSync(path.join(data, "bin/com8")) === "../current/src/com8.mjs", "stable CLI link missing");
  const untouched = path.join(state, "com8/mail/kept/inbox.jsonl");
  fs.mkdirSync(path.dirname(untouched), { recursive: true });
  fs.writeFileSync(untouched, '{"text":"preserved fixture"}\n');
  run(installed, ["setup", "--claude"]);
  assert(target() === first, "repeat setup replaced release directory");

  const next = path.join(temp, "next artifact");
  fs.mkdirSync(next);
  for (const name of ["src", "vendor", "package.json", "LICENSE"]) fs.cpSync(path.join(pkg, name), path.join(next, name), { recursive: true });
  fs.symlinkSync(path.join(pkg, "node_modules"), path.join(next, "node_modules"));
  const metadata = JSON.parse(fs.readFileSync(path.join(next, "package.json")));
  metadata.version = "0.3.1-fixture";
  fs.writeFileSync(path.join(next, "package.json"), JSON.stringify(metadata));
  for (const kind of [".claude-plugin", ".codex-plugin"]) {
    const p = path.join(next, "vendor/plugins/communicate", kind, "plugin.json");
    const plugin = JSON.parse(fs.readFileSync(p)); plugin.version = metadata.version;
    fs.writeFileSync(p, JSON.stringify(plugin));
  }
  const manifestFile = path.join(next, "vendor/release.json");
  const manifest = JSON.parse(fs.readFileSync(manifestFile)); manifest.version = metadata.version;
  for (const name of Object.keys(manifest.files)) manifest.files[name] = hash(path.join(next, "vendor", name));
  for (const name of Object.keys(manifest.packageFiles)) manifest.packageFiles[name] = hash(path.join(next, name));
  fs.writeFileSync(manifestFile, JSON.stringify(manifest));
  const nextCli = path.join(next, "src/com8.mjs");
  fs.writeFileSync(path.join(home, "fail-client"), "fixture");
  run(nextCli, ["setup", "--claude"], false);
  assert(target() === first, "failed client activation changed current release");
  fs.rmSync(path.join(home, "fail-client"));
  // Refuse unknown executables before switching payloads or refreshing clients.
  const stableBin = path.join(data, "bin/com8");
  fs.unlinkSync(stableBin); fs.writeFileSync(stableBin, "owned by another tool");
  run(nextCli, ["setup", "--claude"], false);
  assert(target() === first && fs.readFileSync(stableBin, "utf8") === "owned by another tool", "executable conflict modified the installation");
  fs.unlinkSync(stableBin); fs.symlinkSync("../current/src/com8.mjs", stableBin);

  // Service-manager fixture, never the host's actual launchctl/systemctl. A
  // failed replacement must restore /current BEFORE reloading the previous unit.
  const manager = process.platform === "darwin" ? "launchctl" : "systemctl";
  const beforeDefinitionEnv = { ...process.env };
  Object.assign(process.env, env);
  const unitFile = serviceDefinition(process.platform, "/usr/bin/python3", "fixture").path;
  for (const key of Object.keys(process.env)) if (!(key in beforeDefinitionEnv)) delete process.env[key];
  Object.assign(process.env, beforeDefinitionEnv);
  env.SERVICE_TEST_PATH = unitFile;
  fs.mkdirSync(path.dirname(unitFile), { recursive: true }); fs.writeFileSync(unitFile, "previous fixture definition");
  fs.writeFileSync(path.join(home, "bin", manager), `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path'),a=process.argv.slice(2);
const state=path.join(process.env.HOME,'service-manager-state');
const job=fs.existsSync(state)?JSON.parse(fs.readFileSync(state)):{active:true,enabled:true};
if(a[0]==='print'||a.includes('show')){
 if(a[0]==='print'&&!job.active){console.error('Could not find service');process.exit(113);}
 console.log(a[0]==='print'?'path = '+process.env.SERVICE_TEST_PATH:'FragmentPath='+process.env.SERVICE_TEST_PATH+'\\nActiveState='+(job.active?'active':'inactive')+'\\nUnitFileState='+(job.enabled?'enabled':'disabled'));process.exit(0);
}
if(a[0]==='bootout')job.active=false;
if(a.includes('disable')){job.active=false;job.enabled=false;}
if(a.includes('enable'))job.enabled=true;
if (a.includes('bootstrap') || a.includes('start')) {
 const target=fs.realpathSync(path.join(process.env.COMMUNICATE_DATA,'current'));
 fs.appendFileSync(path.join(process.env.HOME,'service-calls'),target+'\\n');
 const version=JSON.parse(fs.readFileSync(path.join(target,'package.json'))).version;
 if (version==='0.3.1-fixture') process.exit(9);
 job.active=true;
}
fs.writeFileSync(state,JSON.stringify(job));
`, { mode: 0o755 });
  run(nextCli, ["setup", "--no-clients", "--service"], false);
  const serviceCalls = fs.readFileSync(path.join(home, "service-calls"), "utf8").trim().split("\n");
  assert(serviceCalls.length === 2 && serviceCalls[1] === first, "previous service reloaded before pointer restoration");
  assert(target() === first && fs.readFileSync(unitFile, "utf8") === "previous fixture definition", "failed service activation lost previous unit/payload");
  run(nextCli, ["setup", "--claude"]);
  const second = target();
  assert(second !== first && fs.existsSync(first), "upgrade lost the previous release");
  run(installed, ["rollback"]);
  assert(target() === first, "rollback did not restore previous release");
  assert(fs.readFileSync(untouched, "utf8").includes("preserved"), "rollback altered identity state");
  run(installed, ["rollback"]);
  assert(target() === second, "rollback did not retain forward recovery");

  // An obsolete uninstaller must not deregister another installation.
  const modified = JSON.parse(fs.readFileSync(settings));
  modified.extraKnownMarketplaces.communicate.source.path = path.join(home, "different-checkout/plugins");
  fs.writeFileSync(settings, JSON.stringify(modified));
  run(installed, ["uninstall", "--claude"]);
  assert(JSON.parse(fs.readFileSync(settings)).extraKnownMarketplaces.communicate.source.path.includes("different-checkout"), "uninstall removed a replacement registration");
  modified.extraKnownMarketplaces.communicate.source.path = path.join(JSON.parse(fs.readFileSync(path.join(data,"install.json"))).integration.root, "plugins");
  fs.writeFileSync(settings, JSON.stringify(modified));
  run(installed, ["setup", "--claude"]);
  run(installed, ["uninstall"]);
  assert(fs.existsSync(untouched), "uninstall removed durable mail");
  assert(JSON.parse(fs.readFileSync(settings)).enabledPlugins["other@other"], "uninstall damaged another plugin");
  run(nextCli, ["setup", "--no-clients"]);
  assert(fs.existsSync(untouched), "reinstall removed durable mail");
  fs.appendFileSync(path.join(next, "vendor/lib/com8.py"), "\n# tampered fixture\n");
  run(nextCli, ["setup", "--no-clients"], false);
  assert(target() === second, "tampered payload was activated");

  // Render both platform definitions; no service manager is invoked here.
  const mac = serviceDefinition("darwin", "/tmp/python & tools/python3", "fixture");
  const linux = serviceDefinition("linux", "/tmp/python tools/python3", "fixture");
  assert(mac.content.includes("python &amp; tools"), "launchd XML path was not escaped");
  assert(linux.content.includes('ExecStart="/tmp/python tools/python3"'), "systemd path with spaces was not quoted");
  assert(mac.label.startsWith("com.communicate.com8") && linux.label.startsWith("communicate-com8"), "service identity prefix changed");
  console.log("PASS: lifecycle — immutable upgrade, failed activation, rollback, ownership, preserved state, tamper detection and platform service rendering");
} finally { fs.rmSync(temp, { recursive: true, force: true }); }
