#!/usr/bin/env node
// Default: deterministic client/config API failures. --real-client: actual
// unauthenticated Codex CLI in fresh homes; no model, MCP, service or live config.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { readCodexSettings, writeCodexSettings } from "../src/codex-settings.mjs";

const pkg = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const cli = path.join(pkg, "src/com8.mjs"), real = process.argv.includes("--real-client");
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "com8-codex-restoration-"));
const originalEnv = { ...process.env };
const ID = "communicate@communicate";
const custom = { enabled: false, mcp_servers: { communicate: { tools: { bus_send: { approval_mode: "prompt" } } } } };

const fake = `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline');
const h=process.env.HOME, p=path.join(h,'client.json'), a=process.argv.slice(2), id='communicate@communicate';
const read=()=>JSON.parse(fs.readFileSync(p)), save=s=>fs.writeFileSync(p,JSON.stringify(s));
const flag=n=>path.join(h,n), json=v=>console.log(JSON.stringify(v));
fs.appendFileSync(path.join(h,'calls'),JSON.stringify(a)+'\\n');
if(a[0]==='--version'){console.log('codex-cli 0.156.1-fixture');process.exit(0);}
if(process.env.CODEX_HOME&&!fs.existsSync(process.env.CODEX_HOME)){console.error('failed to resolve CODEX_HOME: path does not exist');process.exit(1);}
if(a[0]==='app-server'){
 fs.appendFileSync(path.join(h,'api-pids'),process.pid+'\\n');
 readline.createInterface({input:process.stdin}).on('line',line=>{
  const q=JSON.parse(line),s=read(),error=message=>json({id:q.id,error:{code:-32602,message}});
  if(q.method==='initialize'){json({id:q.id,result:{}});return;}
  if(fs.existsSync(flag('unsupported-api'))){error('unsupported');return;}
  if(fs.existsSync(flag('malformed-api'))){console.log('malformed fixture');return;}
  if(q.method==='config/read'){
   json({id:q.id,result:{layers:[{name:{type:'user',file:path.join(process.env.CODEX_HOME,'config.toml')},version:String(s.version),config:{plugins:s.settings?{[id]:s.settings}:{}}},
     ...(s.layered?[{name:{type:'system',file:'/fixture/config.toml'},version:'managed',config:{plugins:{[id]:{enabled:false}}}}]:[])]}});return;
  }
  if(q.method==='config/value/write'){
   if(q.params.expectedVersion!==String(s.version)||fs.existsSync(flag('version-conflict'))){error('version conflict');return;}
   if(fs.existsSync(flag('fail-activation'))&&s.market?.includes('/integrations/')&&q.params.value?.enabled){fs.rmSync(flag('fail-activation'));error('injected activation failure');return;}
   if(q.params.keyPath!=='plugins."'+id+'"')throw Error('unexpected write');
   s.settings=q.params.value;s.version++;save(s);json({id:q.id,result:{status:'ok',version:String(s.version)}});return;
  }
  error('unexpected method');
 });
}else{
 const s=read();
 if(a[0]!=='plugin')process.exit(7);
 if(a[1]==='marketplace'){
  if(a[2]==='list')json(s.market?[{name:'communicate',root:s.market,marketplaceSource:{sourceType:'local',source:s.market}}]:[]);
  else if(a[2]==='add'){if(s.market&&s.market!==a[3])process.exit(3);s.market=a[3];}
  else if(a[2]==='remove'){s.market=null;}
  else process.exit(8);
 }else if(a[1]==='list')json({installed:s.installed?[{pluginId:id,installed:true,enabled:s.settings?.enabled!==false}]:[],available:[{pluginId:id,installed:false,enabled:false}]});
 else if(a[1]==='add'){if(!s.market)process.exit(9);s.installed=true;s.settings={...(s.settings||{}),enabled:true};s.version++;}
 else if(a[1]==='remove'){s.installed=false;s.settings=null;s.version++;}
 else process.exit(10);
 save(s);
}
`;

