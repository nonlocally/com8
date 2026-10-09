#!/usr/bin/env bash
# com8 adopt: one command onboards a device — probe facts, PLAN the exact
# provisioning this fleet needed by hand (reverse keys, dial alias from
# SSH_CONNECTION, per-user runtime dir, CLI shim, kernel hash-refresh),
# execute, pair, spawn. The planner is PURE (facts in, actions out) — these
# tests cover shared hosts, missing DNS, stale kernels, and file ownership.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
PY(){ python3 -c "
import sys; sys.path.insert(0, '$HERE/lib')
import com8_adopt as ha
$1"; }

echo "== parse_facts: KEY=VALUE lines survive ssh banner noise, defaults on absence"
r="$(PY "
out='''Welcome to the cluster! (unauthorized use prohibited)
OS=Darwin
HOMEDIR=/Users/example
SHELL_=/opt/homebrew/bin/bash
XDG=
SSHIP=203.0.113.8
CCOWNER=501
UID_=502
OWNKEY=1
REV=0
SHIM=0
TMUX_BIN=/opt/homebrew/bin/tmux
CLAUDE_BIN=
KHASH=abc123
garbage line without equals
'''
f = ha.parse_facts(out)
print(f['os'], f['login_shell'], f['ssh_ip'], f['cc_collision'], f['own_key'], f['reverse_ok'], f['claude_bin'] or '-')")"
if [ "$r" = "Darwin /opt/homebrew/bin/bash 203.0.113.8 True True False -" ]; then
  ok "facts parsed; banner and junk ignored; collision from owner!=uid"
else bad "parse_facts (got: $r)"; fi

echo "== plan: healthy adopted device -> NO actions, NO checklist (idempotent)"
r="$(PY "
f = dict(os='Linux', login_shell='/bin/bash', home='/home/a', xdg='/run/user/1008',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         reverse_ok=True, shim=True, tmux_bin='/usr/bin/tmux',
         claude_bin='/home/a/.local/bin/claude', kernel_hash='SAME', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='SAME', my_addr='me@hub'))
print(len(acts), len(checklist))")"
if [ "$r" = "0 0" ]; then ok "adopting twice is a no-op"
else bad "idempotence (got: $r)"; fi

echo "== plan: a shared mac (bash, no MagicDNS, stale kernel)"
r="$(PY "
f = dict(os='Darwin', login_shell='/opt/homebrew/bin/bash', home='/Users/a', xdg='',
         ssh_ip='203.0.113.8', cc_collision=True, own_key=True, reverse_ok=False,
         shim=False, tmux_bin='/opt/homebrew/bin/tmux', claude_bin='/Users/a/.local/bin/claude',
         kernel_hash='OLD', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='NEW', my_addr='me@hub',
                                  reverse_candidates=['203.0.113.8','100.1.1.1']))
steps = [a['step'] for a in acts]
alias = [a for a in acts if a['step']=='reverse_alias'][0]
rt = [a for a in acts if a['step']=='runtime_dir'][0]
print('authorize_key_here' in steps, 'gen_fabric_key' in steps,
      alias['candidates'][0], sorted(rt['profiles']),
      'kernel_refresh' in steps, 'restart_daemon' in steps, 'shim' in steps)")"
if [ "$r" = "True True 203.0.113.8 ['~/.bash_profile', '~/.bashrc'] True True True" ]; then
  ok "candidates offered; bash profiles; a fabric key is minted even though a personal key exists; kernel refreshed"
else bad "shared mac plan (got: $r)"; fi

echo "== plan: the mw83 scenario (linux, xdg fine, bare box: no tmux/claude/shim)"
r="$(PY "
f = dict(os='Linux', login_shell='/bin/bash', home='/home/a', xdg='/run/user/1008',
         ssh_ip='203.0.113.8', cc_collision=False, own_key=True, reverse_ok=False,
         shim=False, tmux_bin='', claude_bin='', kernel_hash='OLD', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='NEW', my_addr='me@hub',
                                  reverse_candidates=['203.0.113.8']))
steps = [a['step'] for a in acts]
print('runtime_dir' in steps, 'reverse_alias' in steps,
      'install_tmux_static' in steps, 'install_claude' in steps, 'shim' in steps)")"
if [ "$r" = "False True True True True" ]; then
  ok "linux keeps systemd runtime dir; static tmux + native claude planned"
else bad "mw83 plan (got: $r)"; fi

echo "== plan: fresh NON-shared mac -> keygen, but the default cc-socks is kept"
r="$(PY "
f = dict(os='Darwin', login_shell='/bin/zsh', home='/Users/a', xdg='',
         ssh_ip='10.0.0.9', cc_collision=False, own_key=False, reverse_ok=False,
         shim=False, tmux_bin='/opt/homebrew/bin/tmux', claude_bin='/Users/a/.local/bin/claude',
         kernel_hash='', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='NEW', my_addr='me@hub'))
