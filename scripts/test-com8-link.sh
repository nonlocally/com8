#!/usr/bin/env bash
# Com8↔com8 link test: two isolated com8s on one host,
# linked over DIRECT socket paths (the --sock transport override; the ssh
# transport wraps exactly this wire protocol). Covers: envelope delivery with
# arrival-line attribution, acks emptying the outbound queue, hold-and-retry
# while the far side is down, dedup by msg_id, proxy identities for remote
# senders, and reply routing with auto-claimed mailboxes.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-link.XXXXXX)"

A_STATE="$T/a-state"; A_SOCKS="$T/a-socks"; A_SESS="$T/a-sess"
B_STATE="$T/b-state"; B_SOCKS="$T/b-socks"; B_SESS="$T/b-sess"
mkdir -p "$A_SESS" "$B_SESS"

# Only the final SSH-failure case uses ssh; all real link exchanges below use
# local Unix sockets. Fail deterministically instead of relying on a DNS lookup
# of .invalid completing within three seconds (macOS resolvers can take longer).
# Actual authenticated SSH delivery is qualified separately on installed devices.
mkdir -p "$T/sshbin"
cat > "$T/sshbin/ssh" <<'SH'
#!/bin/sh
printf 'fixture: SSH endpoint unavailable\n' >&2
exit 255
SH
chmod +x "$T/sshbin/ssh"

acomm() { PATH="$T/sshbin:$PATH" COMM_STATE="$A_STATE" COM8_SOCK_DIR="$A_SOCKS" COM8_SESSIONS_DIR="$A_SESS" COM8_SELF=alpha COM8_TICK=1 "$COMM" "$@"; }
bcomm() { COMM_STATE="$B_STATE" COM8_SOCK_DIR="$B_SOCKS" COM8_SESSIONS_DIR="$B_SESS" COM8_SELF=beta  COM8_TICK=1 "$COMM" "$@"; }

pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup() { acomm com8 stop >/dev/null 2>&1 || true; bcomm com8 stop >/dev/null 2>&1 || true; rm -rf "$T"; }
trap cleanup EXIT

wait_for() { # wait_for <deadline-s> <desc> <cmd...>
  local dl=$((SECONDS + $1)); shift
  local desc="$1"; shift
  while [ $SECONDS -lt $dl ]; do
    if "$@" >/dev/null 2>&1; then ok "$desc"; return 0; fi
    sleep 0.5
  done
  bad "$desc"; return 1
}

echo "== setup: two com8s, symmetric links"
acomm com8 start >/dev/null 2>&1 || bad "A start"
bcomm com8 start >/dev/null 2>&1 || bad "B start"
bcomm com8 claim remote-x >/dev/null 2>&1 || bad "B claim remote-x"
# A's outbound targets B's inbound-for-alpha; B's outbound targets A's inbound-for-beta.
if acomm com8 link beta --sock "$B_STATE/com8/in/alpha.sock" >/dev/null 2>&1; then ok "A link beta"; else bad "A link beta"; fi
if bcomm com8 link alpha --sock "$A_STATE/com8/in/beta.sock" >/dev/null 2>&1; then ok "B link alpha"; else bad "B link alpha"; fi
[ -S "$A_STATE/com8/in/beta.sock" ] && ok "A inbound-for-beta bound" || bad "A inbound-for-beta bound"
[ -S "$B_STATE/com8/in/alpha.sock" ] && ok "B inbound-for-alpha bound" || bad "B inbound-for-alpha bound"

echo "== cross-com8 delivery + ack"
acomm com8 send remote-x@beta "hello across" --from tester >/dev/null 2>&1 || bad "A send remote-x@beta"
BX="$B_STATE/com8/mail/remote-x/inbox.jsonl"
wait_for 10 "delivered into B's remote-x inbox" grep -q "hello across" "$BX"
if grep -q '"via": "alpha"' "$BX"; then ok "arrival-line attribution (via alpha)"; else bad "arrival-line attribution"; fi
sleep 1
qn="$(ls "$A_STATE/com8/out/beta" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$qn" = "0" ]; then ok "A outbound queue empty (acked)"; else bad "A outbound queue empty (got $qn)"; fi

