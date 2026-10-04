#!/usr/bin/env node
// Isolated registry model; --real-client uses the actual unauthenticated Claude
// CLI, with no thread/model/MCP execution and no access to live configuration.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

const pkg = path.resolve(fileURLToPath(new URL("..", import.meta.url))), cli = path.join(pkg, "src/com8.mjs");
const real = process.argv.includes("--real-client"), temp = fs.mkdtempSync(path.join(os.tmpdir(), "com8-claude-restoration-"));
const originalEnv = { ...process.env }, ID = "communicate@communicate";
const fake = `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path'),a=process.argv.slice(2),h=process.env.HOME,id='communicate@communicate';
const p=path.join(process.env.CLAUDE_CONFIG_DIR,'settings.json'), cache=path.join(h,'claude-cache.json');
let s=fs.existsSync(p)?JSON.parse(fs.readFileSync(p)):{}, c=fs.existsSync(cache)?JSON.parse(fs.readFileSync(cache)):null;
const save=()=>{fs.writeFileSync(p,JSON.stringify(s));if(c)fs.writeFileSync(cache,JSON.stringify(c));else fs.rmSync(cache,{force:true});};
if(a[0]==='--version'){console.log('Claude Code fixture');process.exit(0);}
if(a[0]!=='plugin')process.exit(7);
if(a[1]==='list'){
 const rows=c?[{id,scope:'user',version:c.version,enabled:s.enabledPlugins?.[id]!==false}]:[];
 if(fs.existsSync(path.join(h,'other-scope')))rows.push({id,scope:'project',enabled:false,version:'project'});
 console.log(JSON.stringify(rows));process.exit(0);
}
if(a[1]==='marketplace'){
 s.extraKnownMarketplaces??={};
 if(a[2]==='add')s.extraKnownMarketplaces.communicate={source:{source:'directory',path:a[3]}};
 else if(a[2]==='remove')delete s.extraKnownMarketplaces.communicate;
 else process.exit(8);
}else if(['install','update'].includes(a[1])){
 const root=s.extraKnownMarketplaces?.communicate?.source?.path;
 if(root?.includes('/integrations/')&&fs.existsSync(path.join(h,'fail-activation')))process.exit(9);
 c={version:JSON.parse(fs.readFileSync(path.join(root,'communicate/.claude-plugin/plugin.json'))).version};
 s.enabledPlugins??={};s.enabledPlugins[id]=true;
}else if(a[1]==='uninstall'){c=null;if(s.enabledPlugins)delete s.enabledPlugins[id];}
else if(['enable','disable'].includes(a[1])){s.enabledPlugins??={};s.enabledPlugins[id]=a[1]==='enable';}
else process.exit(10);
save();
`;

