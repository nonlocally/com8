#!/usr/bin/env bash
# Boxed-agent-as-com8-peer: the shim (com8-boxed-init) simulated ON HOST with
# --dir pointed at the exact paths a container would publish — same sockets,
# same wire, no VM needed. Proves: claim --boxed adopts the published socket;
# mail delivers into the shim (held durably when no in-box session); the
# outbox drain routes shim sends with the boxed identity's attribution; and
# liveness is measured from the published socket.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
SHIM="$HERE/bin/com8-boxed-init"
T="$(mktemp -d /tmp/com8-boxed.XXXXXX)"
export COMM_STATE="$T/state" COM8_SOCK_DIR="$T/socks" COM8_SESSIONS_DIR="$T/sess"
export COM8_SELF=boxhost COM8_TICK=1
mkdir -p "$COM8_SESSIONS_DIR"
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
SHIMPID=""
cleanup(){ [ -n "$SHIMPID" ] && kill "$SHIMPID" 2>/dev/null; "$COMM" com8 stop >/dev/null 2>&1||true; rm -rf "$T"; }
trap cleanup EXIT

"$COMM" com8 start >/dev/null 2>&1

echo "== claim --boxed prints the publish pair"
OUT="$("$COMM" com8 claim worker9 --boxed 2>/dev/null)"
printf '%s\n' "$OUT" | grep -q 'publish-socket' && ok "claim --boxed prints publish flags" || bad "claim output ($OUT)"
IN_SOCK="$T/socks/com8-worker9.sock"
OUT_SOCK="$T/state/com8/boxes/worker9/outbox.sock"

echo "== before the box starts: identity is stored (measured absence)"
st="$("$COMM" com8 agents 2>/dev/null | grep worker9 | awk '{print $3}')"
[ "$st" = "stored" ] && ok "boxed identity reads stored while box is down" || bad "pre-box state ($st)"
"$COMM" com8 send worker9 "queued before the box existed" --from boss >/dev/null 2>&1
ok "mail accepted for a down box (durable)"

echo "== the 'box' starts: shim binds the exact published paths"
BOXDIR="$T/boxguest"   # the guest's view of /run/com8
mkdir -p "$BOXDIR"
# In a real box, `container --publish-socket HOST:GUEST` forwards these. On
# host we get identical wiring by symlinking the host paths to the shim's dir.
COM8_BOXED_SESSIONS="$T/no-sessions" python3 "$SHIM" --dir "$BOXDIR" >/dev/null 2>&1 &
SHIMPID=$!
sleep 1
ln -sf "$BOXDIR/agent.sock" "$IN_SOCK"
mkdir -p "$(dirname "$OUT_SOCK")"; ln -sf "$BOXDIR/outbox.sock" "$OUT_SOCK"
[ -S "$BOXDIR/agent.sock" ] && ok "shim bound agent.sock" || bad "shim inbound bind"
[ -S "$BOXDIR/outbox.sock" ] && ok "shim bound outbox.sock" || bad "shim outbox bind"

echo "== liveness flips to live (probed on the published socket)"
deadline=$((SECONDS+10)); st=""
while [ $SECONDS -lt $deadline ]; do
  st="$("$COMM" com8 agents 2>/dev/null | grep worker9 | awk '{print $3}')"
  [ "$st" = "live" ] && break
  sleep 1
done
[ "$st" = "live" ] && ok "boxed identity probes live once published" || bad "boxed liveness ($st)"

echo "== held mail drains INTO the box (shim holds it; no in-box session)"
deadline=$((SECONDS+12)); got=""
while [ $SECONDS -lt $deadline ]; do
  grep -q 'queued before the box existed' "$BOXDIR/inbox.jsonl" 2>/dev/null && { got=1; break; }
  sleep 1