steps = [a['step'] for a in acts]
print(steps.index('gen_fabric_key') < steps.index('authorize_key_here'),
      'runtime_dir' in steps, 'kernel_refresh' in steps)")"
if [ "$r" = "True False False" ]; then
  ok "keygen precedes authorize; no steer without a collision (default cc-socks matches the launchd daemon); absent kernel left to pair"
else bad "fresh mac plan (got: $r)"; fi

echo "== plan: SHARED mac (collision) DOES steer, into zshenv"
r="$(PY "
f = dict(os='Darwin', login_shell='/bin/zsh', home='/Users/a', xdg='',
         ssh_ip='10.0.0.9', cc_collision=True, own_key=True, reverse_ok=True,
         shim=True, tmux_bin='/x/tmux', claude_bin='/x/claude',
         kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='me@hub'))
rt = [a for a in acts if a['step']=='runtime_dir']
print(bool(rt), rt[0]['profiles'] if rt else '-', rt[0]['reason'] if rt else '-')")"
if [ "$r" = "True ['~/.zshenv'] collision" ]; then
  ok "a real collision steers both daemon and claude to ~/.local/run"
else bad "shared mac plan (got: $r)"; fi

echo "== plan: reverse already works -> no alias, no key work beyond dedupe"
r="$(PY "
f = dict(os='Darwin', login_shell='/bin/zsh', home='/Users/a', xdg='',
         ssh_ip='10.0.0.9', cc_collision=False, own_key=True, reverse_ok=True,
         shim=True, tmux_bin='/x/tmux', claude_bin='/x/claude',
         kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='me@hub'))
steps = [a['step'] for a in acts]
print('reverse_alias' in steps, 'authorize_key_here' in steps, 'runtime_dir' in steps)")"
if [ "$r" = "False False False" ]; then
  ok "working reverse leg is left alone; non-shared mac keeps the default cc-socks"
else bad "reverse-ok plan (got: $r)"; fi

echo "== plan: NO reverse candidate at all -> checklist, never a bad alias"
r="$(PY "
f = dict(os='Linux', login_shell='/bin/bash', home='/home/a', xdg='/run/user/1',
         ssh_ip='', cc_collision=False, own_key=True, reverse_ok=False,
         shim=True, tmux_bin='/x/tmux', claude_bin='/x/claude',
         kernel_hash='SAME', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='SAME', my_addr='me@hub',
                                  reverse_candidates=[]))
print(any(a['step']=='reverse_alias' for a in acts),
      any('reverse' in c for c in checklist))")"
if [ "$r" = "False True" ]; then ok "no candidate degrades to a checklist item, no broken alias"
else bad "no-candidate plan (got: $r)"; fi

echo "== B: hub_reverse_candidates — source IP first, virtual ranges last, deduped"
r="$(PY "
print(ha.hub_reverse_candidates('100.64.0.47',
      ['203.0.113.8','100.64.0.47','192.168.64.1']))")"
if [ "$r" = "['100.64.0.47', '203.0.113.8', '192.168.64.1']" ]; then
  ok "connection IP leads; 192.168 sinks; the duplicate collapses"
else bad "hub candidates (got: $r)"; fi

echo "== A: probe reports empty KHASH when no kernel is present (fresh device)"
r="$(PY "
sc = ha.probe_script('me@hub')
guard = 'current/com8.py ]; then' in sc
empty = 'else echo' in sc
print(guard, empty)")"
if [ "$r" = "True True" ]; then ok "a kernel-less device hashes to empty, not md5-of-nothing"
else bad "probe kernel guard (got: $r)"; fi

echo "== profile selection covers unknown shells"
r="$(PY "print(ha._profile_files('/usr/bin/fish'), ha._profile_files(''))")"
if [ "$r" = "['~/.profile'] ['~/.profile']" ]; then ok "unknown shell -> ~/.profile fallback"
else bad "profile fallback (got: $r)"; fi

echo "== spawn command: settings via FILE; XDG exported ONLY when the device was steered"
r="$(PY "
f = dict(os='Darwin', login_shell='/bin/zsh', home='/Users/a', xdg='',
         tmux_bin='/opt/homebrew/bin/tmux', claude_bin='/Users/a/.local/bin/claude')
steered = ha.spawn_cmds('lathe', f, steered=True)
plain = ha.spawn_cmds('lathe', f, steered=False)
sj, pj = chr(10).join(steered), chr(10).join(plain)
print('--settings ~/.com8-settings.json' in sj,
      '{' not in sj.replace('crossSessionInbound','X'),
      '/opt/homebrew/bin/tmux' in sj,
      'XDG_RUNTIME_DIR' in sj,
      'XDG_RUNTIME_DIR' not in pj,
      'com8-lathe' in sj)" 2>&1 | tail -1)"
if [ "$r" = "True False True True True True" ]; then
  ok "spawn: settings file, no raw JSON braces, tmux path, XDG iff steered, session name"
else bad "spawn cmds (got: $r)"; fi

