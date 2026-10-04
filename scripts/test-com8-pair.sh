#!/usr/bin/env bash
# Pair test: the measured handshake (ping envelope + link-check) and the
# one-sided device-enrollment orchestration (com8 pair). Two isolated com8s on
# one host over DIRECT socket paths, same harness as test-com8-link.sh; the
# full ssh path is opt-in (COM8_TEST_SSH=1 needs `ssh localhost` batch auth).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-pair.XXXXXX)"

A_STATE="$T/a-state"; A_SOCKS="$T/a-socks"; A_SESS="$T/a-sess"
B_STATE="$T/b-state"; B_SOCKS="$T/b-socks"; B_SESS="$T/b-sess"
mkdir -p "$A_SESS" "$B_SESS"

acomm() { COMM_STATE="$A_STATE" COM8_SOCK_DIR="$A_SOCKS" COM8_SESSIONS_DIR="$A_SESS" COM8_SELF=alpha COM8_TICK=1 "$COMM" "$@"; }
bcomm() { COMM_STATE="$B_STATE" COM8_SOCK_DIR="$B_SOCKS" COM8_SESSIONS_DIR="$B_SESS" COM8_SELF=beta  COM8_TICK=1 "$COMM" "$@"; }

pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup() {
  acomm com8 stop >/dev/null 2>&1 || true; bcomm com8 stop >/dev/null 2>&1 || true
  for st in "$T/c-state" "$T/far-installed/state" "$T/far-own/.local/state/communicate" "$T/far-foreign/.local/state/communicate"; do
    COMM_STATE="$st" python3 "$HERE/lib/com8.py" call stop >/dev/null 2>&1 || true
  done
  rm -rf "$T"
}
trap cleanup EXIT

jget() { python3 -c '
import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1:]:
    d=d[k]
print(d)' "$@" 2>/dev/null; }

echo "== setup: two com8s, symmetric direct links"
acomm com8 start >/dev/null 2>&1 || bad "A start"
bcomm com8 start >/dev/null 2>&1 || bad "B start"
acomm com8 link beta --sock "$B_STATE/com8/in/alpha.sock" >/dev/null 2>&1 || bad "A link beta"
bcomm com8 link alpha --sock "$A_STATE/com8/in/beta.sock" >/dev/null 2>&1 || bad "B link alpha"
bcomm com8 init --handle betauser >/dev/null 2>&1 || bad "B claims a handle"

echo "== link-check: the measured handshake"
r="$(acomm com8 link beta --check --json 2>/dev/null)"
if [ "$(printf '%s' "$r" | jget ok)" = "True" ] && [ "$(printf '%s' "$r" | jget far_device)" = "beta" ]; then
  ok "link-check reaches beta and names it from the wire"
else bad "link-check reaches beta (got: $r)"; fi
rtt="$(printf '%s' "$r" | jget rtt_ms)"
if [ -n "$rtt" ] && [ "$rtt" -ge 0 ] 2>/dev/null; then
  ok "round trip is MEASURED (${rtt} ms)"
else bad "round trip measured (got: $rtt)"; fi
srcv="$(grep -m1 '^COM8_VERSION' "$HERE/lib/com8.py" | cut -d'"' -f2)"
if [ "$(printf '%s' "$r" | jget far_version)" = "$srcv" ]; then
  ok "far daemon version rides the pong"
else bad "far daemon version rides the pong"; fi
if [ "$(printf '%s' "$r" | jget far_user)" = "betauser" ]; then
  ok "far user handle rides the pong"
else bad "far user handle rides the pong (got: $(printf '%s' "$r" | jget far_user))"; fi

echo "== raw ping envelopes: stateless, dedup-neutral, fail-closed"
ping_once() { python3 - "$B_STATE/com8/in/alpha.sock" "$1" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5)
s.connect(sys.argv[1])
env = {"v": 1, "kind": "ping", "ts": 0.0}
if sys.argv[2] != "nomid":
    env["msg_id"] = sys.argv[2]
s.sendall((json.dumps(env) + "\n").encode())
print(s.makefile().readline().strip())
PY
}
p1="$(ping_once fixedping1)"
p2="$(ping_once fixedping1)"
if printf '%s' "$p1" | grep -q '"pong": true' && printf '%s' "$p1" | grep -q '"device": "beta"'; then
  ok "ping acked with pong + device"
else bad "ping acked with pong + device (got: $p1)"; fi
if printf '%s' "$p2" | grep -q '"pong": true' && ! printf '%s' "$p2" | grep -q '"dup"'; then
  ok "repeated ping is not deduped (stateless — no seen-ring pollution)"
else bad "repeated ping is not deduped (got: $p2)"; fi
if grep -q "fixedping1" "$B_STATE/com8/seen/alpha" 2>/dev/null; then
  bad "ping msg_id leaked into the dedup ring"
