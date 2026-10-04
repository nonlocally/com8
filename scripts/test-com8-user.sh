#!/usr/bin/env bash
# End-to-end test for the user layer: user.json, the claim ceremony (com8 init),
# handle precedence (env > user.json > OS user), handle stamping into
# card/status/agents, version surfacing, and the restore-tuple guard (no new
# per-identity fields). Fully isolated — same env discipline as test-com8-core.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-test.XXXXXX)"
export COMM_STATE="$T/state"
export COM8_SOCK_DIR="$T/socks"
export COM8_SESSIONS_DIR="$T/sessions"
export COM8_SELF="testhost"
export COM8_TICK=1
export COM8_FLEET_USER="osdefault"   # pin the getpass fallback so assertions are stable
unset COM8_FLEET 2>/dev/null || true
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

echo "== section 1: unclaimed = loud degraded mode, old behavior preserved"
"$COMM" com8 start >/dev/null 2>&1
st="$("$COMM" com8 status --json 2>/dev/null)"
if [ "$(printf '%s' "$st" | jget self user)" = "None" ]; then
  ok "status.self.user is null before any claim"
else bad "status.self.user is null before any claim (got: $(printf '%s' "$st" | jget self user))"; fi
card="$("$COMM" com8 card --json 2>/dev/null)"
if [ "$(printf '%s' "$card" | jget fleet)" = "osdefault" ]; then
  ok "card.fleet falls back to OS user when unclaimed"
else bad "card.fleet falls back to OS user (got: $(printf '%s' "$card" | jget fleet))"; fi
if [ "$(printf '%s' "$card" | jget user)" = "None" ]; then
  ok "card.user is null when unclaimed"
else bad "card.user is null when unclaimed"; fi
if "$COMM" com8 user 2>&1 | grep -qi "unclaimed"; then
  ok "com8 user says unclaimed"
else bad "com8 user says unclaimed"; fi

echo "== section 2: the claim ceremony"
if "$COMM" com8 init --handle tester1 --display "Tester One" >/dev/null 2>&1; then
  ok "com8 init --handle tester1"
else bad "com8 init --handle tester1"; fi
if [ -f "$COM8S/user.json" ]; then ok "user.json written"; else bad "user.json written"; fi
u="$(cat "$COM8S/user.json" 2>/dev/null)"
if [ "$(printf '%s' "$u" | jget v)" = "1" ] && [ "$(printf '%s' "$u" | jget handle)" = "tester1" ] \
   && [ "$(printf '%s' "$u" | jget display)" = "Tester One" ] \
   && [ -n "$(printf '%s' "$u" | jget created_at)" ]; then
  ok "user.json has the v1 shape (v, handle, display, created_at)"
else bad "user.json has the v1 shape"; fi
if "$COMM" com8 user 2>/dev/null | grep -q "@tester1"; then
  ok "com8 user shows @tester1"
else bad "com8 user shows @tester1"; fi

echo "== section 3: the handle rides every surface (re-derived, never copied)"
card="$("$COMM" com8 card --json 2>/dev/null)"
if [ "$(printf '%s' "$card" | jget user)" = "tester1" ]; then
  ok "card.user carries the handle"
else bad "card.user carries the handle"; fi
if [ "$(printf '%s' "$card" | jget fleet)" = "tester1" ]; then
  ok "card.fleet == handle (wire back-compat field follows the claim)"
else bad "card.fleet == handle (got: $(printf '%s' "$card" | jget fleet))"; fi
st="$("$COMM" com8 status --json 2>/dev/null)"
if [ "$(printf '%s' "$st" | jget self user handle)" = "tester1" ]; then
  ok "status.self.user.handle"
else bad "status.self.user.handle"; fi
ag="$("$COMM" com8 agents --json 2>/dev/null)"
if [ "$(printf '%s' "$ag" | jget user)" = "tester1" ]; then
  ok "agents payload carries the user"
else bad "agents payload carries the user"; fi

echo "== section 4: precedence env > user.json > OS user"
"$COMM" com8 stop >/dev/null 2>&1; sleep 0.5
COM8_FLEET=envwins "$COMM" com8 start >/dev/null 2>&1
card="$("$COMM" com8 card --json 2>/dev/null)"
if [ "$(printf '%s' "$card" | jget fleet)" = "envwins" ]; then
  ok "COM8_FLEET env still overrides (test hook preserved)"
else bad "COM8_FLEET env still overrides (got: $(printf '%s' "$card" | jget fleet))"; fi
"$COMM" com8 stop >/dev/null 2>&1; sleep 0.5
"$COMM" com8 start >/dev/null 2>&1   # NO env — the launchd hole: file must win
card="$("$COMM" com8 card --json 2>/dev/null)"
if [ "$(printf '%s' "$card" | jget fleet)" = "tester1" ]; then
  ok "daemon restarted with no env: handle comes from user.json (launchd hole closed)"