echo "== settings file writer: emits exact JSON via printf-safe encoding"
export T_HOME="$(mktemp -d /tmp/com8-adopt.XXXXXX)"
r="$(T_HOME="$T_HOME" PY "
line = ha.settings_file_cmd()
import subprocess, os
out = subprocess.run(['sh','-c', line + '; cat \"\$HOME/.com8-settings.json\"'],
                     capture_output=True, text=True,
                     env={'HOME': os.environ['T_HOME'], 'PATH': '/usr/bin:/bin'})
import json; d = json.loads(out.stdout)
print(d.get('crossSessionInbound'))" 2>&1 | tail -1)"
rm -rf "$T_HOME"
if [ "$r" = "accept" ]; then ok "settings JSON survives the shell round-trip byte-exact"
else bad "settings writer (got: $r)"; fi

echo "== kernel files list includes com8_adopt itself and matches com8.py's"
r="$(PY "
import re
src = open('$HERE/lib/com8.py').read()
from com8_payload import KERNEL_FILES
import com8
names = sorted(com8.KERNEL_FILES)
print('com8_adopt.py' in names, sorted(ha.KERNEL_FILES) == names)")"
if [ "$r" = "True True" ]; then ok "one kernel list, adopt ships with it"
else bad "kernel list (got: $r)"; fi

echo "== CLI wiring: com8 adopt is dispatched"
r="$(grep -c 'op == "adopt"' "$HERE/lib/com8.py")"
if [ "$r" -ge 1 ]; then ok "adopt op wired into the CLI"
else bad "cli wiring (got: $r)"; fi

echo "== C2: arg parsing is strict — extras error, bare --spawn errors, order-free"
r="$(PY "
print(ha.parse_args(['u@h']),
      ha.parse_args(['--spawn','lathe','u@h'])[:2],
      ha.parse_args(['u@h','oops'])[2] is not None,
      ha.parse_args(['u@h','--spawn'])[2] is not None,
      ha.parse_args([])[2] is not None)")"
if [ "$r" = "('u@h', None, None) ('u@h', 'lathe') True True True" ]; then
  ok "one addr only; trailing junk and bare --spawn are errors, not silent"
else bad "arg parsing (got: $r)"; fi

echo "== I1/M1: hostile pubkey fetch never corrupts authorized_keys"
r="$(PY "
import tempfile, os
d = tempfile.mkdtemp(); os.environ['HOME'] = d; os.makedirs(d + '/.ssh')
ak = d + '/.ssh/authorized_keys'
bad1 = ha._authorize_here('ssh-', 't')                      # truncated
bad2 = ha._authorize_here('command=\"evil\" ssh-ed25519 AAAA x', 't')
bad3 = ha._authorize_here('ssh-ed25519 AAAAgood x\nssh-ed25519 AAAAevil y', 't')
content = open(ak).read() if os.path.exists(ak) else ''
ok1 = ha._authorize_here('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample0 dev', 't')
again = ha._authorize_here('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample0 dev', 't')
lines = [l for l in open(ak).read().splitlines() if l.strip()]
print(bad1, bad2, bad3, 'evil' in content, ok1, again, len(lines))")"
if [ "$r" = "False False False False True True 1" ]; then
  ok "truncated/option-prefixed/multiline keys refused; one clean line, deduped"
else bad "authorize hostile (got: $r)"; fi

echo "== I2: execute() reports FAILED honestly when the far ssh fails"
r="$(PY "
said = []
def deadssh(addr, cmd, timeout=60): return (1, 'boom')
acts = [{'step':'runtime_dir','profiles':['~/.zshenv']},{'step':'shim'},
        {'step':'restart_daemon'}]
f = dict(os='Darwin', home='/Users/a')
ha.execute('u@h', acts, f, dict(my_addr='a@m', here_dir='/tmp'),
           ssh=deadssh, say=said.append)
print(sum(('FAILED' in l or 'UNCONFIRMED' in l) for l in said), len(said))")"
if [ "$r" = "3 3" ]; then ok "every failed step says FAILED — no false success lines"
else bad "execute honesty (got: $r)"; fi

echo "== C1: restart_daemon carries the runtime dir INLINE on darwin (no profile trust)"
r="$(PY "
seen = []
def okssh(addr, cmd, timeout=60): seen.append(cmd); return (0, '42')
ha.execute('u@h', [{'step':'restart_daemon'}], dict(os='Darwin'),
           dict(my_addr='a@m', here_dir='/tmp'), ssh=okssh, say=lambda s: None)
darwin = 'XDG_RUNTIME_DIR' in seen[0]
seen2 = []
def okssh2(addr, cmd, timeout=60): seen2.append(cmd); return (0, '42')
ha.execute('u@h', [{'step':'restart_daemon'}], dict(os='Linux'),
           dict(my_addr='a@m', here_dir='/tmp'), ssh=okssh2, say=lambda s: None)
