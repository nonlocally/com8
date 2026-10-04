#!/usr/bin/env node
// Process death and I/O failure at installer boundaries, in a private HOME.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { withInstallLock, installLockStatus } from '../src/lifecycle.mjs';
import { buildIntegration, removeIntegration } from '../src/integration.mjs';

const pkg = fileURLToPath(new URL('..', import.meta.url));
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'com8-recovery-'));
const prior = { ...process.env };
const data = path.join(temp, 'data'), lock = path.join(data, 'install.lock');
let child;
try {
  Object.assign(process.env, { HOME: temp, COMMUNICATE_DATA: data, COMM_STATE: path.join(temp,'state'),
    CLAUDE_CONFIG_DIR: path.join(temp,'claude'), CODEX_HOME: path.join(temp,'codex'), COMM_BUS_PORT: '0',
    COMM_BUS_HUB: 'https://must-not-be-projected.invalid', COMM_BUS_TOKEN: 'fixture-private-value' });
  const moduleUrl = new URL('../src/lifecycle.mjs', import.meta.url).href;
  child = spawn(process.execPath, ['--input-type=module','-e',
    `import {withInstallLock} from ${JSON.stringify(moduleUrl)}; await withInstallLock(async()=>{console.log('locked'); await new Promise(()=>{setInterval(()=>{},1000)});});`],
    { env: process.env, stdio: ['ignore','pipe','pipe'] });
  await Promise.race([once(child.stdout,'data'), new Promise((_,reject)=>setTimeout(()=>reject(new Error('fixture lock timeout')),10000).unref())]);
  assert.equal(installLockStatus().pid,child.pid);
  assert.match(installLockStatus().status,/PID exists/);
  await assert.rejects(withInstallLock(async()=>assert.fail('concurrent installer entered')),/Installation is locked.*pid=/);
  const exited = once(child,'exit'); child.kill('SIGKILL'); await exited; child = null;
  const stale = installLockStatus();
  assert.match(stale.status,/not running/);
  assert.match(stale.recovery,/rename this exact lock directory/);
  await assert.rejects(withInstallLock(async()=>assert.fail('stale lock was automatically removed')),/never removed automatically/);
  assert(fs.existsSync(lock));
  const quarantine = path.join(data,'manually-inspected-old-lock'); fs.renameSync(lock,quarantine);
  await withInstallLock(async()=>assert.equal(installLockStatus().pid,process.pid));
  assert(!fs.existsSync(lock)); assert(fs.existsSync(quarantine));
  // A live owner's cleanup must not delete a new lock occupying the old path.
  await withInstallLock(async()=>{
    fs.renameSync(lock,path.join(data,'manually-moved-live-lock'));
    fs.mkdirSync(lock); fs.writeFileSync(path.join(lock,'owner.json'),JSON.stringify({pid:process.pid,token:'different-owner'}));
  });
  assert.equal(JSON.parse(fs.readFileSync(path.join(lock,'owner.json'))).token,'different-owner');
  fs.rmSync(lock,{recursive:true});

  const write = fs.writeFileSync;
  fs.writeFileSync = function(file,...args) {
    if (String(file).endsWith('/.mcp.json')) throw new Error('injected projection write failure');
    return write.call(this,file,...args);
  };
  try { assert.throws(()=>buildIntegration(pkg),/injected projection write failure/); }
  finally { fs.writeFileSync = write; }
  assert.deepEqual(fs.readdirSync(path.join(data,'integrations')),[],'partial projection survived failed write');
  const created = buildIntegration(pkg), reused = buildIntegration(pkg);
  assert(created.created); assert(!reused.created); assert.equal(created.root,reused.root);
  const descriptor = JSON.parse(fs.readFileSync(path.join(created.root,'plugins/communicate/.mcp.json'))).mcpServers.communicate;
  assert.equal(descriptor.env.COMM_BUS_PORT,'0');
  assert(!('COMM_BUS_HUB' in descriptor.env)); assert(!('COMM_BUS_TOKEN' in descriptor.env));
  process.env.COMM_BUS_PORT = '65536';
  assert.throws(()=>buildIntegration(pkg),/COMM_BUS_PORT must be/);
  process.env.COMM_BUS_PORT = '0';
  fs.writeFileSync(path.join(created.root,'user-note'),'preserve this');
  assert.equal(removeIntegration(created),false,'unowned projection file was deleted');
  fs.rmSync(path.join(created.root,'user-note'));
  assert(removeIntegration(created)); assert(removeIntegration(created));
  console.log('PASS: killed installer diagnostics, explicit lock recovery, replacement-lock ownership, failed projection cleanup and repeat setup');
} finally {
  child?.kill('SIGKILL');
  process.env = prior;
  fs.rmSync(temp,{recursive:true,force:true});
}
