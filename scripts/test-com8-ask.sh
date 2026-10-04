#!/usr/bin/env bash
# A1: message-plane completion — reply-correlated blocking ask, group send,
# durable notify. Fully isolated (own COMM_STATE / sock dir / sessions dir).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-ask.XXXXXX)"
export COMM_STATE="$T/state"
export COM8_SOCK_DIR="$T/socks"
export COM8_SESSIONS_DIR="$T/sessions"
export COM8_SELF="askhost"
export COM8_TICK=1
export COM8_NOTIFY_CMD="cat >> $T/notify-fired.log"
COM8S="$COMM_STATE/com8"
mkdir -p "$COM8_SESSIONS_DIR"

pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup() { "$COMM" com8 stop >/dev/null 2>&1 || true; rm -rf "$T"; }
trap cleanup EXIT
jget() { python3 -c '
import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1:]:
    d=d[k]
print(d)' "$@" 2>/dev/null; }

"$COMM" com8 start >/dev/null 2>&1
"$COMM" com8 claim asker  >/dev/null 2>&1
"$COMM" com8 claim target >/dev/null 2>&1

echo "== ask/reply — explicit correlation via return token"
# Fire the blocking ask in the background; capture its JSON result.
( "$COMM" com8 ask target "what is 2+2?" --from asker --timeout 12 --json > "$T/ask1.json" 2>/dev/null ) &
ASKPID=$!
# The ask lands in target's inbox carrying a return token; pull it out.
TOKEN=""
for i in $(seq 1 20); do
  TOKEN="$("$COMM" com8 inbox target 2>/dev/null | python3 -c '
import json,sys,re
tok=""
for line in sys.stdin:
    try: d=json.loads(line)
    except Exception: continue
    m=re.search(r"com8 reply (\S+)", d.get("text",""))
    if m: tok=m.group(1)
print(tok)' )"
  [ -n "$TOKEN" ] && break
  sleep 0.5
done
if [ -n "$TOKEN" ]; then ok "ask delivered a return token to target"; else bad "ask delivered a return token"; fi
if printf '%s' "$TOKEN" | grep -q '^asker@askhost~'; then ok "return token is asker@device~corr"; else bad "return token shape (got $TOKEN)"; fi
"$COMM" com8 reply "$TOKEN" "the answer is 4" --from target >/dev/null 2>&1
wait $ASKPID 2>/dev/null
if [ "$(jget ok < "$T/ask1.json")" = "True" ]; then ok "ask returned ok"; else bad "ask returned ok"; fi
if [ "$(jget reply < "$T/ask1.json")" = "the answer is 4" ]; then ok "ask returned the correlated reply"; else bad "ask returned reply (got: $(cat "$T/ask1.json"))"; fi

echo "== ask — natural reply (best-effort, no token) resolves oldest pending"
( "$COMM" com8 ask target "ping?" --from asker --timeout 12 --json > "$T/ask2.json" 2>/dev/null ) &
ASKPID=$!
sleep 1
# Reply the "natural" way: a plain message from target back to asker.
"$COMM" com8 send asker "pong (natural)" --from target >/dev/null 2>&1
wait $ASKPID 2>/dev/null
if grep -q 'pong (natural)' "$T/ask2.json"; then ok "natural reply resolved the ask"; else bad "natural reply resolved ask (got: $(cat "$T/ask2.json"))"; fi

echo "== ask — timeout returns cleanly"
start=$SECONDS
"$COMM" com8 ask target "no one answers" --from asker --timeout 3 --json > "$T/ask3.json" 2>/dev/null
dur=$((SECONDS - start))
if [ "$(jget ok < "$T/ask3.json")" = "False" ] && [ "$(jget err < "$T/ask3.json")" = "timeout" ]; then ok "ask timed out with ok:false err:timeout"; else bad "ask timeout shape (got: $(cat "$T/ask3.json"))"; fi
if [ "$dur" -ge 3 ] && [ "$dur" -le 8 ]; then ok "ask timeout respected the deadline (${dur}s)"; else bad "ask timeout duration (${dur}s)"; fi

echo "== group_send"
"$COMM" com8 claim g1 >/dev/null 2>&1
"$COMM" com8 claim g2 >/dev/null 2>&1
"$COMM" com8 group g1,g2 "hello group" --from asker >/dev/null 2>&1
sleep 0.3
c1="$("$COMM" com8 inbox g1 2>/dev/null | grep -c 'hello group')"
c2="$("$COMM" com8 inbox g2 2>/dev/null | grep -c 'hello group')"
if [ "$c1" = "1" ] && [ "$c2" = "1" ]; then ok "group_send hit both members"; else bad "group_send ($c1,$c2)"; fi

echo "== notify (durable human lane + hook)"
"$COMM" com8 notify "the build is blocked on a decision" --from asker >/dev/null 2>&1
sleep 0.3
if ls "$COM8S"/notify/*.json >/dev/null 2>&1; then ok "notify wrote a durable record"; else bad "notify durable record"; fi
if grep -q 'blocked on a decision' "$T/notify-fired.log" 2>/dev/null; then ok "notify fired COM8_NOTIFY_CMD"; else bad "notify fired hook"; fi

"$COMM" com8 stop >/dev/null 2>&1
echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