else ok "ping msg_id never enters the dedup ring"; fi
pn="$(ping_once nomid)"
if printf '%s' "$pn" | grep -q '"bad envelope"'; then
  ok "ping without msg_id refused (bad envelope)"
else bad "ping without msg_id refused (got: $pn)"; fi

echo "== link-check when the far side is down: honest failure, healthy daemon"
bcomm com8 stop >/dev/null 2>&1; sleep 0.7
r="$(acomm com8 link beta --check --json 2>/dev/null)"
if [ "$(printf '%s' "$r" | jget ok)" = "False" ] && [ -n "$(printf '%s' "$r" | jget err)" ]; then
  ok "far-down reports ok:false with a reason"
else bad "far-down reports ok:false (got: $r)"; fi
st="$(acomm com8 status --json 2>/dev/null)"
if [ "$(printf '%s' "$st" | jget ok)" = "True" ]; then ok "A stays healthy"; else bad "A stays healthy"; fi

echo "== legacy peer: an old daemon that rejects the ping kind"
LEG="$T/legacy.sock"
python3 - "$LEG" <<'PY' &
import json, socket, sys
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sys.argv[1]); srv.listen(4); srv.settimeout(20)
try:
    while True:
        c, _ = srv.accept()
        c.makefile().readline()
        c.sendall((json.dumps({"ok": False, "err": "bad envelope"}) + "\n").encode())
        c.close()
except Exception:
    pass
PY
LEGPID=$!
sleep 0.5
acomm com8 link legacydev --sock "$LEG" >/dev/null 2>&1
r="$(acomm com8 link legacydev --check --json 2>/dev/null)"
if [ "$(printf '%s' "$r" | jget ok)" = "True" ] && [ "$(printf '%s' "$r" | jget legacy_peer)" = "True" ] \
   && [ "$(printf '%s' "$r" | jget transport)" = "up" ]; then
  ok "legacy peer: transport measured up, honestly flagged legacy"
else bad "legacy peer flagged (got: $r)"; fi
kill "$LEGPID" 2>/dev/null

echo "== pair --dry-run: the golden plan, zero mutation"
links_before="$(cat "$A_STATE/com8/links.json" 2>/dev/null)"
out="$(acomm com8 pair fake@nowhere.invalid --dry-run 2>&1)"
rc=$?
if [ $rc -eq 0 ]; then ok "pair --dry-run exits 0"; else bad "pair --dry-run exits 0 (rc=$rc; out: $out)"; fi
for step in "1/7" "2/7" "3/7" "4/7" "5/7" "6/7" "7/7"; do
  if printf '%s' "$out" | grep -q "$step"; then ok "dry-run plan names step $step"; else bad "dry-run plan names step $step"; fi
done
if printf '%s' "$out" | grep -qi "ssh"; then ok "dry-run plan mentions the ssh probe"; else bad "dry-run plan mentions ssh"; fi
links_after="$(cat "$A_STATE/com8/links.json" 2>/dev/null)"
if [ "$links_before" = "$links_after" ]; then
  ok "dry-run mutated nothing (links.json unchanged)"
else bad "dry-run mutated links.json"; fi

echo "== pair refuses without reachability (measured, exact fix printed)"
out="$(acomm com8 pair fake@nowhere.invalid 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then ok "pair against unreachable host fails"; else bad "pair against unreachable host fails"; fi
if printf '%s' "$out" | grep -q "ssh-copy-id"; then
  ok "failure prints the exact fix"
else bad "failure prints the exact fix (got: $out)"; fi

echo "== pair against an INSTALLED far side (fake batch ssh: far commands run here, isolated far HOME)"
# A fake ssh/scp on PATH runs every far-side command on THIS host under
# $FAR_HOME with its own sockets/sessions and stubbed launchctl/systemctl/
# tailscale: no sshd, no service manager, no real HOME. It emulates batch
# command execution only; `ssh -N` (a link dial) fails fast, so a run that
# links over --addr is expected to fail at step 7 — what these cases prove is
# step 2, the far-side provisioning, and what it leaves on the far disk.
FAKEBIN="$T/fakebin"; mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/ssh" <<'FAKE'
#!/usr/bin/env bash
addr=""; cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2;;
    -N) echo "fake ssh: port forwarding is not emulated" >&2; exit 255;;
    -*) shift;;
    *) if [ -z "$addr" ]; then addr="$1"; shift; else cmd="$*"; break; fi;;
  esac
done
printf '%s\n' "$cmd" >> "${FAKE_SSH_LOG:-/dev/null}"
[ -n "$cmd" ] || exit 255
export HOME="$FAR_HOME" PATH="$FAKE_PATH" COM8_SOCK_DIR="$FAR_HOME/socks" \
  COM8_SESSIONS_DIR="$FAR_HOME/sess" COM8_SELF="${FAR_SELF:-farbox}" COM8_TICK=1
