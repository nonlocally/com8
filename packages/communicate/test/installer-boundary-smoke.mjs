#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const pkg = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const cli = path.join(pkg, 'src/com8.mjs');
// macOS's per-user TMPDIR leaves too little room for the Unix control socket.
// Keep both the state root and peer sockets short and private, like other
// daemon fixtures; COM8_SOCK_DIR alone does not move state/com8/com8.sock.
const temp = fs.mkdtempSync(path.join(process.platform === 'darwin' ? '/tmp' : os.tmpdir(), 'com8-install-'));
const homes = [];
const daemons = [];
function fixture(name) {
  const home = path.join(temp, name); homes.push(home);
  fs.mkdirSync(path.join(home, 'bin'), { recursive: true });
  const env = { ...process.env, HOME: home, COMMUNICATE_DATA: path.join(home, 'data'),
    COMM_STATE: path.join(home, 'state'), CLAUDE_CONFIG_DIR: path.join(home, '.claude'),
    CODEX_HOME: path.join(home, '.codex'), XDG_CONFIG_HOME: path.join(home, '.config'),
    PATH: path.join(home, 'bin') + path.delimiter + process.env.PATH };
  for (const key of ['COM8_SOCK','COM8_SOCK_DIR','COM8_SESSIONS_DIR','COM8_DAEMON_DIR','CLAUDE_CODE_MESSAGING_SOCKET','COMMUNICATE_HOME','CODEX_THREAD_ID']) delete env[key];
  env.COM8_SOCK_DIR = path.join(home, 'socks');
  env.COM8_SESSIONS_DIR = path.join(home, 'sessions');
  fs.writeFileSync(path.join(home, 'bin/claude'), `#!/usr/bin/env node
const fs=require('node:fs'),path=require('node:path'),a=process.argv.slice(2),h=process.env.HOME;
fs.appendFileSync(path.join(h,'calls'),JSON.stringify(a)+'\\n');
const market=path.join(h,'registered-market');
const installed=path.join(h,'claude-installed');
if(a[0]==='--version') {console.log('fixture');process.exit(0);}
if(a[1]==='marketplace'&&a[2]==='add') fs.writeFileSync(market,a[3]);
if(a[1]==='marketplace'&&a[2]==='remove') fs.rmSync(market,{force:true});
if(['update','install'].includes(a[1])&&fs.existsSync(path.join(h,'fail-refresh')))process.exit(9);
if(['update','install'].includes(a[1]))fs.writeFileSync(installed,'user');
if(a[1]==='uninstall')fs.rmSync(installed,{force:true});
if(a[1]==='list')console.log(JSON.stringify(fs.existsSync(installed)?[{id:'communicate@communicate',scope:'user',enabled:true,version:JSON.parse(fs.readFileSync(path.join(JSON.parse(fs.readFileSync(path.join(h,'.claude/settings.json'))).extraKnownMarketplaces.communicate.source.path,'communicate/.claude-plugin/plugin.json'))).version}]:[]));
`, { mode: 0o755 });
  fs.writeFileSync(path.join(home, 'bin/codex'), '#!/bin/sh\nprintf \'[]\\n\'\n', { mode: 0o755 });
  return { home, env, settings: path.join(home, '.claude/settings.json') };
}
function run(f, args, success = true) {
  const r = spawnSync(process.execPath, [cli, ...args], { env: f.env, encoding: 'utf8', timeout: 60000 });
  assert.equal(r.status === 0, success, args.join(' ') + '\n' + r.stdout + r.stderr);
  return r;
}
try {
  const outside = path.join(temp, 'outside'); fs.mkdirSync(outside);
  const dataLink = fixture('data-symlink'); fs.symlinkSync(outside, dataLink.env.COMMUNICATE_DATA);
  run(dataLink, ['setup', '--no-clients'], false);
  assert.deepEqual(fs.readdirSync(outside), [], 'symlinked data root was followed');
  const original = '{"sentinel":"unchanged"}\n';
  fs.writeFileSync(path.join(outside, 'settings.json'), original);
  const fileLink = fixture('settings-symlink'); fs.mkdirSync(path.dirname(fileLink.settings));
  fs.symlinkSync(path.join(outside, 'settings.json'), fileLink.settings);
  run(fileLink, ['setup', '--claude'], false);
  assert.equal(fs.readFileSync(path.join(outside, 'settings.json'),'utf8'), original);

  const codexLink = fixture('codex-config-symlink'); fs.mkdirSync(codexLink.env.CODEX_HOME);
  fs.writeFileSync(path.join(outside, 'config.toml'), 'sentinel = true\n');
  fs.symlinkSync(path.join(outside,'config.toml'),path.join(codexLink.env.CODEX_HOME,'config.toml'));
  run(codexLink, ['setup','--codex'], false);
  assert.equal(fs.readFileSync(path.join(outside,'config.toml'),'utf8'),'sentinel = true\n');

  const core = fixture('core-without-clients');
  fs.symlinkSync(outside,path.dirname(core.settings));
  fs.symlinkSync(outside,core.env.CODEX_HOME);
  for (const provider of ['claude','codex']) fs.writeFileSync(path.join(core.home,'bin',provider),
    '#!/bin/sh\nprintf forbidden >> "$HOME/forbidden-provider"\nexit 93\n',{mode:0o755});
  run(core,['setup','--no-clients']);
  run(core,['uninstall','--no-clients','--no-service','--purge']);
  assert(!fs.existsSync(path.join(core.home,'forbidden-provider')),'CLI-only lifecycle invoked a provider');
  assert.equal(fs.readFileSync(path.join(outside,'settings.json'),'utf8'),original);
  assert.deepEqual(fs.readdirSync(path.dirname(fileLink.settings)), ['settings.json']);
  const dirLink = fixture('config-symlink'); fs.symlinkSync(outside, path.dirname(dirLink.settings));
  run(dirLink, ['setup', '--claude'], false);
  assert.equal(fs.readFileSync(path.join(outside, 'settings.json'),'utf8'), original);

  const normal = fixture('regular-settings'); fs.mkdirSync(path.dirname(normal.settings));
  fs.writeFileSync(normal.settings, original, { mode: 0o644 });
  run(normal, ['setup', '--claude']);
  assert.equal(fs.statSync(normal.settings).mode & 0o777, 0o600);
  const backup = fs.readdirSync(path.dirname(normal.settings)).find(n => n.includes('communicate-backup'));
  const backupPath = path.join(path.dirname(normal.settings), backup);
  assert(!fs.lstatSync(backupPath).isSymbolicLink());
  assert.equal(fs.statSync(backupPath).mode & 0o777, 0o600);
  assert.equal(fs.readFileSync(backupPath,'utf8'), original);
  const before = fs.readFileSync(normal.settings, 'utf8');
  const ledger = fs.readFileSync(path.join(normal.env.COMMUNICATE_DATA,'install.json'),'utf8');
  const calls = fs.readFileSync(path.join(normal.home,'calls'),'utf8');
  run(normal, ['uninstall','--claude','--purge'], false);
  assert.equal(fs.readFileSync(normal.settings,'utf8'), before);
  assert.equal(fs.readFileSync(path.join(normal.env.COMMUNICATE_DATA,'install.json'),'utf8'), ledger);
  assert.equal(fs.readFileSync(path.join(normal.home,'calls'),'utf8'), calls);
  run(normal, ['uninstall','--claude']);
  assert(!fs.existsSync(path.join(normal.home,'registered-market')), 'Claude CLI marketplace survived uninstall');

  const failed = fixture('failed-activation'); fs.mkdirSync(path.dirname(failed.settings));
  fs.writeFileSync(failed.settings, original); fs.writeFileSync(path.join(failed.home,'fail-refresh'),'fixture');
  run(failed, ['setup','--claude'], false);
  assert(!fs.existsSync(path.join(failed.home,'registered-market')), 'failed activation left a CLI marketplace');
  const commands = fs.readFileSync(path.join(failed.home,'calls'),'utf8').trim().split('\n').map(JSON.parse);
  assert(commands.some(a => a.join(' ') === 'plugin marketplace remove communicate'));
  assert.equal(JSON.parse(fs.readFileSync(failed.settings)).sentinel, 'unchanged');
  const failedLedger = JSON.parse(fs.readFileSync(path.join(failed.env.COMMUNICATE_DATA,'install.json')));
  assert.equal(failedLedger.releases.length,1,'failed activation left an untracked staged release');
  assert.equal(failedLedger.integrations.length,1,'failed activation left an untracked projection');
  assert(!failedLedger.current,'failed activation claimed a current release');
  run(failed,['uninstall','--purge']);
  assert(!fs.existsSync(failedLedger.releases[0]),'failed staged release was not purgeable');

  const moved = fixture('moved-registration'); run(moved, ['setup','--claude']);
  const changed = JSON.parse(fs.readFileSync(moved.settings));
  changed.extraKnownMarketplaces.communicate.source.path = path.join(outside,'different-application');
  fs.writeFileSync(moved.settings,JSON.stringify(changed));
  run(moved,['uninstall','--purge']);
  assert.equal(JSON.parse(fs.readFileSync(moved.settings)).extraKnownMarketplaces.communicate.source.path, path.join(outside,'different-application'));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(moved.env.COMMUNICATE_DATA,'install.json'))).clients, {});

  const installedRepoSetup = spawnSync(path.join(pkg,'vendor/bin/communicate'), ['setup-repo','--dry-run'], { env: normal.env, encoding: 'utf8', timeout: 15000 });
  assert.notEqual(installedRepoSetup.status,0);
  assert.match(installedRepoSetup.stderr,/installed COM8 release: use com8 setup/);
  assert(!fs.existsSync(path.join(normal.env.COMMUNICATE_DATA,'repo-path')));
  const damaged = fixture('missing-dependencies'); run(damaged, ['setup','--no-clients']);
  const active = fs.realpathSync(path.join(damaged.env.COMMUNICATE_DATA,'current'));
  fs.rmSync(path.join(active,'node_modules'),{recursive:true});
  const refusal = run(damaged,['setup','--no-clients'],false);
  assert.match(refusal.stderr,/Installed MCP dependencies are missing/);
  assert.equal(fs.realpathSync(path.join(damaged.env.COMMUNICATE_DATA,'current')),active);
  const busy = fixture('running-unmanaged-daemon'); run(busy, ['setup','--no-clients']);
  const busyRoot = fs.realpathSync(path.join(busy.env.COMMUNICATE_DATA,'current'));
  const busyCli = path.join(busyRoot,'src/com8.mjs');
  const start = spawnSync(process.execPath,[busyCli,'start'],{env:busy.env,encoding:'utf8',timeout:15000});
  daemons.push([busyCli,busy.env]);
  const daemonLog = path.join(busy.env.COMM_STATE, 'com8/daemon.log');
  assert.equal(start.status,0,[start.error?.message, start.stdout, start.stderr,
    fs.existsSync(daemonLog) ? fs.readFileSync(daemonLog, 'utf8') : 'daemon log not created'].filter(Boolean).join('\n'));
  const purge = run(busy,['uninstall','--purge'],false);
  assert.match(purge.stderr,/running daemon still uses a retained release/);
  assert(fs.existsSync(path.join(busyRoot,'vendor/lib/com8.py')),'purge deleted active daemon code');
  const stop = spawnSync(process.execPath,[busyCli,'stop'],{env:busy.env,encoding:'utf8',timeout:15000});
  assert.equal(stop.status,0,stop.stdout+stop.stderr);
  run(busy,['uninstall','--purge']);
  console.log('PASS: installer path guards, private real backups, client registry recovery, purge preflight/ownership and installed setup-repo refusal');
} finally {
  for (const [entry,env] of daemons) if(fs.existsSync(entry)) spawnSync(process.execPath,[entry,'stop'],{env,stdio:'ignore',timeout:15000});
  fs.rmSync(temp,{recursive:true,force:true});
}