function run(entry, args, success = true) {
  const result = spawnSync(process.execPath, [entry, ...args], { env: process.env, encoding: "utf8", timeout: 60000 });
  assert.equal(result.status === 0, success, `${args.join(" ")}\n${result.stdout}${result.stderr}`);
  return result;
}
function codex(args) {
  const result = spawnSync("codex", args, { env: process.env, encoding: "utf8", timeout: 30000 });
  assert.equal(result.status, 0, `codex ${args.join(" ")}\n${result.stderr}`);
  return result.stdout;
}
async function state() {
  const markets = JSON.parse(codex(["plugin", "marketplace", "list", "--json"]));
  const market = (Array.isArray(markets) ? markets : markets.marketplaces).find((row) => row.name === "communicate");
  const plugin = JSON.parse(codex(["plugin", "list", "--marketplace", "communicate", "--json"])).installed.find((row) => row.pluginId === ID);
  return { root: market?.marketplaceSource?.source || market?.root || null, installed: !!plugin,
    enabled: plugin?.enabled ?? false, settings: await readCodexSettings() };
}
function fixture(name, { createCodexHome = true } = {}) {
  const home = path.join(temp, name); fs.mkdirSync(home, { mode: 0o700 });
  process.env = Object.fromEntries(Object.entries(originalEnv).filter(([key]) => !/^(CODEX_|COMM|COM8|ANU|CLAUDE|XDG|OPENAI)/.test(key) && !["TMUX", "TMUX_PANE"].includes(key)));
  Object.assign(process.env, { HOME: home, CODEX_HOME: path.join(home, ".codex"), CLAUDE_CONFIG_DIR: path.join(home, ".claude"),
    COMMUNICATE_DATA: path.join(home, "data"), COMM_STATE: path.join(home, "state"), COM8_SOCK_DIR: path.join(home, "sockets"), COMM_BUS_PORT: "0" });
  if (createCodexHome) fs.mkdirSync(process.env.CODEX_HOME, { mode: 0o700 });
  if (!real) {
    fs.mkdirSync(path.join(home, "bin"));
    fs.writeFileSync(path.join(home, "bin/codex"), fake, { mode: 0o755 });
    // Doctor inspects both installed clients; keep this fake-client suite from
    // invoking a host Claude binary even when one is on the inherited PATH.
    fs.writeFileSync(path.join(home, "bin/claude"), "#!/bin/sh\nprintf '[]\\n'\n", { mode: 0o755 });
    process.env.PATH = path.join(home, "bin") + path.delimiter + originalEnv.PATH;
    fs.writeFileSync(path.join(home, "client.json"), JSON.stringify({ market: null, installed: false, settings: null, version: 0, unrelated: "preserve" }));
  }
  const market = path.join(home, "original-market"); fs.mkdirSync(market);
  for (const name of [".agents", "plugins"]) fs.cpSync(path.join(pkg, "vendor", name), path.join(market, name), { recursive: true });
  const pluginFile = path.join(market, "plugins/communicate/.codex-plugin/plugin.json");
  const manifest = JSON.parse(fs.readFileSync(pluginFile)); manifest.version = "0.2.3-fixture"; fs.writeFileSync(pluginFile, JSON.stringify(manifest));
  return { home, market, ledger: path.join(process.env.COMMUNICATE_DATA, "install.json") };
}

function nextArtifact() {
  const next = path.join(temp, "next-artifact"); fs.mkdirSync(next);
  for (const name of ["src", "vendor", "package.json", "LICENSE"]) fs.cpSync(path.join(pkg, name), path.join(next, name), { recursive: true });
  fs.symlinkSync(path.join(pkg, "node_modules"), path.join(next, "node_modules"));
  const metadata = JSON.parse(fs.readFileSync(path.join(next, "package.json"))); metadata.version = "0.3.1-fixture";
  fs.writeFileSync(path.join(next, "package.json"), JSON.stringify(metadata));
  const file = path.join(next, "vendor/release.json"), manifest = JSON.parse(fs.readFileSync(file)); manifest.version = metadata.version;
  for (const [prefix, rows] of [["vendor", manifest.files], ["", manifest.packageFiles]])
    for (const name of Object.keys(rows)) rows[name] = createHash("sha256").update(fs.readFileSync(path.join(next, prefix, name))).digest("hex");
  fs.writeFileSync(file, JSON.stringify(manifest));
  return path.join(next, "src/com8.mjs");
}