function run(entry, args, success = true) {
  const result = spawnSync(process.execPath, [entry, ...args], { encoding: "utf8", env: process.env, timeout: 60000 });
  assert.equal(result.status === 0, success, args.join(" ") + "\n" + result.stdout + result.stderr); return result;
}
function client(args) {
  const result = spawnSync("claude", args, { encoding: "utf8", env: process.env, timeout: 30000 });
  assert.equal(result.status, 0, "claude " + args.join(" ") + "\n" + result.stderr); return result.stdout;
}
function state() {
  const s = JSON.parse(fs.readFileSync(path.join(process.env.CLAUDE_CONFIG_DIR, "settings.json")));
  const rows = JSON.parse(client(["plugin", "list", "--json"]));
  const plugin = rows.find((row) => row.id === ID && row.scope === "user");
  return { market: s.extraKnownMarketplaces?.communicate ?? null, enabledSetting: s.enabledPlugins?.[ID] ?? null,
    installed: !!plugin, enabled: plugin?.enabled ?? false, version: plugin?.version ?? null,
    unrelated: { permissions: s.permissions, other: s.enabledPlugins?.["other@fixture"] } };
}
function fixture(name) {
  const home = path.join(temp, name); fs.mkdirSync(home, { mode: 0o700 });
  process.env = Object.fromEntries(Object.entries(originalEnv).filter(([key]) => !/^(CODEX_|COMM|COM8|ANU|CLAUDE|XDG|OPENAI)/.test(key) && !["TMUX", "TMUX_PANE"].includes(key)));
  Object.assign(process.env, { HOME: home, CLAUDE_CONFIG_DIR: path.join(home, ".claude"), CODEX_HOME: path.join(home, ".codex"),
    COMMUNICATE_DATA: path.join(home, "data"), COMM_STATE: path.join(home, "state"), COM8_SOCK_DIR: path.join(home, "sockets") });
  fs.mkdirSync(process.env.CLAUDE_CONFIG_DIR);
  fs.writeFileSync(path.join(process.env.CLAUDE_CONFIG_DIR, "settings.json"), JSON.stringify({ permissions: { allow: ["Read(/fixture)"] }, enabledPlugins: { "other@fixture": true } }));
  if (!real) { fs.mkdirSync(path.join(home, "bin")); fs.writeFileSync(path.join(home, "bin/claude"), fake, { mode: 0o755 }); process.env.PATH = path.join(home, "bin") + path.delimiter + originalEnv.PATH; }
  const market = path.join(home, "original-market"); fs.cpSync(path.join(pkg, "vendor/plugins"), market, { recursive: true });
  const manifest = path.join(market, "communicate/.claude-plugin/plugin.json"), plugin = JSON.parse(fs.readFileSync(manifest));
  plugin.version = "0.2.3-fixture"; fs.writeFileSync(manifest, JSON.stringify(plugin));
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
  fs.writeFileSync(file, JSON.stringify(manifest)); return path.join(next, "src/com8.mjs");
}
try {
  const next = nextArtifact();
  for (const variant of ["enabled", "disabled", "marketplace-only", "none", "current-alias"]) {
    const f = fixture(variant);
    let market = f.market, originalTarget;
    if (variant === "current-alias") {
      const old = path.join(f.home, "legacy-release"); fs.mkdirSync(path.join(old, "vendor"), { recursive: true });
      originalTarget = path.join(old, "vendor/plugins"); fs.renameSync(market, originalTarget);
      fs.mkdirSync(process.env.COMMUNICATE_DATA); fs.symlinkSync(old, path.join(process.env.COMMUNICATE_DATA, "current"));
      market = path.join(process.env.COMMUNICATE_DATA, "current/vendor/plugins");
    }
    if (variant !== "none") client(["plugin", "marketplace", "add", market]);
    if (["enabled", "disabled", "current-alias"].includes(variant)) client(["plugin", "install", ID, "--scope", "user"]);
    if (variant === "disabled") client(["plugin", "disable", ID, "--scope", "user"]);
    const before = state(); run(cli, ["setup", "--claude", "--no-service"]);
    const first = JSON.parse(fs.readFileSync(f.ledger)).clients.claude;
    assert.equal(first.previousInstalled, before.installed); assert.equal(first.previousPluginEnabled, before.enabled);
    if (originalTarget) {
      assert.deepEqual(first.previousMarketLiteral, before.market);
      assert.equal(first.previousMarket.source.path, fs.realpathSync(originalTarget));
      before.market.source.path = fs.realpathSync(originalTarget);
    }
    run(next, ["setup", "--claude", "--no-service"]); run(next, ["setup", "--claude", "--no-service"]); run(next, ["rollback"]);
    const current = JSON.parse(fs.readFileSync(f.ledger)).clients.claude;
    for (const key of ["previousMarket", "previousEnabled", "previousInstalled", "previousPluginEnabled"]) assert.deepEqual(current[key], first[key]);
    run(next, ["uninstall", "--claude"]); assert.deepEqual(state(), before, variant + ": original registry/settings not restored");
  }
  const edited = fixture("user-disabled"); run(cli, ["setup", "--claude", "--no-service"]);
  client(["plugin", "disable", ID, "--scope", "user"]); const kept = state();
  assert.match(run(cli, ["uninstall", "--claude"], false).stderr, /changed after setup/); assert.deepEqual(state(), kept);
  client(["plugin", "enable", ID, "--scope", "user"]); run(cli, ["uninstall", "--claude"]);
  if (!real) {
    const f = fixture("activation-failure"); client(["plugin", "marketplace", "add", f.market]); client(["plugin", "install", ID, "--scope", "user"]);
    client(["plugin", "disable", ID, "--scope", "user"]); const before = state(); fs.writeFileSync(path.join(f.home, "fail-activation"), "fixture");
    run(cli, ["setup", "--claude", "--no-service"], false); assert.deepEqual(state(), before, "failed activation lost disabled original");
    const scoped = fixture("other-scope"); fs.writeFileSync(path.join(scoped.home, "other-scope"), "project"); const settings = fs.readFileSync(path.join(process.env.CLAUDE_CONFIG_DIR, "settings.json"), "utf8");
    assert.match(run(cli, ["setup", "--claude", "--no-service"], false).stderr, /Another Claude scope/);
    assert.equal(fs.readFileSync(path.join(process.env.CLAUDE_CONFIG_DIR, "settings.json"), "utf8"), settings);
  }
  console.log(`PASS (${real ? "actual unauthenticated Claude CLI" : "isolated fake client"}): original user-scope installed/enabled restoration, first-original upgrade/rollback lineage, changed-user-enablement refusal${real ? "" : ", failed activation and scope conflict"}`);
} finally { process.env = originalEnv; fs.rmSync(temp, { recursive: true, force: true }); }