print(darwin, 'XDG_RUNTIME_DIR' not in seen2[0])")"
if [ "$r" = "True True" ]; then
  ok "darwin daemon restarts WITH the env claude uses; linux trusts systemd"
else bad "restart env (got: $r)"; fi

echo "== C1: plan() restarts the daemon whenever the runtime dir was provisioned"
r="$(PY "
f = dict(os='Darwin', login_shell='/bin/zsh', home='/Users/a', xdg='',
         ssh_ip='10.0.0.9', cc_collision=True, own_key=True, reverse_ok=True,
         shim=True, tmux_bin='/x/tmux', claude_bin='/x/claude',
         kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@m'))
steps = [a['step'] for a in acts]
print('restart_daemon' in steps, steps[-1] if steps else '-')")"
if [ "$r" = "True restart_daemon" ]; then
  ok "runtime-dir provisioning implies a daemon restart, ordered last (post-pair)"
else bad "plan restart coupling (got: $r)"; fi

echo "== I4: kernel hashes are per-file name-tagged — a missing file can't mask"
r="$(PY "
import tempfile, os
a = tempfile.mkdtemp(); b = tempfile.mkdtemp()
for d in (a, b):
    for fn in ha.KERNEL_FILES:
        open(os.path.join(d, fn), 'w').write('same')
os.remove(os.path.join(b, ha.KERNEL_FILES[0]))
print(ha._local_kernel_hash(a) != ha._local_kernel_hash(b),
      ha._local_kernel_hash(a) == ha._local_kernel_hash(a))")"
if [ "$r" = "True True" ]; then ok "presence is part of the hash; equal trees still equal"
else bad "kernel hash presence (got: $r)"; fi

echo "== I5/M2: profile export preserves a systemd value; junk ssh_ip is rejected"
r="$(PY "
line = ha.runtime_profile_lines()
f = ha.parse_facts('SSHIP=1.2.3.4\n')
g = ha.parse_facts('SSHIP=1.2.3.4\'\ninjected\n')
print(':-' in line, f['ssh_ip'], g['ssh_ip'] == '')")"
if [ "$r" = "True 1.2.3.4 True" ]; then
  ok "\${XDG_RUNTIME_DIR:-...} guard; ssh_ip must be address-shaped"
else bad "profile guard / ip validation (got: $r)"; fi

echo "== C2 is WIRED: _cli_adopt actually calls ha.parse_args (not a private loop)"
r="$(grep -c 'ha.parse_args(args)' "$HERE/lib/com8.py")"
if [ "$r" -ge 1 ]; then ok "the shipped CLI path delegates to the strict parser"
else bad "C2 wiring (parse_args not called in com8.py)"; fi

echo "== I4: hub-completeness guard + name-tagged hashing"
r="$(PY "
import tempfile, os
good = tempfile.mkdtemp()
for fn in ha.KERNEL_FILES: open(os.path.join(good, fn),'w').write('x')
bad = tempfile.mkdtemp()
for fn in ha.KERNEL_FILES[1:]: open(os.path.join(bad, fn),'w').write('x')
print(ha.hub_missing_files(good) == [],
      ha.hub_missing_files(bad) == [ha.KERNEL_FILES[0]],
      ha._local_kernel_hash(good) != ha._local_kernel_hash(bad))")"
if [ "$r" = "True True True" ]; then
  ok "healthy hub clean; incomplete hub names the gap; presence changes the hash"
else bad "I4 hub guard (got: $r)"; fi

echo "== fabric key: a device with no id_com8 gets one generated (personal key untouched)"
r="$(PY "
f = dict(os='Darwin', login_shell='/bin/zsh', home='/Users/a', xdg='',
         ssh_ip='203.0.113.8', cc_collision=False, own_key=True, fabric_key=False,
         reverse_ok=False, shim=True, tmux_bin='/x/tmux', claude_bin='/x/claude',
         kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@mini',
                          reverse_candidates=['203.0.113.8']))
steps = [a['step'] for a in acts]
print('gen_fabric_key' in steps, 'gen_own_key' not in steps,
      steps.index('gen_fabric_key') < steps.index('authorize_key_here'))")"
if [ "$r" = "True True True" ]; then
  ok "a dedicated passphrase-free fabric key is generated before authorizing"
else bad "fabric key plan (got: $r)"; fi

echo "== fabric key: present -> not regenerated (idempotent)"
r="$(PY "
f = dict(os='Linux', login_shell='/bin/bash', home='/h', xdg='/run/user/1',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=False, fabric_key=True,
         reverse_ok=True, shim=True, tmux_bin='/x/tmux', claude_bin='/x/claude',
         kernel_hash='SAME', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@mini',
                                  reverse_candidates=['1.2.3.4']))
print(len(acts), len(checklist))")"
if [ "$r" = "0 0" ]; then ok "existing fabric key + working reverse = still a no-op"
else bad "fabric key idempotence (got: $r)"; fi