unset COMM_STATE COMMUNICATE_DATA CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CONFIG_DIR XDG_RUNTIME_DIR XDG_CONFIG_HOME
exec bash -c "$cmd"
FAKE
cat > "$FAKEBIN/scp" <<'FAKE'
#!/usr/bin/env bash
files=()
for a in "$@"; do case "$a" in -q|-o|BatchMode=yes) ;; *) files+=("$a");; esac; done
dest="${files[${#files[@]}-1]}"; unset 'files[${#files[@]}-1]'
dest="${dest#*:}"; case "$dest" in "~"*) dest="$FAR_HOME${dest#\~}";; esac
mkdir -p "$dest" && cp "${files[@]}" "$dest"/
FAKE
for stub in launchctl systemctl tailscale; do
  printf '#!/usr/bin/env bash\nprintf "%%s %%s\\n" "%s" "$*" >> "${FAKE_SSH_LOG:-/dev/null}"\nexit 0\n' "$stub" > "$FAKEBIN/$stub"
done
chmod +x "$FAKEBIN"/*
C_STATE="$T/c-state"; C_SOCKS="$T/c-socks"; C_SESS="$T/c-sess"; mkdir -p "$C_SESS"
FAR=""; FARSELF=farbox
# The near daemon and every pair run see the fake ssh first on PATH.
ccomm() { PATH="$FAKEBIN:$PATH" FAKE_PATH="$FAKEBIN:$PATH" FAKE_SSH_LOG="$T/ssh.log" FAR_HOME="$FAR" FAR_SELF="$FARSELF" \
  COMM_STATE="$C_STATE" COM8_SOCK_DIR="$C_SOCKS" COM8_SESSIONS_DIR="$C_SESS" COM8_SELF=gamma COM8_TICK=1 "$COMM" "$@"; }
far_stop() { COMM_STATE="$1" python3 "$HERE/lib/com8.py" call stop >/dev/null 2>&1 || true; }
ccomm com8 start >/dev/null 2>&1 || bad "C start"
ccomm com8 init --handle pairtester --force >/dev/null 2>&1 || bad "C claims a handle"
realp() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
srcv="$(grep -m1 '^COM8_VERSION' "$HERE/lib/com8.py" | cut -d'"' -f2)"

echo "-- an installed release on the far side (stopped): found, started via its own CLI, never staged over"
# The installed layout the release lifecycle creates: <data>/current/vendor/{bin,lib}.
FAR_A="$T/far-installed"; INST_A="$FAR_A/share/communicate/current/vendor"
mkdir -p "$INST_A"; cp -R "$HERE/bin" "$INST_A/bin"; cp -R "$HERE/lib" "$INST_A/lib"
FAR="$FAR_A"; FARSELF=fardev
out="$(COM8_PAIR_FAR_HOME="$FAR_A" ccomm com8 pair tester@far.invalid --name farbox 2>&1)"; rc=$?
if [ $rc -eq 0 ]; then ok "pair completes all 7 steps against an installed far release"; else bad "pair completes against an installed far release (rc=$rc; out: $out)"; fi
if printf '%s' "$out" | grep -q "installed release"; then ok "step 2 names the installed release"; else bad "step 2 names the installed release (out: $out)"; fi
if [ ! -e "$FAR_A/daemon" ]; then ok "no legacy kernel staged beside an installed release"; else bad "legacy kernel staged beside an installed release"; fi
farsrc="$(COMM_STATE="$FAR_A/state" python3 "$HERE/lib/com8.py" call status --json 2>/dev/null | jget self source_file)"
if [ "$farsrc" = "$(realp "$INST_A/lib/com8.py")" ]; then ok "the far daemon runs the installed release's own kernel"; else bad "far daemon runs the installed kernel (got: $farsrc)"; fi
out2="$(COM8_PAIR_FAR_HOME="$FAR_A" ccomm com8 pair tester@far.invalid --name farbox 2>&1)"; rc2=$?
if [ $rc2 -eq 0 ] && printf '%s' "$out2" | grep -q "installed release, current"; then ok "second run: installed release reported current, nothing restaged"; else bad "second run reports the installed release current (rc=$rc2; out: $out2)"; fi
far_stop "$FAR_A/state"

echo "-- a service definition pair did not write is never overwritten (refused before staging)"
FAR_B="$T/far-foreign"; mkdir -p "$FAR_B"
case "$(uname -s)" in
  Darwin) unit_b="$FAR_B/Library/LaunchAgents/com.communicate.com8.plist";;
  *)      unit_b="$FAR_B/.config/systemd/user/communicate-com8.service";;
esac
mkdir -p "$(dirname "$unit_b")"
printf 'managed by com8 setup --service: %s/.local/share/communicate/current/vendor/lib/com8.py daemon\n' "$FAR_B" > "$unit_b"
before_b="$(cat "$unit_b")"
FAR="$FAR_B"; FARSELF=farbox
out="$(ccomm com8 pair tester@far.invalid 2>&1)"; rc=$?
if [ $rc -ne 0 ]; then ok "pair refuses to take over a foreign service definition"; else bad "pair refuses a foreign service definition (out: $out)"; fi
if printf '%s' "$out" | grep -q "pair did not write"; then ok "refusal names the service definition it will not touch"; else bad "refusal names the definition (out: $out)"; fi
if printf '%s' "$out" | grep -q "com8 setup --service"; then ok "refusal prints the release-channel fix"; else bad "refusal prints the fix (out: $out)"; fi
if [ "$(cat "$unit_b")" = "$before_b" ]; then ok "the foreign service definition is byte-identical afterwards"; else bad "foreign service definition was modified"; fi
if [ ! -e "$FAR_B/.local/share/com8" ]; then ok "nothing was staged on the far side"; else bad "a legacy kernel was staged despite the refusal"; fi

echo "-- pair's OWN previous service definition is backed up before it is refreshed"
FAR_D="$T/far-own"; mkdir -p "$FAR_D"
case "$(uname -s)" in
  Darwin) unit_d="$FAR_D/Library/LaunchAgents/com.communicate.com8.plist";;
  *)      unit_d="$FAR_D/.config/systemd/user/com8.service";;
esac
mkdir -p "$(dirname "$unit_d")"
printf 'previous pair unit: %s/.local/share/com8/daemon/current/com8.py daemon\n' "$FAR_D" > "$unit_d"
before_d="$(cat "$unit_d")"
FAR="$FAR_D"; FARSELF=farown
out="$(ccomm com8 pair tester@far.invalid 2>&1)"; rc=$?   # steps 3+ may fail: no link forwarding here
if printf '%s' "$out" | grep -q "staged v$srcv and started"; then ok "a legacy far side with pair's own unit is staged and started"; else bad "own-unit far side staged and started (out: $out)"; fi
backup_d="$(ls "$unit_d".pair-backup-* 2>/dev/null | head -1)"
if [ -n "$backup_d" ] && [ "$(cat "$backup_d")" = "$before_d" ]; then ok "the previous definition is backed up beside it, byte-identical"; else bad "previous definition backed up (found: $backup_d)"; fi
if printf '%s' "$out" | grep -q "pair-backup"; then ok "the backup is reported"; else bad "the backup is reported (out: $out)"; fi
far_stop "$FAR_D/.local/state/communicate"

echo "-- an absent far daemon with --no-install points at the release, not a registry"
FAR_C="$T/far-empty"; mkdir -p "$FAR_C"
FAR="$FAR_C"; FARSELF=farbox
out="$(ccomm com8 pair tester@far.invalid --no-install 2>&1)"; rc=$?
if [ $rc -ne 0 ]; then ok "--no-install with no far daemon fails"; else bad "--no-install with no far daemon fails"; fi
if printf '%s' "$out" | grep -q "com8 setup --service"; then ok "the hint names the release-channel setup"; else bad "hint names com8 setup --service (out: $out)"; fi
if ! printf '%s' "$out" | grep -q "npx"; then ok "the hint no longer advertises an npm registry package"; else bad "hint still says npx"; fi
ccomm com8 stop >/dev/null 2>&1 || true

if [ "${COM8_TEST_SSH:-0}" = "1" ]; then
  echo "== full pair over ssh localhost (opt-in)"
  FARHOME="$T/farhome"
  mkdir -p "$FARHOME"
  # A fresh far HOME with no com8: pair must stage the kernel, start the
  # daemon, link both ways, sync the handle, and measure both round trips.
  acomm com8 init --handle pairtester --force >/dev/null 2>&1
  out="$(COM8_PAIR_FAR_HOME="$FARHOME" acomm com8 pair "$(whoami)@localhost" --name farbox --no-persist 2>&1)"
  rc=$?
  if [ $rc -eq 0 ]; then ok "pair localhost completes"; else bad "pair localhost completes (out: $out)"; fi
  if printf '%s' "$out" | grep -q "round trip"; then ok "pair reports measured round trips"; else bad "pair reports round trips"; fi
  out2="$(COM8_PAIR_FAR_HOME="$FARHOME" acomm com8 pair "$(whoami)@localhost" --name farbox --no-persist 2>&1)"
  if [ $? -eq 0 ] && printf '%s' "$out2" | grep -qi "kept\|already"; then
    ok "second pair run is idempotent"
  else bad "second pair run is idempotent"; fi
fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