else bad "daemon restarted with no env: handle from user.json (got: $(printf '%s' "$card" | jget fleet))"; fi
st="$("$COMM" com8 status --json 2>/dev/null)"
if [ "$(printf '%s' "$st" | jget self user handle)" = "tester1" ]; then
  ok "user survives daemon restart (loaded, not regenerated)"
else bad "user survives daemon restart"; fi

echo "== section 5: re-claim is refused, --force re-derives"
if "$COMM" com8 init --handle tester2 >/dev/null 2>&1; then
  bad "changing the handle without --force must be refused"
else ok "changing the handle without --force refused"; fi
if [ "$(cat "$COM8S/user.json" | jget handle)" = "tester1" ]; then
  ok "refused re-claim left user.json untouched"
else bad "refused re-claim left user.json untouched"; fi
if "$COMM" com8 init --handle tester2 --force >/dev/null 2>&1; then
  ok "com8 init --force changes the handle"
else bad "com8 init --force changes the handle"; fi
card="$("$COMM" com8 card --json 2>/dev/null)"
if [ "$(printf '%s' "$card" | jget user)" = "tester2" ]; then
  ok "card re-derives the new handle immediately (re-assertion, not write-once)"
else bad "card re-derives the new handle immediately"; fi
if "$COMM" com8 init --handle tester2 --display "Renamed" >/dev/null 2>&1; then
  ok "same handle, new display needs no --force"
else bad "same handle, new display needs no --force"; fi

echo "== section 6: validation"
for h in "Tester" "has_underscore" "-leading" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" "pm" "self" "all" "com8"; do
  if "$COMM" com8 init --handle "$h" --force >/dev/null 2>&1; then
    bad "invalid handle accepted: $h"
  else ok "invalid handle refused: $h"; fi
done
H32="$(python3 -c 'print("a"*32)')"
if "$COMM" com8 init --handle "$H32" --force >/dev/null 2>&1; then
  ok "a 32-char handle (the max) is legal"
else bad "a 32-char handle (the max) is legal"; fi
"$COMM" com8 init --handle tester2 --force >/dev/null 2>&1
if [ "$(cat "$COM8S/user.json" | jget handle)" = "tester2" ]; then
  ok "invalid claims never touched user.json"
else bad "invalid claims never touched user.json"; fi

echo "== section 7: version is surfaced and matches the source"
src_v="$(grep -m1 '^COM8_VERSION' "$HERE/lib/com8.py" | cut -d'"' -f2)"
st="$("$COMM" com8 status --json 2>/dev/null)"
if [ -n "$src_v" ] && [ "$(printf '%s' "$st" | jget self version)" = "$src_v" ]; then
  ok "status.self.version == COM8_VERSION ($src_v)"
else bad "status.self.version == COM8_VERSION (src=$src_v got=$(printf '%s' "$st" | jget self version))"; fi

echo "== section 8: restore-tuple guard — no new per-identity fields"
"$COMM" com8 claim guardcheck >/dev/null 2>&1
"$COMM" com8 stop >/dev/null 2>&1; sleep 0.5
"$COMM" com8 start >/dev/null 2>&1; sleep 1.5
keys="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(",".join(sorted(d["guardcheck"].keys())))' "$COM8S/identities.json" 2>/dev/null)"
golden="aliases,boxed,card,claimed_at,home,kind,place,seat,supervision,surface,workspace"
if [ "$keys" = "$golden" ]; then
  ok "identity record keys unchanged ($keys)"
else bad "identity record keys unchanged (got: $keys, want: $golden)"; fi

echo "== section 9: a user-less legacy state dir boots clean"
"$COMM" com8 stop >/dev/null 2>&1; sleep 0.5
rm -f "$COM8S/user.json"
"$COMM" com8 start >/dev/null 2>&1; sleep 1
st="$("$COMM" com8 status --json 2>/dev/null)"
if [ "$(printf '%s' "$st" | jget ok)" = "True" ] && [ "$(printf '%s' "$st" | jget self user)" = "None" ]; then
  ok "boot without user.json: daemon healthy, user null again"
else bad "boot without user.json: daemon healthy, user null again"; fi
"$COMM" com8 stop >/dev/null 2>&1; sleep 0.5
printf '{"v":1,"handle":12}' > "$COM8S/user.json"   # wrong TYPE, valid JSON
"$COMM" com8 start >/dev/null 2>&1; sleep 1
st="$("$COMM" com8 status --json 2>/dev/null)"
if [ "$(printf '%s' "$st" | jget ok)" = "True" ] && [ "$(printf '%s' "$st" | jget self user)" = "None" ]; then
  ok "a non-string handle degrades to unclaimed (no crash-loop)"
else bad "a non-string handle degrades to unclaimed (got: $(printf '%s' "$st" | head -c 120))"; fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
