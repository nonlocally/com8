#!/usr/bin/env bash
# A2: the seat plane over a real (isolated) tmux server. Spawns a bash seat and
# drives it through spawn/send/read/state/wait/bind/kill. Uses a throwaway tmux
# socket so it never touches the user's ambient tmux.
set -uo pipefail
command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not installed"; exit 0; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-seat.XXXXXX)"
TMUXSOCK="com8-seat-test-$$"
export COMM_STATE="$T/state"
export COM8_SOCK_DIR="$T/socks"
export COM8_SESSIONS_DIR="$T/sessions"
export COM8_SELF="seathost"
export COM8_TICK=1
export COM8_TMUX_SOCKET="$TMUXSOCK"
export COM8_SEAT_SESSION="seat-test"
mkdir -p "$COM8_SESSIONS_DIR"

pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup() {
  "$COMM" com8 stop >/dev/null 2>&1 || true
  tmux -L "$TMUXSOCK" kill-server >/dev/null 2>&1 || true
  rm -rf "$T"
}
trap cleanup EXIT

"$COMM" com8 start >/dev/null 2>&1

echo "== spawn a bash seat"
SEAT="$("$COMM" com8 seat spawn 'bash --norc --noprofile' --name work 2>/dev/null)"
if printf '%s' "$SEAT" | grep -q '^%[0-9]'; then ok "seat spawned ($SEAT)"; else bad "seat spawn (got $SEAT)"; fi
sleep 1.2

echo "== state of an idle shell"
st="$("$COMM" com8 seat state "$SEAT" 2>/dev/null)"
if [ "$st" = "idle" ]; then ok "idle shell reads idle"; else bad "idle shell state (got $st)"; fi

echo "== send + read (deliver-and-verify)"
"$COMM" com8 seat send "$SEAT" 'echo COM8SEAT_MARKER_9713' >/dev/null 2>&1
sleep 0.8
scr="$("$COMM" com8 seat read "$SEAT" 2>/dev/null)"
if printf '%s' "$scr" | grep -q 'COM8SEAT_MARKER_9713'; then ok "send delivered + read saw the output"; else bad "send/read roundtrip"; fi

echo "== busy detection: a foreground process reads busy, then settles idle"
# A long-lived foreground command so the busy window can't expire mid-check
# under load (a fixed short sleep raced the sampler -- see history). Poll for
# busy instead of a single fixed-delay sample, same idiom as the
# deadline=$((SECONDS+N)) loops in test-com8-move.sh / test-com8-fleet.sh.
"$COMM" com8 seat send "$SEAT" 'sleep 30' >/dev/null 2>&1
deadline=$((SECONDS+10)); busy=""
while [ $SECONDS -lt $deadline ]; do
  st="$("$COMM" com8 seat state "$SEAT" 2>/dev/null)"
  if [ "$st" = "busy" ]; then busy=1; break; fi
  sleep 0.3
done
if [ -n "$busy" ]; then ok "running sleep reads busy"; else bad "busy detection (got $st)"; fi
# seat wait should observe busy and then settle to idle -- without waiting
# out the full 30s. Interrupt the sleep for real (a background job fires a
# Ctrl-C on the pane a few seconds in, once wait is certain to have sampled
# busy). `seat interrupt` sends Escape, which a foreground `sleep` ignores
# (verified: state stayed busy through it); a real SIGINT on the pane is the
# only thing that ends it without killing/respawning the seat, so this still
# exercises a genuine busy->idle transition, not a busy->dead one.
( sleep 3; tmux -L "$TMUXSOCK" send-keys -t "$SEAT" C-c ) &
intpid=$!
w="$("$COMM" com8 seat wait "$SEAT" --timeout 20 2>/dev/null)"
wait "$intpid" 2>/dev/null
if printf '%s' "$w" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("state")=="idle" and d.get("sawbusy") else 1)' 2>/dev/null; then
  ok "seat wait saw busy and settled to idle"
else bad "seat wait (got $w)"; fi