echo "== keygen command: ed25519, NO passphrase, dedicated path"
r="$(PY "
c = ha.fabric_keygen_cmd()
print('id_com8' in c, \"-N ''\" in c, 'ed25519' in c, 'id_ed25519' not in c)")"
if [ "$r" = "True True True True" ]; then
  ok "keygen writes ~/.ssh/id_com8 with an empty passphrase, never the personal key"
else bad "keygen cmd (got: $r)"; fi

echo "== the dial alias pins the fabric key (IdentitiesOnly), so a locked personal key can't shadow it"
r="$(PY "
seen = []
def okssh(addr, cmd, timeout=60):
    seen.append(cmd)
    return (0, '')
ha.execute('u@h', [{'step':'reverse_alias','candidates':['203.0.113.8']}],
           dict(os='Darwin'), dict(my_addr='me@hub', here_dir='/tmp'),
           ssh=okssh, say=lambda s: None)
probe = seen[0]; write = seen[-1]
print('id_com8' in probe, 'IdentitiesOnly' in probe,
      'IdentityFile' in write and 'id_com8' in write,
      'IdentitiesOnly yes' in write)")"
if [ "$r" = "True True True True" ]; then
  ok "reverse probe AND the written Host block both pin ~/.ssh/id_com8"
else bad "alias identity pinning (got: $r)"; fi

echo "== probe asks about the fabric key and tests reverse WITH it"
r="$(PY "
sc = ha.probe_script('me@hub')
print('COM8KEY=' in sc, 'id_com8' in sc, 'IdentitiesOnly=yes' in sc)")"
if [ "$r" = "True True True" ]; then
  ok "probe reports COM8KEY and its REV test uses the fabric key only"
else bad "probe fabric key (got: $r)"; fi

echo "== parse_facts surfaces fabric_key"
r="$(PY "
print(ha.parse_facts('COM8KEY=1\n')['fabric_key'],
      ha.parse_facts('COM8KEY=0\n')['fabric_key'],
      ha.parse_facts('OS=Darwin\n')['fabric_key'])")"
if [ "$r" = "True False False" ]; then ok "fabric_key parsed, absent means false"
else bad "parse fabric_key (got: $r)"; fi

echo "== a hung/unreachable device never tracebacks — _run_ssh reports honestly"
r="$(PY "
import subprocess
real = subprocess.run
def boom(*a, **k):
    raise subprocess.TimeoutExpired(cmd='ssh', timeout=1)
subprocess.run = boom
try:
    rc, out = ha._run_ssh('u@h', 'true', timeout=1)
finally:
    subprocess.run = real
print(rc != 0, 'timed out' in out.lower())")"
if [ "$r" = "True True" ]; then ok "an ssh timeout returns a nonzero rc + message, never an exception"
else bad "ssh timeout handling (got: $r)"; fi

echo "== an OSError (no ssh binary / DNS blowup) is also caught"
r="$(PY "
import subprocess
real = subprocess.run
def boom(*a, **k):
    raise OSError('no such binary')
subprocess.run = boom
try:
    rc, out = ha._run_ssh('u@h', 'true', timeout=1)
finally:
    subprocess.run = real
print(rc != 0, bool(out.strip()))")"
if [ "$r" = "True True" ]; then ok "an OSError degrades to a reported failure"
else bad "ssh oserror handling (got: $r)"; fi

echo "== the HUB's own key is pushed to the device (so a launchd daemon can dial without an agent)"
r="$(PY "
f = dict(os='Darwin', login_shell='/bin/zsh', home='/Users/a', xdg='',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         hub_key_there=False, reverse_ok=True, shim=True, tmux_bin='/x/tmux',
         claude_bin='/x/claude', kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@mini',
                          reverse_candidates=['1.2.3.4']))
steps = [a['step'] for a in acts]
print('authorize_hub_key_there' in steps)")"
if [ "$r" = "True" ]; then ok "a device missing the hub key gets it installed"
else bad "hub key plan (got: $r)"; fi

echo "== hub key already installed -> no-op"
r="$(PY "
f = dict(os='Linux', login_shell='/bin/bash', home='/h', xdg='/run/user/1',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         hub_key_there=True, reverse_ok=True, shim=True, tmux_bin='/x/tmux',
         claude_bin='/x/claude', kernel_hash='SAME', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@mini',
                                  reverse_candidates=['1.2.3.4']))
print(len(acts), len(checklist))")"
if [ "$r" = "0 0" ]; then ok "a fully adopted device stays a no-op"
else bad "hub key idempotence (got: $r)"; fi

echo "== probe asks whether the hub key is already authorized there"
r="$(PY "
sc = ha.probe_script('a@mini', hub_key_material='ABCDEFmaterial123')
print('HUBKEY=' in sc, 'ABCDEFmaterial123' in sc, 'authorized_keys' in sc)")"
if [ "$r" = "True True True" ]; then
  ok "probe greps the device's authorized_keys for the hub's key material"
else bad "probe hub key (got: $r)"; fi