echo "== proxy identity for the remote sender"
wait_for 6 "B grew proxy socket for tester" test -S "$B_SOCKS/com8-tester.sock"
tside=""
for f in "$B_SESS"/*.json; do
  [ -f "$f" ] || continue
  grep -q '"name":"tester"' "$f" 2>/dev/null && grep -q '"version":"communicate-com8"' "$f" 2>/dev/null && { tside="$f"; break; }
done
if [ -n "$tside" ]; then ok "B planted proxy sidecar"; else bad "B planted proxy sidecar"; fi
if python3 - "$B_STATE/com8/identities.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
p = d["tester"]["place"]
assert p == {"kind": "remote", "device": "alpha"}, p
PY
then ok "proxy place is remote, not fabricated local"; else bad "proxy place honesty"; fi

echo "== reply routing (proxy -> link -> auto-claimed mailbox)"
python3 "$HERE/lib/cc_peer.py" send --to "$B_SOCKS/com8-tester.sock" --text "reply back" \
  --from "$B_SOCKS/replier.sock" --name remote-x 2>/dev/null
AT="$A_STATE/com8/mail/tester/inbox.jsonl"
wait_for 10 "reply landed in A's auto-claimed tester mailbox" grep -q "reply back" "$AT"
if grep -q '"from_name": "remote-x"' "$AT"; then ok "reply attribution preserved"; else bad "reply attribution preserved"; fi

echo "== hold + retry while far side down"
bcomm com8 stop >/dev/null 2>&1; sleep 1
acomm com8 send remote-x@beta "while down" --from tester >/dev/null 2>&1
sleep 2
qn="$(ls "$A_STATE/com8/out/beta" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$qn" = "1" ]; then ok "queued while beta down"; else bad "queued while beta down (got $qn)"; fi
bcomm com8 start >/dev/null 2>&1
wait_for 15 "drained after beta restart" grep -q "while down" "$BX"
sleep 1
qn="$(ls "$A_STATE/com8/out/beta" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$qn" = "0" ]; then ok "queue emptied after restart"; else bad "queue emptied (got $qn)"; fi

echo "== dedup by msg_id at the receiving com8"
python3 - "$B_STATE/com8/in/alpha.sock" <<'PY'
import json, socket, sys
env = {"v": 1, "kind": "m", "to": "remote-x", "from": "tester",
       "msg_id": "dupenv42", "text": "dup envelope", "ts": 0}
for _ in range(2):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5); s.connect(sys.argv[1])
    s.sendall((json.dumps(env) + "\n").encode())
    print(s.recv(4096).decode().strip())
    s.close()
PY
sleep 0.5
if [ "$(grep -c 'dup envelope' "$BX")" = "1" ]; then ok "envelope dedup"; else bad "envelope dedup"; fi

echo "== dead-letter + local validation (a poison envelope must not wedge the queue)"
if acomm com8 send 'Bad@beta' "nope" --from tester >/dev/null 2>&1; then
  bad "invalid remote name refused locally"
else ok "invalid remote name refused locally"; fi
python3 - "$A_STATE/com8/out/beta" <<'PY'
import json, os, sys
d = sys.argv[1]; os.makedirs(d, exist_ok=True)
env = {"v": 1, "kind": "m", "to": "no such name", "from": "tester",
       "msg_id": "poison1", "text": "poison", "ts": 0}
open(os.path.join(d, "0000000000000000-poison1.json"), "w").write(json.dumps(env))
PY
acomm com8 send remote-x@beta "after poison" --from tester >/dev/null 2>&1
wait_for 12 "queue drained past the poison envelope" grep -q "after poison" "$BX"
if [ -f "$A_STATE/com8/out/beta/dead/0000000000000000-poison1.json" ]; then
  ok "poison dead-lettered"
else bad "poison dead-lettered"; fi

echo "== ssh transport dial (golden command + graceful failure)"
cmd="$(acomm com8 link gamma --addr fake@nowhere.invalid --print-cmd 2>/dev/null)"
case "$cmd" in
  "ssh -N -o BatchMode=yes -o ConnectTimeout=8 -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o StreamLocalBindMask=0177 -o StreamLocalBindUnlink=yes -L $A_STATE/com8/links/gamma.sock:<REMOTE_HOME>/.local/state/communicate/com8/in/alpha.sock fake@nowhere.invalid")
    ok "golden ssh dial command";;
  *) bad "golden ssh dial command (got: $cmd)";;
esac
acomm com8 link gamma --addr fake@nowhere.invalid >/dev/null 2>&1 || bad "link gamma (addr)"
acomm com8 send remote-y@gamma "never arrives" --from tester >/dev/null 2>&1
sleep 3
qn="$(ls "$A_STATE/com8/out/gamma" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$qn" = "1" ]; then ok "queued for unreachable addr link"; else bad "queued for unreachable addr link (got $qn)"; fi
st="$(acomm com8 status --json 2>/dev/null)"
if [ -n "$st" ]; then ok "daemon healthy despite dead link"; else bad "daemon healthy despite dead link"; fi
if printf '%s' "$st" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["links"]["gamma"]["last_err"] else 1)' 2>/dev/null; then
  ok "link gamma reports last_err"
else bad "link gamma reports last_err"; fi
acomm com8 unlink gamma >/dev/null 2>&1 && ok "unlink gamma" || bad "unlink gamma"

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