echo "== secret redaction on read (unless --raw)"
"$COMM" com8 seat send "$SEAT" 'echo tok_sk-abcdef0123456789abcd' >/dev/null 2>&1
sleep 0.6
red="$("$COMM" com8 seat read "$SEAT" 2>/dev/null)"
raw="$("$COMM" com8 seat read "$SEAT" --raw 2>/dev/null)"
if printf '%s' "$red" | grep -q 'REDACTED' && ! printf '%s' "$red" | grep -q 'sk-abcdef0123456789'; then
  ok "read redacts a token by default"
else bad "read redaction"; fi
if printf '%s' "$raw" | grep -q 'sk-abcdef0123456789'; then ok "--raw shows the token"; else bad "--raw passthrough"; fi

echo "== seat ls + bind to an identity"
"$COMM" com8 claim worker >/dev/null 2>&1
"$COMM" com8 seat bind "$SEAT" worker >/dev/null 2>&1
if grep -q "\"seat\": \"$SEAT\"" "$COMM_STATE/com8/identities.json" 2>/dev/null; then ok "bind recorded seat on the identity"; else bad "seat bind persisted"; fi
if "$COMM" com8 seat ls 2>/dev/null | grep -q "$SEAT"; then ok "seat ls lists the seat"; else bad "seat ls"; fi

echo "== kill + dead detection"
"$COMM" com8 seat kill "$SEAT" >/dev/null 2>&1
sleep 0.5
st="$("$COMM" com8 seat state "$SEAT" 2>/dev/null)"
if [ "$st" = "dead" ]; then ok "killed seat reads dead"; else bad "dead detection (got $st)"; fi

echo "== the surface is measured, never asserted"
"$COMM" com8 claim surf >/dev/null 2>&1
S2="$("$COMM" com8 seat spawn 'bash --norc --noprofile' 2>/dev/null)"
"$COMM" com8 seat bind "$S2" surf >/dev/null 2>&1
sleep 1
"$COMM" com8 agents --json 2>/dev/null | python3 -c '
import json,sys
a={x["name"]:x for x in json.load(sys.stdin)["agents"]}
s=a["surf"]["surface"]
assert s and s["driver"]=="tmux" and s["state"] in ("idle","busy","booting"), s
' && ok "a live seat reports a measured surface" || bad "surface measurement"

"$COMM" com8 seat kill "$S2" >/dev/null 2>&1
sleep 1
"$COMM" com8 agents --json 2>/dev/null | python3 -c '
import json,sys
a={x["name"]:x for x in json.load(sys.stdin)["agents"]}
s=a["surf"]["surface"]
assert s["state"]=="dead", s
' && ok "a dead seat reports dead, not a stale handle" || bad "dead surface honesty"

echo "== the seat relay: opt-in per binding is the boundary"
"$COMM" com8 claim tester >/dev/null 2>&1
"$COMM" com8 claim relaybot >/dev/null 2>&1
"$COMM" com8 claim shellbot >/dev/null 2>&1
"$COMM" com8 claim bootbot >/dev/null 2>&1
"$COMM" com8 claim quietbot >/dev/null 2>&1
"$COMM" com8 claim spoofbot >/dev/null 2>&1
# a fake agent by NAME only: a real shell copied to a file literally named
# "node" — the reviewer's spoof. pane_current_command reads "node", so any
# surface-sniffing gate is fooled; only the opt-in stops it.
mkdir -p "$T/fakebin"; cp "$(command -v bash)" "$T/fakebin/node"
ASEAT="$("$COMM" com8 seat spawn 'node -e "process.stdin.resume()"' --name relayagent 2>/dev/null)"
BSEAT="$("$COMM" com8 seat spawn 'bash --norc --noprofile' --name relayshell 2>/dev/null)"
CSEAT="$("$COMM" com8 seat spawn 'node -i' --name relaybooting 2>/dev/null)"
QSEAT="$("$COMM" com8 seat spawn 'node -e "process.stdin.resume()"' --name relayquiet 2>/dev/null)"
SSEAT="$("$COMM" com8 seat spawn "$T/fakebin/node --norc --noprofile" --name relayspoof 2>/dev/null)"
sleep 2
# opted IN: agent, shell, booting-agent
"$COMM" com8 seat bind "$ASEAT" relaybot --relay >/dev/null 2>&1
"$COMM" com8 seat bind "$BSEAT" shellbot --relay >/dev/null 2>&1
"$COMM" com8 seat bind "$CSEAT" bootbot --relay >/dev/null 2>&1
# opted OUT (default): a real agent, and the spoofed-node shell
"$COMM" com8 seat bind "$QSEAT" quietbot >/dev/null 2>&1
"$COMM" com8 seat bind "$SSEAT" spoofbot >/dev/null 2>&1

