#!/usr/bin/env bash
# Cross-fleet: two isolated daemons as two OPERATORS (fleets) linked over direct
# sockets. Verifies the deny-by-default grant model: ungranted names are refused
# with one ambiguous error, granted+claimed names deliver, no auto-claim oracle,
# foreign senders proxy fleet-qualified, control ops demand the token once a
# fleet link exists, and card/invite encoding round-trips.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-fleet.XXXXXX)"
mkdir -p "$T/a-sess" "$T/b-sess"
acomm(){ COMM_STATE="$T/a" COM8_SOCK_DIR="$T/as" COM8_SESSIONS_DIR="$T/a-sess" \
         COM8_SELF=alice-dev COM8_FLEET=alice COM8_TICK=1 "$COMM" "$@"; }
bcomm(){ COMM_STATE="$T/b" COM8_SOCK_DIR="$T/bs" COM8_SESSIONS_DIR="$T/b-sess" \
         COM8_SELF=bob-dev COM8_FLEET=bob COM8_TICK=1 "$COMM" "$@"; }
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup(){ acomm com8 stop >/dev/null 2>&1||true; bcomm com8 stop >/dev/null 2>&1||true; rm -rf "$T"; }
trap cleanup EXIT

acomm com8 start >/dev/null 2>&1; bcomm com8 start >/dev/null 2>&1

echo "== fleet links both ways (direct socket transport; kind=fleet)"
acomm com8 link bob   --fleet --sock "$T/b/com8/in/alice.sock" >/dev/null 2>&1
bcomm com8 link alice --fleet --sock "$T/a/com8/in/bob.sock" >/dev/null 2>&1
grep -q '"kind": "fleet"' "$T/a/com8/links.json" && ok "A persisted a fleet link" || bad "A fleet link kind"
grep -q '"kind": "fleet"' "$T/b/com8/links.json" && ok "B persisted a fleet link" || bad "B fleet link kind"

echo "== deny-by-default: A -> ungranted name on B is refused ambiguously"
bcomm com8 claim librarian >/dev/null 2>&1     # exists on B but NOT granted
acomm com8 send librarian@bob "hello?" --from scout >/dev/null 2>&1
sleep 2.5
dl="$(ls "$T/a/com8/out/bob/dead" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$dl" -ge 1 ]; then ok "ungranted send dead-lettered on the sender"; else bad "ungranted send (dead=$dl)"; fi
if ! bcomm com8 inbox librarian 2>/dev/null | grep -q 'hello?'; then ok "nothing landed in the ungranted inbox"; else bad "ungranted mail leaked in"; fi

echo "== no-auto-claim oracle: granted-but-unclaimed name is also refused"
bcomm com8 grant alice ghost >/dev/null 2>&1   # granted but never claimed
acomm com8 send ghost@bob "anyone?" --from scout >/dev/null 2>&1
sleep 2.5
if [ ! -d "$T/b/com8/mail/ghost" ]; then ok "no mailbox was created for a foreigner"; else bad "mailbox-creation oracle"; fi

echo "== the granted path: grant librarian, mail flows, proxy is fleet-qualified"
bcomm com8 grant alice librarian >/dev/null 2>&1
acomm com8 send librarian@bob "REQUEST sweep budget=2h" --from orchestrator >/dev/null 2>&1
deadline=$((SECONDS+12)); got=""
while [ $SECONDS -lt $deadline ]; do
  if bcomm com8 inbox librarian 2>/dev/null | grep -q 'REQUEST sweep'; then got=1; break; fi
  sleep 1
done
[ -n "$got" ] && ok "granted mail crossed the fleet boundary" || bad "granted mail delivery"
if bcomm com8 inbox librarian 2>/dev/null | grep -q 'orchestrator@alice'; then
  ok "sender proxied fleet-qualified (orchestrator@alice)"
else bad "fleet-qualified attribution"; fi
if grep -q '"orchestrator@alice"' "$T/b/com8/identities.json" 2>/dev/null; then
  ok "proxy identity stored fleet-qualified"
else bad "proxy stored bare (squat risk)"; fi

echo "== control token: required once a fleet link exists"
tok="$(cat "$T/b/com8/control.token")"
raw="$(python3 - "$T/b/com8/com8.sock" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])
s.sendall(b'{"op":"claim","name":"intruder"}\n'); s.shutdown(socket.SHUT_WR)
buf = b""
while b"\n" not in buf:
    c = s.recv(4096)
    if not c: break
    buf += c
print(buf.decode().strip())
PY
)"
if printf '%s' "$raw" | grep -q 'control token required'; then ok "tokenless control op refused"; else bad "token gate (got: $raw)"; fi
if ! grep -q '"intruder"' "$T/b/com8/identities.json" 2>/dev/null; then ok "intruder claim did not land"; else bad "intruder claimed"; fi
# and the legit CLI (which reads the token file) still works:
bcomm com8 claim tokentest >/dev/null 2>&1
grep -q '"tokentest"' "$T/b/com8/identities.json" && ok "token-bearing CLI still works" || bad "CLI with token"

