#!/usr/bin/env node
// Actual lifecycle functions, fake global manager registry, local socket fixture.
// No host service manager, provider, daemon or current HOME configuration is used.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import { once } from "node:events";
import { serviceDefinition, installService, uninstallService, assertManagedPath, withInstallLock, writeJson, daemonRequest, hash } from "../src/lifecycle.mjs";

// Exercise the actual Linux command branch on macOS without a Linux manager.
if (process.argv.includes("--linux-fixture")) Object.defineProperty(process, "platform", { value: "linux" });
const priorEnv = { ...process.env };
const temp = fs.mkdtempSync("/tmp/com8-svc-");
const home = path.join(temp, "home"), data = path.join(home, "data"), state = path.join(home, "state");
const bin = path.join(home, "bin"), registry = path.join(temp, "global-manager.json"), calls = path.join(temp, "calls.jsonl");
let server;
try {
  for (const key of ["COMMUNICATE_DATA", "COMM_STATE", "XDG_STATE_HOME", "XDG_CONFIG_HOME"] ) delete process.env[key];
  process.env.HOME = os.userInfo().homedir;
  const standardMac = serviceDefinition("darwin", "/usr/bin/python3", "fixture");
  const standardLinux = serviceDefinition("linux", "/usr/bin/python3", "fixture");
  assert.equal(standardMac.label, "com.communicate.com8");
  assert.equal(standardLinux.label, "communicate-com8.service");
  Object.assign(process.env, { HOME: home, COMMUNICATE_DATA: data, COMM_STATE: state,
    XDG_CONFIG_HOME: path.join(home, ".config"), PATH: bin + path.delimiter + priorEnv.PATH,
    COM8_SELF: "fixture", COM8_SOCK: path.join(temp, "never-contact-this.sock"),
    CODEX_HOME: path.join(temp, "ambient-private-account"), SERVICE_REGISTRY: registry, SERVICE_CALLS: calls });
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.join(state, "com8"), { recursive: true });
  const definition = serviceDefinition(process.platform, "/usr/bin/python3", "fixture");
  const standard = process.platform === "darwin" ? standardMac : standardLinux;
  assert.notEqual(definition.label, standard.label, "isolated HOME collided with production label");
  const sameScope = serviceDefinition(process.platform, "/different/python3", "fixture");
  assert.equal(definition.label, sameScope.label, "interpreter change changed service identity");
  process.env.COMM_STATE = path.join(home, "other-state");
  assert.notEqual(serviceDefinition(process.platform, "/usr/bin/python3", "fixture").label, definition.label);
  process.env.COMM_STATE = state;
  const production = { active: true, enabled: true, path: standard.path, source: "/production/daemon.py" };
  const original = "# original operator service\n";
  fs.mkdirSync(path.dirname(definition.path), { recursive: true });
  fs.writeFileSync(definition.path, original, { mode: 0o640 });
  const readRegistry = () => JSON.parse(fs.readFileSync(registry));
  const writeRegistry = (value) => fs.writeFileSync(registry, JSON.stringify(value));
  writeRegistry({ [standard.label]: production, [definition.label]: { active: true, enabled: false, path: definition.path, source: "/legacy/daemon.py" } });
  const manager = `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path'),a=process.argv.slice(2),file=process.env.SERVICE_REGISTRY;
const jobs=JSON.parse(fs.readFileSync(file));
fs.appendFileSync(process.env.SERVICE_CALLS,JSON.stringify(a)+'\\n');
let label, op;
if(a[0]==='print'||a[0]==='bootout'){op=a[0];label=a[1].split('/').at(-1);}
else if(a[0]==='bootstrap'){op='load';label=path.basename(a[2],'.plist');}
else if(a.includes('show')){op='print';label=a[a.indexOf('show')+1];}
else if(a.includes('disable')){op='bootout';label=a.at(-1);}
else if(a.includes('enable')){op='enable';label=a.at(-1);}
else if(a.includes('start')){op='load';label=a.at(-1);}
else if(a.includes('daemon-reload'))process.exit(0);
if(op==='print'){
 if(process.env.FAIL_INSPECT==='1'){console.error('manager unavailable');process.exit(9);}
 const job=jobs[label];
 if(job?.unloadPolls){
  if(process.env.FAIL_PENDING_INSPECT==='1'){console.error('manager unavailable during removal');process.exit(9);}
  if(process.env.FOREIGN_PENDING_UNIT==='1')job.path+='.foreign';
  job.unloadPolls--;
  if(!job.unloadPolls){job.active=false;job.enabled=false;}
  fs.writeFileSync(file,JSON.stringify(jobs));
 }
 if(!job||(a[0]==='print'&&!job.active)){console.error('Could not find service');process.exit(113);}
 console.log(a[0]==='print'?'path = '+job.path:'FragmentPath='+(process.env.EMPTY_FRAGMENT==='1'?'':job.path)+'\\nActiveState='+(job.active?'active':'inactive')+'\\nUnitFileState='+(job.enabled?(job.runtime?'enabled-runtime':'enabled'):'disabled'));process.exit(0);
}
if(op==='bootout'){
 if(process.env.FAIL_UNLOAD==='1'){console.error('manager refused unload');process.exit(9);}
 if(process.env.NOOP_UNLOAD==='1')process.exit(0);
 if(jobs[label]){
  if(process.env.DELAY_UNLOAD_POLLS)jobs[label].unloadPolls=Number(process.env.DELAY_UNLOAD_POLLS);
  else{jobs[label].active=false;if(a[0]!=='bootout')jobs[label].enabled=false;}
 }
}
if(op==='enable'){
 jobs[label]??={active:false,path:path.join(process.env.XDG_CONFIG_HOME,'systemd/user',label)};
 jobs[label].enabled=true;jobs[label].runtime=a.includes('--runtime');
}
if(op==='load'){
 const unit=a[0]==='bootstrap'?a[2]:path.join(process.env.XDG_CONFIG_HOME,'systemd/user',label);
 const text=fs.readFileSync(unit,'utf8');
 if(process.env.FAIL_ORIGINAL==='1'&&text.startsWith('# original'))process.exit(9);
 if(process.env.FAIL_CURRENT==='1'&&!text.startsWith('# original'))process.exit(9);
 jobs[label]={...jobs[label],active:true,path:unit,source:text.startsWith('# original')?'/legacy/daemon.py':fs.realpathSync(path.join(process.env.COMMUNICATE_DATA,'current/vendor/lib/com8.py'))};
}
fs.writeFileSync(file,JSON.stringify(jobs));
`;
  for (const name of ["launchctl", "systemctl"]) fs.writeFileSync(path.join(bin, name), manager, { mode: 0o755 });
  const release = (name) => {
    const p = path.join(data, name);
    fs.mkdirSync(path.join(p, "vendor/lib"), { recursive: true });
    fs.writeFileSync(path.join(p, "vendor/lib/com8.py"), "# fixture source " + name);
    return p;
  };
  const first = release("one"), second = release("two");
  fs.symlinkSync(first, path.join(data, "current"));
  server = net.createServer((socket) => {
    let input = "";
    socket.on("data", (data) => { input += data; });
    socket.on("end", () => {
      const request = JSON.parse(input), jobs = readRegistry(), job = jobs[definition.label];
      if (!job?.active) { socket.end(); return; }
      if (request.op === "stop") { job.active = false; writeRegistry(jobs); socket.end('{"ok":true}\n'); return; }
      socket.end(JSON.stringify({ ok: true, self: { device: "fixture", source_file: job.source } }) + "\n");
    });
  });
  server.listen(path.join(state, "com8/com8.sock")); await once(server, "listening");

  // Global manager label collisions are rejected before bootout, even when the
  // on-disk file appears to belong to this installation.
  let jobs = readRegistry(); jobs[definition.label].path = path.join(temp, "other.plist"); writeRegistry(jobs);
  await assert.rejects(installService(), /another\/unknown unit/);
  assert(!fs.readFileSync(calls, "utf8").includes("bootout"));
  jobs[definition.label].path = definition.path; writeRegistry(jobs);

  let record = await installService();
  assert.equal(record.original.hash, hash(original));
  assert.equal(fs.readFileSync(record.original.backup, "utf8"), original);
  assert.equal(fs.statSync(record.original.backup).mode & 0o077, 0);
  assert(!record.environment.CODEX_HOME, "ambient account home leaked into service");
  assert(!record.environment.PATH.includes(bin), "invoking agent PATH leaked into service");
  const oldCalls = fs.readFileSync(calls, "utf8").split("\n").filter((s) => s.includes("bootout") || s.includes("bootstrap") || s.includes("disable") || s.includes("enable") || s.includes('"start"'));
  assert.equal(await installService(false, () => {}, record), record, "same release restarted its daemon");
  const newCalls = fs.readFileSync(calls, "utf8").split("\n").filter((s) => s.includes("bootout") || s.includes("bootstrap") || s.includes("disable") || s.includes("enable") || s.includes('"start"'));
  assert.deepEqual(newCalls, oldCalls);

  const firstOriginal = record.original;
  Object.assign(process.env, { COM8_SOCK_DIR: path.join(temp, "socks"), COM8_SESSIONS_DIR: path.join(temp, "sessions"),
    COM8_TMUX_SOCKET: path.join(temp, "tmux.sock"), CLAUDE_CONFIG_DIR: path.join(temp, "claude") });
  const inherited = ["COM8_SOCK_DIR", "COM8_SESSIONS_DIR", "COM8_TMUX_SOCKET", "CLAUDE_CONFIG_DIR", "CODEX_HOME"];
  // Equal explicit values must produce identical unit bytes regardless of flag
  // order or the order used when replaying the saved service environment.
  for (const platform of ["darwin", "linux"]) {
    const initial = serviceDefinition(platform, "/usr/bin/python3", "fixture", { inherit: inherited });
    const reordered = serviceDefinition(platform, "/usr/bin/python3", "fixture", { inherit: [...inherited].reverse() });
    const replayed = serviceDefinition(platform, "/usr/bin/python3", "fixture", { environment: initial.environment });
    assert.equal(reordered.content, initial.content, `${platform}: reordered flags changed equivalent unit bytes`);
    assert.equal(replayed.content, initial.content, `${platform}: saved environment changed equivalent unit bytes`);
  }
  record = await installService(false, () => {}, record, { inherit: inherited });
  assert.equal(record.environment.CODEX_HOME, process.env.CODEX_HOME);
  const beforeReordered = fs.readFileSync(calls, "utf8").split("\n").filter((s) => s.includes("bootout") || s.includes("bootstrap") || s.includes("disable") || s.includes("enable") || s.includes('"start"'));
  assert.equal(await installService(false, () => {}, record, { inherit: [...inherited].reverse() }), record,
    "equivalent flag order restarted the managed daemon");
  assert.equal(await installService(false, () => {}, record), record, "saved environment restarted the managed daemon");
  const afterReordered = fs.readFileSync(calls, "utf8").split("\n").filter((s) => s.includes("bootout") || s.includes("bootstrap") || s.includes("disable") || s.includes("enable") || s.includes('"start"'));
  assert.deepEqual(afterReordered, beforeReordered, "equivalent setup mutated manager state");
  process.env.CODEX_HOME = path.join(temp, "another-account");
  assert.equal(await installService(false, () => {}, record), record, "repeat captured a different ambient account");
  fs.unlinkSync(path.join(data, "current")); fs.symlinkSync(second, path.join(data, "current"));
  record = await installService(false, () => {}, record);
  assert.deepEqual(record.original, firstOriginal, "update lost original ownership chain");
  assert.equal(readRegistry()[definition.label].source, fs.realpathSync(path.join(second, "vendor/lib/com8.py")));
  fs.appendFileSync(definition.path, "# user edit\n");
  await assert.rejects(installService(false, () => {}, record), /changed outside/);
  assert.equal(await uninstallService(record), false, "uninstall removed user-edited unit");
  fs.writeFileSync(definition.path, record.content);
  process.env.FAIL_ORIGINAL = "1";
  await assert.rejects(uninstallService(record), /failed/);
  assert.equal(fs.readFileSync(definition.path, "utf8"), record.content, "failed original reload lost active unit");
  delete process.env.FAIL_ORIGINAL;
  await uninstallService(record);
  assert.equal(fs.readFileSync(definition.path, "utf8"), original, "uninstall did not restore original bytes");
  assert.equal(fs.statSync(definition.path).mode & 0o777, 0o640, "uninstall lost original unit mode");
  assert.equal(readRegistry()[definition.label].source, "/legacy/daemon.py", "uninstall did not restore original loaded job");
  if (process.platform === "linux") {
    assert.equal(readRegistry()[definition.label].enabled, false, "manually started original gained autostart");
    for (const enabled of [true, false]) {
      const jobs = readRegistry(); jobs[definition.label].active = false; jobs[definition.label].enabled = enabled; writeRegistry(jobs);
      if (enabled) {
        process.env.FAIL_CURRENT = "1";
        await assert.rejects(installService(), /failed/);
        delete process.env.FAIL_CURRENT;
        assert.equal(readRegistry()[definition.label].active, false, "failed replacement started original idle unit");
        assert.equal(readRegistry()[definition.label].enabled, true, "failed replacement lost original autostart");
        assert.equal(fs.readFileSync(definition.path, "utf8"), original);
      }
      const inactiveOriginal = await installService();
      assert.equal(inactiveOriginal.original.active, false);
      assert.equal(inactiveOriginal.original.enabled, enabled);
      await uninstallService(inactiveOriginal);
      const restored = readRegistry()[definition.label];
      assert.equal(restored.active, false, "original inactive service unexpectedly started");
      assert.equal(restored.enabled, enabled, "original inactive service lost enablement state");
      assert.equal(fs.readFileSync(definition.path, "utf8"), original);
    }
    // A temporary autostart setting must not become persistent on restoration.
    const jobs = readRegistry(); jobs[definition.label].enabled = true; jobs[definition.label].runtime = true; writeRegistry(jobs);
    const runtimeOriginal = await installService();
    await uninstallService(runtimeOriginal);
    assert.equal(readRegistry()[definition.label].runtime, true);
    assert.equal(readRegistry()[definition.label].active, false);
  }

  // A unit can be deleted while its manager job remains loaded. Missing files
  // must not cause ownership to be released without checking/stopping that job.
  const restoreManager = () => {
    const jobs = readRegistry();
    jobs[definition.label] = { active: true, enabled: true, path: definition.path, source: "/owned/daemon.py" };
    writeRegistry(jobs);
  };
  const missingRecord = JSON.parse(JSON.stringify({ ...record, original: null }));
  fs.rmSync(definition.path);
  restoreManager();
  const ledger = path.join(data, "missing-unit-ledger.json");
  writeJson(ledger, { service: missingRecord });
  const uninstallRecorded = async () => {
    const saved = JSON.parse(fs.readFileSync(ledger));
    if (await uninstallService(saved.service)) delete saved.service;
    writeJson(ledger, saved);
  };
  for (const failure of ["foreign", "inspect", "unload", "noop"]) {
    restoreManager();
    if (failure === "foreign") {
      const jobs = readRegistry(); jobs[definition.label].path = path.join(temp, "foreign-unit"); writeRegistry(jobs);
    } else process.env[{ inspect: "FAIL_INSPECT", unload: "FAIL_UNLOAD", noop: "NOOP_UNLOAD" }[failure]] = "1";
    const beforeCalls = fs.readFileSync(calls, "utf8").length;
    await assert.rejects(uninstallRecorded(), /another\/unknown unit|verify service ownership|refused unload|did not stop and disable/);
    assert.deepEqual(JSON.parse(fs.readFileSync(ledger)).service, missingRecord, "failed missing-unit uninstall lost ownership");
    assert.equal(readRegistry()[definition.label].active, true);
    assert(!fs.existsSync(definition.path), "failed missing-unit inspection recreated a unit");
    if (["foreign", "inspect"].includes(failure)) assert(!/bootout|disable/.test(fs.readFileSync(calls, "utf8").slice(beforeCalls)), "unverified manager job was stopped");
    delete process.env.FAIL_INSPECT; delete process.env.FAIL_UNLOAD; delete process.env.NOOP_UNLOAD;
  }
  if (process.platform === "linux") {
    process.env.EMPTY_FRAGMENT = "1";
    for (const [active, enabled] of [[true, false], [false, true]]) {
      const jobs = readRegistry(); jobs[definition.label] = { ...jobs[definition.label], active, enabled }; writeRegistry(jobs);
      await assert.rejects(uninstallRecorded(), /no FragmentPath/);
      assert.deepEqual(JSON.parse(fs.readFileSync(ledger)).service, missingRecord);
    }
    const jobs = readRegistry(); jobs[definition.label].active = false; jobs[definition.label].enabled = false; writeRegistry(jobs);
    await uninstallRecorded();
    assert(!JSON.parse(fs.readFileSync(ledger)).service, "explicit absent/inactive systemd unit was not removable");
    writeJson(ledger, { service: missingRecord });
    delete process.env.EMPTY_FRAGMENT;
  }
  restoreManager();
  await uninstallRecorded();
  assert(!JSON.parse(fs.readFileSync(ledger)).service);
  assert.equal(readRegistry()[definition.label].active, false);
  if (process.platform === "linux") assert.equal(readRegistry()[definition.label].enabled, false);
  assert(!fs.existsSync(definition.path));

  // Missing managed units still retain the original operator's backup lineage.
  restoreManager();
  process.env.FAIL_ORIGINAL = "1";
  await assert.rejects(uninstallService(record), /already missing.*ownership and original backup retained/);
  delete process.env.FAIL_ORIGINAL;
  assert(!fs.existsSync(definition.path), "failed original restoration left a replacement for an already missing unit");
  assert.equal(fs.readFileSync(record.original.backup, "utf8"), original);
  restoreManager();
  await uninstallService(record);
  assert.equal(fs.readFileSync(definition.path, "utf8"), original);
  assert.equal(fs.readFileSync(record.original.backup, "utf8"), original);
  assert.equal(readRegistry()[definition.label].source, "/legacy/daemon.py");
  fs.writeFileSync(definition.path, record.content);
  restoreManager();
  process.env.FAIL_UNLOAD = "1";
  await assert.rejects(uninstallService(record), /refused unload/);
  delete process.env.FAIL_UNLOAD;
  assert.equal(fs.readFileSync(definition.path, "utf8"), record.content, "rejected unload removed an existing unit");
  assert.equal(readRegistry()[definition.label].active, true);
  assert.deepEqual(readRegistry()[standard.label], production, "isolated service changed production manager job");

  // A successful manager command may return before the job is gone. Keep the
  // owned file and ledger until subsequent inspection confirms removal.
  process.env.DELAY_UNLOAD_POLLS = "3";
  const delayedCallsStart = fs.readFileSync(calls, "utf8").length;
  writeJson(ledger, { service: missingRecord });
  await uninstallRecorded();
  delete process.env.DELAY_UNLOAD_POLLS;
  assert(!fs.existsSync(definition.path), "confirmed delayed unload retained the owned unit");
  assert(!JSON.parse(fs.readFileSync(ledger)).service);
  assert.equal(readRegistry()[definition.label].active, false);
  const delayedCalls = fs.readFileSync(calls, "utf8").slice(delayedCallsStart).trim().split("\n").map(JSON.parse);
  const unloadIndex = delayedCalls.findIndex((a) => a.includes("bootout") || a.includes("disable"));
  assert(unloadIndex >= 0);
  assert.equal(delayedCalls.filter((a) => a.includes("bootout") || a.includes("disable")).length, 1,
    "asynchronous removal repeated a manager mutation");
  assert.equal(delayedCalls.slice(unloadIndex + 1).filter((a) => a.includes("print") || a.includes("show")).length, 3,
    "ownership released before the manager confirmed removal");
  assert.deepEqual(readRegistry()[standard.label], production);

  for (const failure of ["FAIL_PENDING_INSPECT", "FOREIGN_PENDING_UNIT"]) {
    fs.writeFileSync(definition.path, record.content);
    restoreManager();
    writeJson(ledger, { service: missingRecord });
    process.env.DELAY_UNLOAD_POLLS = "3";
    process.env[failure] = "1";
    await assert.rejects(uninstallRecorded(), /verify service ownership|another\/unknown unit/);
    delete process.env[failure]; delete process.env.DELAY_UNLOAD_POLLS;
    assert.deepEqual(JSON.parse(fs.readFileSync(ledger)).service, missingRecord);
    assert.equal(fs.readFileSync(definition.path, "utf8"), record.content,
      "unverified pending removal deleted the owned unit");
    assert.equal(readRegistry()[definition.label].active, true);
    assert.deepEqual(readRegistry()[standard.label], production);
  }

  // Managed-path guards must stop aliases before writes; system /tmp aliases and
  // the intentional current release symlink are not mistaken for config files.
  const outside = path.join(temp, "outside"); fs.mkdirSync(outside);
  const alias = path.join(home, "alias"); fs.symlinkSync(outside, alias);
  assert.throws(() => assertManagedPath(path.join(alias, "new.json")), /symlink/);
  assert.throws(() => writeJson(path.join(alias, "new.json"), {}), /symlink/);
  const otherState = path.join(home, "symlink-state"); fs.mkdirSync(otherState);
  fs.symlinkSync(path.join(state, "com8"), path.join(otherState, "com8"));
  process.env.COMM_STATE = otherState;
  assert.throws(() => daemonRequest("stop", 100, true), /symlink/, "service control followed a state alias into another daemon");
  process.env.COMM_STATE = state;
  process.env.COMMUNICATE_DATA = alias;
  await assert.rejects(withInstallLock(() => {}), /symlink/);
  assert.deepEqual(fs.readdirSync(outside), []);
  console.log(`PASS (${process.platform} fixture): scoped service ownership, explicit environment, unchanged refresh, original lineage/activity/enablement, missing-unit/rejected/delayed-unload ownership, restoration failure, and symlink boundaries`);
} finally {
  if (server) await new Promise((resolve) => server.close(resolve));
  for (const key of Object.keys(process.env)) if (!(key in priorEnv)) delete process.env[key];
  Object.assign(process.env, priorEnv);
  fs.rmSync(temp, { recursive: true, force: true });
}