# THE boundary: a binding NOT opted in never receives typed mail, even on a
# real agent surface, even when the surface spoofs an agent name.
"$COMM" com8 send quietbot "hello you should not see this" --from tester >/dev/null 2>&1
"$COMM" com8 send spoofbot "touch $T/SPOOF_PWNED #" --from tester >/dev/null 2>&1
sleep 6
if tmux -L "$TMUXSOCK" capture-pane -t "$QSEAT" -p 2>/dev/null | grep -q "should not see this"; then
  bad "typed into a NON-opted-in agent seat (opt-in gate bypassed)"
else ok "no --relay: agent seat holds its mail (opt-in is the gate)"; fi
if [ -f "$T/SPOOF_PWNED" ]; then
  bad "SPOOFED-NODE SHELL EXECUTED MAIL (the reviewer exploit, no opt-in)"
else ok "no --relay: a shell spoofing 'node' cannot be driven by mail"; fi

# opted-in shell: the agent-surface guard still holds it (defense in depth)
"$COMM" com8 send shellbot "touch $T/SHELL_PWNED #" --from tester >/dev/null 2>&1
sleep 5
if [ -f "$T/SHELL_PWNED" ]; then bad "opted-in bare shell executed mail"
else ok "opted-in bare shell still held (agent-surface guard)"; fi

# opted-in agent, idle: delivered, with attribution + reply path
"$COMM" com8 send relaybot "hello from the mail plane" --from tester >/dev/null 2>&1
typed=""
for i in $(seq 1 12); do
  tmux -L "$TMUXSOCK" capture-pane -t "$ASEAT" -p 2>/dev/null | grep -q "hello from the mail plane" && { typed=1; break; }
  sleep 1
done
if [ -n "$typed" ]; then ok "opted-in agent seat receives typed mail"; else bad "agent seat relay typing"; fi
cap="$(tmux -L "$TMUXSOCK" capture-pane -t "$ASEAT" -p 2>/dev/null)"
if printf '%s' "$cap" | grep -q "from @tester"; then ok "typed mail carries attribution"
else bad "typed attribution"; fi
if printf '%s' "$cap" | grep -q "communicate com8 send tester"; then ok "typed mail teaches the reply path"
else bad "typed reply instruction"; fi
sleep 3
n="$(tmux -L "$TMUXSOCK" capture-pane -t "$ASEAT" -p 2>/dev/null | grep -c "hello from the mail plane")"
if [ "$n" = "1" ]; then ok "cursor advanced: delivered once, never retyped"
else bad "retype guard (copies=$n)"; fi

# opted-in agent, NOT idle (node -i banner reads booting): state gate holds
"$COMM" com8 send bootbot "should wait for idle" --from tester >/dev/null 2>&1
sleep 5
if tmux -L "$TMUXSOCK" capture-pane -t "$CSEAT" -p 2>/dev/null | grep -q "should wait for idle"; then
  bad "typed into a non-idle agent seat (state gate bypassed)"
else ok "opted-in agent that is not idle holds its mail (state gate)"; fi

# the opt-in must SURVIVE a daemon restart (persistence, not just memory)
"$COMM" com8 stop >/dev/null 2>&1; sleep 1
"$COMM" com8 start >/dev/null 2>&1; sleep 2
if python3 -c "import json,sys; d=json.load(open('$COMM_STATE/com8/identities.json')); sys.exit(0 if d.get('relaybot',{}).get('seat_relay') is True else 1)"; then
  ok "seat_relay opt-in persists across a daemon restart"
else bad "seat_relay lost on restart (persistence gap)"; fi

"$COMM" com8 stop >/dev/null 2>&1
echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