echo "== control gate covers status too (no roster oracle for a fleet peer)"
raw="$(python3 - "$T/b/com8/com8.sock" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])
s.sendall(b'{"op":"status"}\n'); s.shutdown(socket.SHUT_WR)
buf=b""
while b"\n" not in buf:
    c=s.recv(4096)
    if not c: break
    buf+=c
print(buf.decode().strip())
PY
)"
if printf '%s' "$raw" | grep -q 'control token required'; then ok "tokenless status refused (no enumeration)"; else bad "status gate (got: $raw)"; fi
if ! printf '%s' "$raw" | grep -q 'librarian'; then ok "no identity names leaked to an unauthed status"; else bad "status leaked names"; fi
# the token-bearing CLI status still works
bcomm com8 status >/dev/null 2>&1 && ok "authed status still works" || bad "authed status"

echo "== authorized_keys injection is refused (newline in a card pubkey)"
BADCARD="$(python3 -c '
import base64,json
c={"v":1,"kind":"com8-card","fleet":"evil","device":"evilbox","addr":"e@evilbox",
   "tailscale_ip":"100.64.0.9","fingerprint":"SHA256:x","inbound_dir":"/tmp/x/in",
   "pubkey":"ssh-ed25519 AAAAreal keyx\nssh-ed25519 AAAABACKDOOR attacker@evil"}
print(base64.b64encode(json.dumps(c).encode()).decode())')"
FAKEHOME="$T/fakehome"; mkdir -p "$FAKEHOME/.ssh"
inj="$(HOME="$FAKEHOME" bcomm com8 federate accept evil --card "$BADCARD" --yes 2>&1)"
if printf '%s' "$inj" | grep -qi 'refusing card\|not a single'; then ok "newline-injected pubkey refused"; else bad "injection not refused (got: $inj)"; fi
if [ ! -f "$FAKEHOME/.ssh/authorized_keys" ] || ! grep -q 'BACKDOOR' "$FAKEHOME/.ssh/authorized_keys" 2>/dev/null; then ok "no backdoor key written"; else bad "BACKDOOR key landed in authorized_keys"; fi

echo "== card / invite encoding round-trips"
CARD="$(acomm com8 card 2>/dev/null)"
if printf '%s' "$CARD" | python3 -c '
import base64,json,sys
c=json.loads(base64.b64decode(sys.stdin.read().strip()))
assert c["kind"]=="com8-card" and c["fleet"]=="alice" and c["pubkey"].startswith("ssh-ed25519")
assert c["inbound_dir"].endswith("/com8/in")
print("ok")' 2>/dev/null | grep -q ok; then ok "card encodes fleet+pubkey+inbound_dir"; else bad "card contents"; fi
acomm com8 federate status 2>/dev/null | grep -q 'fleet=alice' && ok "federate status prints the fleet" || bad "federate status"

echo "== cross-fleet ask/reply: token rewritten at the boundary, reply routes home"
bcomm com8 grant alice librarian >/dev/null 2>&1
( acomm com8 ask librarian@bob "what is 2+2?" --from orchestrator --timeout 25 --json > "$T/ask.json" 2>/dev/null ) &
ASKPID=$!
deadline=$((SECONDS+15)); TOKEN=""
while [ $SECONDS -lt $deadline ]; do
  TOKEN="$(bcomm com8 inbox librarian 2>/dev/null | grep -o 'com8 reply [^ ]*' | tail -1 | awk '{print $3}')"
  [ -n "$TOKEN" ] && break
  sleep 1
done
if printf '%s' "$TOKEN" | grep -q '^orchestrator@alice~'; then
  ok "ask token rewritten to the fleet petname ($TOKEN)"
else bad "token rewrite (got: $TOKEN)"; fi
bcomm com8 reply "$TOKEN" "it is 4" --from librarian >/dev/null 2>&1
wait $ASKPID
if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("ok") and d.get("reply")=="it is 4" else 1)' "$T/ask.json" 2>/dev/null; then
  ok "cross-fleet ask got the correlated reply"
else bad "cross-fleet ask/reply (got: $(cat "$T/ask.json" 2>/dev/null))"; fi
if grep -q '"orchestrator"' "$T/a/com8/grants/bob.json" 2>/dev/null; then
  ok "asking auto-granted the return path (orchestrator to fleet bob)"
else bad "return-path auto-grant"; fi

echo "== grants listing + ungrant"
bcomm com8 ungrant alice librarian >/dev/null 2>&1
acomm com8 send librarian@bob "after revoke" --from orchestrator >/dev/null 2>&1
sleep 2.5
if ! bcomm com8 inbox librarian 2>/dev/null | grep -q 'after revoke'; then ok "ungrant closes the door again"; else bad "ungrant"; fi

acomm com8 stop >/dev/null 2>&1; bcomm com8 stop >/dev/null 2>&1
echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