echo "== the hub key is installed with a real ssh-key shape, never blind text"
r="$(PY "
seen = []
def okssh(addr, cmd, timeout=60):
    seen.append(cmd); return (0, '')
ha.execute('u@h', [{'step':'authorize_hub_key_there'}], dict(os='Darwin'),
           dict(my_addr='a@mini', here_dir='/tmp',
                hub_pubkey='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIhubkeymaterial hub'),
           ssh=okssh, say=lambda s: None)
c = seen[-1]
print('authorized_keys' in c, 'ssh-ed25519' in c, 'grep -q' in c)")"
if [ "$r" = "True True True" ]; then
  ok "installs into authorized_keys, deduped by grep, key-shaped"
else bad "hub key install (got: $r)"; fi

echo "== the reverse test only counts when the fabric key EXISTS (no fallback-identity false pass)"
r="$(PY "
sc = ha.probe_script('a@mini')
i_key = sc.find('id_com8 ] && echo \"COM8KEY')
i_rev = sc.find('REV=1')
guarded = '[ -f ~/.ssh/id_com8 ] &&' in sc.split('REV=1')[0].split('COM8KEY')[-1]
print(guarded)")"
if [ "$r" = "True" ]; then
  ok "no fabric key -> REV=0, so adopt authorizes it instead of trusting a fallback"
else bad "reverse test guard (got: $r)"; fi

echo "== a STEERED device carries the runtime dir into its daemon restart on ANY os"
r="$(PY "
def cap(store):
    def f(addr, cmd, timeout=60):
        store.append(cmd); return (0, '42')
    return f
# steered linux (no systemd: termux/android) -> restart must carry the env
f = dict(os='Linux', login_shell='/bin/bash', home='/h', xdg='',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         hub_key_there=True, reverse_ok=True, shim=True, tmux_bin='/x/tmux',
         claude_bin='/x/claude', kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@m',
                          reverse_candidates=['1.2.3.4']))
rst = [a for a in acts if a['step']=='restart_daemon']
seen = []
ha.execute('u@h', rst, f, dict(my_addr='a@m', here_dir='/tmp'),
           ssh=cap(seen), say=lambda s: None)
# a real systemd box (xdg present) is never steered -> no inline env
g = dict(f, xdg='/run/user/1000')
acts2, _ = ha.plan(g, dict(kernel_hash='SAME', my_addr='a@m',
                           reverse_candidates=['1.2.3.4']))
seen2 = []
ha.execute('u@h', [{'step':'restart_daemon'}], g,
           dict(my_addr='a@m', here_dir='/tmp'), ssh=cap(seen2), say=lambda s: None)
print(bool(rst), 'XDG_RUNTIME_DIR' in seen[0],
      not any(a['step']=='runtime_dir' for a in acts2),
      'XDG_RUNTIME_DIR' not in seen2[0])")"
if [ "$r" = "True True True True" ]; then
  ok "steered -> daemon restarts WITH the runtime dir (any os); real systemd untouched"
else bad "steered restart env (got: $r)"; fi

echo "== termux is detected as its own device class"
r="$(PY "
a = ha.parse_facts('OS=Linux\nUNAME_O=Android\n')['is_termux']
b = ha.parse_facts('OS=Linux\nPREFIX=/data/data/com.termux/files/usr\n')['is_termux']
c = ha.parse_facts('OS=Linux\nUNAME_O=GNU/Linux\n')['is_termux']
print(a, b, c)")"
if [ "$r" = "True True False" ]; then ok "Android via uname -o or the termux PREFIX; a normal linux is not"
else bad "is_termux (got: $r)"; fi

echo "== termux: TMPDIR is provisioned (SELinux makes /tmp unwritable at ANY mode)"
r="$(PY "
f = dict(os='Linux', is_termux=True, login_shell='/bin/bash', home='/h', xdg='',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         hub_key_there=True, reverse_ok=True, shim=True, tmux_bin='/x/tmux',
         claude_bin='/x/claude', kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@m',
                          reverse_candidates=['1.2.3.4']))
td = [a for a in acts if a['step']=='tmpdir']
rt = [a for a in acts if a['step']=='runtime_dir']
print(bool(td), sorted(td[0]['profiles'])[:2] if td else '-', bool(rt))")"
if [ "$r" = "True ['~/.bash_profile', '~/.bashrc'] True" ]; then
  ok "TMPDIR exported into the login profiles; runtime dir steered too"
else bad "termux tmpdir (got: $r)"; fi

echo "== termux: the amd64 static tmux is NEVER planned (Android is arm64)"
r="$(PY "
f = dict(os='Linux', is_termux=True, login_shell='/bin/bash', home='/h', xdg='',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         hub_key_there=True, reverse_ok=True, shim=True, tmux_bin='',
         claude_bin='/x/claude', kernel_hash='SAME', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@m',
                                  reverse_candidates=['1.2.3.4']))