try {
  const next = nextArtifact();
  fixture("fresh-explicit-home", { createCodexHome: false });
  run(cli, ["setup", "--codex", "--no-service", "--dry-run"]);
  assert(!fs.existsSync(process.env.CODEX_HOME), "dry-run created the selected Codex home");
  run(cli, ["doctor"]);
  assert(!fs.existsSync(process.env.CODEX_HOME), "doctor created the selected Codex home");
  run(cli, ["setup", "--codex", "--no-service"]);
  assert.equal(fs.statSync(process.env.CODEX_HOME).mode & 0o777, 0o700, "fresh client home is not private");
  assert((await state()).enabled, "fresh explicit Codex home did not install the plugin");
  run(cli, ["uninstall", "--codex"]);
  assert.equal((await state()).installed, false, "fresh client plugin survived uninstall");

  fixture("existing-home-mode");
  fs.chmodSync(process.env.CODEX_HOME, 0o750);
  run(cli, ["setup", "--codex", "--no-service"]);
  assert.equal(fs.statSync(process.env.CODEX_HOME).mode & 0o777, 0o750, "setup changed an existing client-home mode");
  run(cli, ["uninstall", "--codex"]);

  const linked = fixture("symlinked-client-home", { createCodexHome: false });
  const outside = path.join(temp, "outside-client-home"); fs.mkdirSync(outside, { mode: 0o700 });
  fs.writeFileSync(path.join(outside, "sentinel"), "preserve");
  fs.symlinkSync(outside, process.env.CODEX_HOME);
  assert.match(run(cli, ["setup", "--codex", "--no-service"], false).stderr, /Refusing symlinked managed path/);
  assert.deepEqual(fs.readdirSync(outside), ["sentinel"], "setup followed a client-home symlink");
  assert(!fs.existsSync(path.join(linked.home, "calls")), "symlink refusal invoked the client");

  for (const variant of ["enabled", "disabled-custom", "marketplace-only", "none"]) {
    const f = fixture(variant);
    if (variant !== "none") codex(["plugin", "marketplace", "add", f.market]);
    if (["enabled", "disabled-custom"].includes(variant)) codex(["plugin", "add", ID]);
    if (variant === "disabled-custom") await writeCodexSettings(custom, await readCodexSettings());
    const before = await state();
    run(cli, ["setup", "--codex", "--no-service"]);
    const first = JSON.parse(fs.readFileSync(f.ledger)).clients.codex.original;
    assert.equal(fs.statSync(f.ledger).mode & 0o777, 0o600);
    assert.deepEqual(first.settings, before.settings); assert.equal(first.installed, before.installed); assert.equal(first.enabled, before.enabled);
    assert((await state()).enabled);
    if (variant === "disabled-custom") assert.deepEqual((await state()).settings, { ...custom, enabled: true });
    run(next, ["setup", "--codex", "--no-service"]);
    run(next, ["setup", "--codex", "--no-service"]);
    run(next, ["rollback"]);
    assert.deepEqual(JSON.parse(fs.readFileSync(f.ledger)).clients.codex.original, first, "update/rollback replaced the first original");
    run(next, ["uninstall", "--codex"]);
    assert.deepEqual(await state(), before, `${variant}: original state not restored`);
    const doctor = run(next, ["doctor"]);
    const status = !before.installed ? "not installed" : before.enabled ? "installed and enabled" : "installed but disabled";
    assert.match(doctor.stdout, new RegExp("codex plugin\\s+" + status));
    if (!real) assert.equal(JSON.parse(fs.readFileSync(path.join(f.home, "client.json"))).unrelated, "preserve");
  }
  const changed = fixture("user-edits"); run(cli, ["setup", "--codex", "--no-service"]);
  await writeCodexSettings(custom, await readCodexSettings()); const preserved = await state();
  for (const args of [["setup", "--codex", "--no-service"], ["uninstall", "--codex"]]) {
    assert.match(run(cli, args, false).stderr, /changed after setup/);
    assert.deepEqual(await state(), preserved);
  }
  await writeCodexSettings({ enabled: true }, await readCodexSettings()); run(cli, ["uninstall", "--codex"]);

  if (!real) {
    const failed = fixture("failed-activation"); codex(["plugin", "marketplace", "add", failed.market]); codex(["plugin", "add", ID]);
    await writeCodexSettings(custom, await readCodexSettings()); const original = await state();
    fs.writeFileSync(path.join(failed.home, "fail-activation"), "once");
    run(cli, ["setup", "--codex", "--no-service"], false);
    assert.deepEqual(await state(), original, "activation rollback lost original plugin/cache/policy");
    for (const flag of ["unsupported-api", "malformed-api", "version-conflict"]) {
      const f = fixture(flag); codex(["plugin", "marketplace", "add", f.market]); codex(["plugin", "add", ID]);
      const original = JSON.parse(fs.readFileSync(path.join(f.home, "client.json")));
      const calls = fs.readFileSync(path.join(f.home, "calls"), "utf8").split("\n").length;
      fs.writeFileSync(path.join(f.home, flag), "fixture"); run(cli, ["setup", "--codex", "--no-service"], false);
      assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.home, "client.json"))), original);
      const mutations = fs.readFileSync(path.join(f.home, "calls"), "utf8").split("\n").slice(calls - 1).filter(Boolean).map(JSON.parse)
        .filter((a) => a[0] === "plugin" && (a[1] === "add" || a[1] === "remove" || ["add", "remove"].includes(a[2])));
      assert.deepEqual(mutations, [], "unsupported API mutated registration");
    }
    const old = fixture("old-ledger"); run(cli, ["setup", "--codex", "--no-service"]);
    const ledger = JSON.parse(fs.readFileSync(old.ledger)); delete ledger.clients.codex.original; fs.writeFileSync(old.ledger, JSON.stringify(ledger));
    const retained = await state(); assert.match(run(cli, ["uninstall", "--codex"], false).stderr, /Earlier installer/); assert.deepEqual(await state(), retained);
    for (const home of fs.readdirSync(temp).map((name) => path.join(temp, name))) {
      const pidFile = path.join(home, "api-pids"); if (!fs.existsSync(pidFile)) continue;
      for (const pid of fs.readFileSync(pidFile, "utf8").trim().split("\n").map(Number)) {
        assert.throws(() => process.kill(pid, 0), { code: "ESRCH" }, "config API subprocess leaked");
      }
    }
  }
  console.log(`PASS (${real ? "actual unauthenticated Codex CLI" : "isolated fake client"}): fresh explicit client home, dry-run/doctor read-only, existing modes and symlinks, original installed/enabled/settings restoration, first-original lineage, upgrade/rollback, changed-user-settings refusal, doctor state${real ? "" : ", activation/API failure recovery and process cleanup"}`);
} finally { process.env = originalEnv; fs.rmSync(temp, { recursive: true, force: true }); }