done
[ -n "$got" ] && ok "pre-box mail reached the in-box hold file (store->wake)" || bad "in-box delivery"
"$COMM" com8 send worker9 "live line" --from boss >/dev/null 2>&1
sleep 2
grep -q 'live line' "$BOXDIR/inbox.jsonl" 2>/dev/null && ok "live mail relays into the box" || bad "live in-box mail"
# attribution wrapper survived the trip (quotes are JSON-escaped in the frame)
grep -q 'from-name=.\{0,2\}boss' "$BOXDIR/inbox.jsonl" 2>/dev/null && ok "attribution wrapper crossed the boundary" || bad "attribution in box"

echo "== outbound: shim send -> host drain -> routed with boxed attribution"
"$COMM" com8 claim manager >/dev/null 2>&1
COM8_BOXED_DIR="$BOXDIR" python3 "$SHIM" send manager "result: 42 tests green" >/dev/null 2>&1
deadline=$((SECONDS+12)); got=""
while [ $SECONDS -lt $deadline ]; do
  "$COMM" com8 inbox manager 2>/dev/null | grep -q 'result: 42 tests green' && { got=1; break; }
  sleep 1
done
[ -n "$got" ] && ok "boxed send drained + routed to a host mailbox" || bad "outbox drain"
"$COMM" com8 inbox manager 2>/dev/null | grep -q '"from_name": "worker9"' && ok "outbound mail attributed to the BOXED IDENTITY (from-rewrite)" || bad "boxed attribution"
sleep 2
n="$(ls "$BOXDIR/spool"/*.json 2>/dev/null | wc -l | tr -d ' ')"
[ "$n" = "0" ] && ok "spool cleared on ack (at-least-once complete)" || bad "spool cleanup ($n left)"

echo "== outbound dedup: a re-offered frame (lost ack) delivers exactly once"
# Spool the SAME msg_id twice by hand (simulating the shim re-offering after a
# dropped ack); the boxed identity routes both, but end-to-end msg_id dedup
# must land it in the host mailbox only once.
"$COMM" com8 claim sink >/dev/null 2>&1
before="$("$COMM" com8 inbox sink 2>/dev/null | grep -c 'dupe-once' || true)"
DUPID="fixedmsgid0000000000000000000000aa"
printf '{"to":"sink","text":"dupe-once please","msg_id":"%s","ts":1}\n' "$DUPID" > "$BOXDIR/spool/1-$DUPID.json"
sleep 2
# re-offer the identical frame (as a reconnect would)
printf '{"to":"sink","text":"dupe-once please","msg_id":"%s","ts":1}\n' "$DUPID" > "$BOXDIR/spool/2-$DUPID.json"
sleep 2
after="$("$COMM" com8 inbox sink 2>/dev/null | grep -c 'dupe-once' || true)"
if [ "$after" = "1" ]; then ok "re-offered frame delivered exactly once (end-to-end dedup)"; else bad "boxed dedup (landed $after times)"; fi

echo "== box dies: liveness honestly returns to stored; mail holds again"
kill "$SHIMPID" 2>/dev/null; SHIMPID=""
sleep 1
rm -f "$IN_SOCK" "$OUT_SOCK"   # container teardown removes published socks
deadline=$((SECONDS+10)); st=""
while [ $SECONDS -lt $deadline ]; do
  st="$("$COMM" com8 agents 2>/dev/null | grep worker9 | awk '{print $3}')"
  [ "$st" = "stored" ] && break
  sleep 1
done
[ "$st" = "stored" ] && ok "dead box reads stored again" || bad "post-box state ($st)"
"$COMM" com8 send worker9 "after death" --from boss >/dev/null 2>&1
u="$("$COMM" com8 agents --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print([a["undelivered"] for a in d["agents"] if a["name"]=="worker9"][0])')"
[ "$u" -ge 1 ] && ok "post-death mail held durably ($u undelivered)" || bad "post-death hold"

"$COMM" com8 stop >/dev/null 2>&1
echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