steps = [a['step'] for a in acts]
print('install_tmux_static' in steps, any('pkg install tmux' in c for c in checklist))")"
if [ "$r" = "False True" ]; then
  ok "no amd64 binary pushed to an arm64 phone; the checklist names pkg instead"
else bad "termux tmux (got: $r)"; fi

echo "== termux: claude is pinned (the native installer ships no android binary)"
r="$(PY "
f = dict(os='Linux', is_termux=True, login_shell='/bin/bash', home='/h', xdg='',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         hub_key_there=True, reverse_ok=True, shim=True, tmux_bin='/x/tmux',
         claude_bin='', kernel_hash='SAME', py3=True)
acts, _ = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@m',
                          reverse_candidates=['1.2.3.4']))
ic = [a for a in acts if a['step']=='install_claude'][0]
print(ic.get('pin'))")"
if [ "$r" = "2.1.72" ]; then ok "install_claude carries the android pin"
else bad "termux claude pin (got: $r)"; fi

echo "== termux: an unknown fabric identity is a CHECKLIST item, never a guess"
r="$(PY "
f = dict(os='Linux', is_termux=True, login_shell='/bin/bash', home='/h', xdg='',
         ssh_ip='1.2.3.4', cc_collision=False, own_key=True, fabric_key=True,
         hub_key_there=True, reverse_ok=True, shim=True, tmux_bin='/x/tmux',
         claude_bin='/x/claude', kernel_hash='SAME', py3=True, com8_self='')
acts, checklist = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@m',
                                  reverse_candidates=['1.2.3.4']))
named = dict(f, com8_self='example-phone')
_, cl2 = ha.plan(named, dict(kernel_hash='SAME', my_addr='a@m',
                             reverse_candidates=['1.2.3.4']))
print(any('COM8_SELF' in c for c in checklist), any('COM8_SELF' in c for c in cl2))")"
if [ "$r" = "True False" ]; then
  ok "no MagicDNS -> COM8_SELF asked for, not invented; already-named device is quiet"
else bad "termux com8_self (got: $r)"; fi

echo "== a normal linux is untouched by any of this"
r="$(PY "
f = dict(os='Linux', is_termux=False, login_shell='/bin/bash', home='/h',
         xdg='/run/user/1000', ssh_ip='1.2.3.4', cc_collision=False,
         own_key=True, fabric_key=True, hub_key_there=True, reverse_ok=True,
         shim=True, tmux_bin='', claude_bin='', kernel_hash='SAME', py3=True)
acts, checklist = ha.plan(f, dict(kernel_hash='SAME', my_addr='a@m',
                                  reverse_candidates=['1.2.3.4']))
steps = [a['step'] for a in acts]
ic = [a for a in acts if a['step']=='install_claude'][0]
print('install_tmux_static' in steps, 'tmpdir' in steps, ic.get('pin'), len(checklist))")"
if [ "$r" = "True False None 0" ]; then
  ok "plain linux still gets the static tmux, no tmpdir, unpinned claude"
else bad "linux unchanged (got: $r)"; fi

echo "== the pin reaches the installer as its positional arg (it takes [stable|latest|VERSION])"
r="$(PY "
seen = []
def cap(addr, cmd, timeout=60):
    seen.append(cmd); return (0, '/x/claude')
ha.execute('u@h', [{'step':'install_claude','pin':'2.1.72'}], dict(os='Linux'),
           dict(my_addr='a@m', here_dir='/tmp'), ssh=cap, say=lambda s: None)
pinned = seen[0]
seen2 = []
def cap2(addr, cmd, timeout=60):
    seen2.append(cmd); return (0, '/x/claude')
ha.execute('u@h', [{'step':'install_claude','pin':None}], dict(os='Linux'),
           dict(my_addr='a@m', here_dir='/tmp'), ssh=cap2, say=lambda s: None)
print('bash -s 2.1.72' in pinned, 'bash -s' not in seen2[0])")"
if [ "$r" = "True True" ]; then
  ok "pinned install passes the version to install.sh; unpinned stays latest"
else bad "installer pin shape (got: $r)"; fi

echo "== COM8_SELF: the check asks 'is it CONFIGURED', not 'is it in my shell'"
r="$(PY "
base = dict(os='Linux', is_termux=True, login_shell='/bin/bash', home='/h',
            xdg='', ssh_ip='1.2.3.4', cc_collision=False, own_key=True,
            fabric_key=True, hub_key_there=True, reverse_ok=True, shim=True,
            tmux_bin='/x/tmux', claude_bin='/x/claude', kernel_hash='SAME',
            py3=True)
loc = dict(kernel_hash='SAME', my_addr='a@m', reverse_candidates=['1.2.3.4'])
def has_item(f):
    return any('COM8_SELF' in c for c in ha.plan(f, loc)[1])
# the probe runs non-login/non-interactive, so env is empty even when the
# human HAS configured it — the item must clear on the profile, not the env
# the login shell's answer is the authority — that is what the agent gets
cfg   = has_item(dict(base, com8_self_value='ember', com8_self_files=['.bash_profile']))
envd  = has_item(dict(base, com8_self='ember', com8_self_files=[]))
none  = has_item(dict(base, com8_self='', com8_self_value='', com8_self_files=[]))
# an export the login shell cannot actually produce is NOT configured,
# however many files it appears in
wrong = has_item(dict(base, com8_self='', com8_self_value='', com8_self_files=['.zshenv']))
print(cfg, envd, none, wrong)")"
if [ "$r" = "False False True True" ]; then
  ok "login-shell value clears it; env clears it; nothing keeps it; a file the shell cannot yield keeps it"
else bad "com8_self check (got: $r)"; fi

echo "== the probe actually looks in the profiles (not just \$COM8_SELF)"
r="$(PY "
sc = ha.probe_script('a@mini')
print('COM8_SELF_FILES=' in sc, '.bash_profile' in sc, '.zshenv' in sc)")"
if [ "$r" = "True True True" ]; then ok "probe reports which profiles carry the export"
else bad "probe com8_self files (got: $r)"; fi

echo "== COM8_SELF is read back from the device's own LOGIN shell, and inlined into the daemon"
r="$(PY "
base = dict(os='Linux', is_termux=True, login_shell='/bin/bash', home='/h',
            xdg='', ssh_ip='1.2.3.4', cc_collision=False, own_key=True,
            fabric_key=True, hub_key_there=True, reverse_ok=True, shim=True,
            tmux_bin='/x/tmux', claude_bin='/x/claude', kernel_hash='SAME',
            py3=True, com8_self='', com8_self_files=[])
loc = dict(kernel_hash='SAME', my_addr='a@m', reverse_candidates=['1.2.3.4'])
named = dict(base, com8_self_value='example-phone')
acts, cl = ha.plan(named, loc)
rst = [a for a in acts if a['step']=='restart_daemon'][0]
seen = []
def cap(addr, cmd, timeout=60):
    seen.append(cmd); return (0, '9')
ha.execute('u@h', [rst], named, dict(my_addr='a@m', here_dir='/tmp'),
           ssh=cap, say=lambda s: None)
print(rst.get('com8_self'), 'COM8_SELF=example-phone' in seen[0],
      any('COM8_SELF' in c for c in cl))")"
if [ "$r" = "example-phone True False" ]; then
  ok "the read-back name rides into the daemon start; no checklist once configured"
else bad "com8_self read-back (got: $r)"; fi

echo "== unconfigured: the item names the CONSEQUENCE, not the state"
r="$(PY "
base = dict(os='Linux', is_termux=True, login_shell='/bin/bash', home='/h',
            xdg='', ssh_ip='1.2.3.4', cc_collision=False, own_key=True,
            fabric_key=True, hub_key_there=True, reverse_ok=True, shim=True,
            tmux_bin='/x/tmux', claude_bin='/x/claude', kernel_hash='SAME',
            py3=True, com8_self='', com8_self_value='', com8_self_files=[])
loc = dict(kernel_hash='SAME', my_addr='a@m', reverse_candidates=['1.2.3.4'])
cl = ha.plan(base, loc)[1]
item = [c for c in cl if 'COM8_SELF' in c][0]
# and the wrong-shell diagnostic
wrong = ha.plan(dict(base, com8_self_files=['.zshenv']), loc)[1]
witem = [c for c in wrong if 'COM8_SELF' in c][0]
print('identity' in item, '.zshenv' in witem and 'bash' in witem)")"
if [ "$r" = "True True" ]; then
  ok "says the daemon has no identity; and names the wrong-shell file when that is the cause"
else bad "com8_self checklist text (got: $r)"; fi

echo "== the probe asks the LOGIN shell, and always emits the key"
r="$(PY "
sc = ha.probe_script('a@m')
print('COM8_SELF_VALUE=' in sc, '-lc' in sc, sc.count('COM8_SELF_VALUE=') >= 2)")"
if [ "$r" = "True True True" ]; then
  ok "\$SHELL -lc read-back with an unconditional default"
else bad "probe read-back (got: $r)"; fi

echo "== a device with no COM8_SELF never gets an empty one inlined"
r="$(PY "
f = dict(os='Darwin', is_termux=False, login_shell='/bin/zsh', home='/U',
         xdg='', cc_collision=True, com8_self_value='')
seen = []
def cap(addr, cmd, timeout=60):
    seen.append(cmd); return (0, '9')
ha.execute('u@h', [{'step':'restart_daemon','env':True}], f,
           dict(my_addr='a@m', here_dir='/tmp'), ssh=cap, say=lambda s: None)
print('COM8_SELF=' not in seen[0], 'XDG_RUNTIME_DIR' in seen[0])")"
if [ "$r" = "True True" ]; then ok "no name, no COM8_SELF in the launch; the runtime dir still rides"
else bad "empty com8_self guard (got: $r)"; fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
