#!/usr/bin/env bash
# Deterministic test of `communicate ask` + in-band coaching. No network, no
# real codex: cooperating cc_peer stubs and a fake codex CLI.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLI="$ROOT/bin/communicate"
PY="$ROOT/lib/cc_peer.py"
SOCKDIR="${XDG_RUNTIME_DIR:-/tmp}/cc-socks"; mkdir -p "$SOCKDIR"; chmod 700 "$SOCKDIR" 2>/dev/null || true
STATE="$(mktemp -d)"
SESS="$STATE/sessions"; mkdir -p "$SESS"
fails=0; ok(){ echo "  ok  $*"; }; fail(){ echo "  FAIL $*"; fails=$((fails+1)); }
cleanup(){ jobs -p | xargs kill 2>/dev/null; rm -rf "$STATE"; rm -f "$SOCKDIR"/test-ask-*.sock; }
trap cleanup EXIT
# Sandbox the sessions dir so we neither read nor pollute the real bus.
export CLAUDE_CONFIG_DIR="$STATE"
# Sections 1-5 test the original behaviour; sections 6-8 opt in to the grace period.
export COMM_ASK_GRACE=0
# Late-reply files (and everything else under COMM_STATE) stay out of the real state dir.
export COMM_STATE="$STATE/state"

plant() { # <name> <socket> -> sidecar with live pid, numeric filename
  python3 - "$SESS" "$1" "$2" <<'P'
import json, os, sys, time, zlib
d, name, sock = sys.argv[1:4]
n = 3000000 + zlib.crc32(name.encode()) % 900000
json.dump({"pid": os.getppid(), "sessionId": "t", "cwd": "-", "startedAt": int(time.time()*1000),
           "version": "test", "peerProtocol": 1, "kind": "interactive", "entrypoint": "test",
           "messagingSocketPath": sock, "name": name, "status": "idle"},
          open(os.path.join(d, "%d.json" % n), "w"))
P
}

echo "1) claude-lane round trip: coached ask -> auto-replying peer -> unwrapped answer"
PEER="$SOCKDIR/test-ask-peer-$$.sock"
plant "test-target-$$" "$PEER"
python3 "$PY" respond --socket "$PEER" --reply "ANSWER-42" --name test-peer --timeout 30 >/dev/null 2>&1 &
for i in $(seq 1 20); do [ -S "$PEER" ] && break; sleep 0.2; done
OUT="$("$CLI" ask "test-target-$$" --timeout 20 "what is the answer" 2>/dev/null)"
case "$OUT" in
  "<cross-session-message"*) fail "reply not unwrapped: $OUT";;
  ANSWER-42*) ok "reply received and unwrapped cleanly (respond echoes the coached question back)";;
  *) fail "got: '$OUT'";;
esac

echo "2) coaching content reaches the receiver"
SINK="$SOCKDIR/test-ask-sink-$$.sock"; GOT="$STATE/got.txt"
plant "test-sink-$$" "$SINK"
python3 "$PY" recv --socket "$SINK" --timeout 15 >"$GOT" 2>/dev/null &
for i in $(seq 1 20); do [ -S "$SINK" ] && break; sleep 0.2; done
"$CLI" ask "test-sink-$$" --timeout 2 --as coach-check "ping" >/dev/null 2>&1
grep -q "\[reply-to coach-check uds:" "$GOT" && grep -q 'communicate send' "$GOT" && grep -q "SendMessage" "$GOT" \
  && ok "[reply-to] block teaches both reply methods" || { fail "coaching missing: $(cat "$GOT")"; }
grep -q "answer in place" "$GOT" && ok "escape clause present" || fail "no escape clause"

echo "3) codex-lane: name resolved from session index, coached, full reply loop"
IDX="$STATE/index.jsonl"; CLOG="$STATE/codex.log"
printf '{"id":"thread-123","thread_name":"cq-target"}\n' > "$IDX"
mkdir -p "$STATE/bin"
cat > "$STATE/bin/codex" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$@" >> "$CLOG"
[ "\${1:-}" = "--version" ] && echo "codex-cli 0.151.0-stub"
exit 0
STUB
chmod +x "$STATE/bin/codex"
( sleep 2
  SOCK="$(grep -o 'uds:[^ ]*\.sock' "$CLOG" | head -1 | cut -c5-)"
  [ -S "$SOCK" ] && python3 "$PY" send --to "$SOCK" --text "CODEX-ANSWER" --from /tmp/x.sock --name fake-codex ) &
OUT="$(COMM_CODEX_PATH="$STATE/bin" COMM_CODEX_INDEX="$IDX" "$CLI" ask cq-target --timeout 15 "hello codex" 2>/dev/null)"
[ "$OUT" = "CODEX-ANSWER" ] && ok "codex-session ask round trip" || fail "got: '$OUT'"
grep -q -- "--thread=thread-123" "$CLOG" && ok "queued to the right thread" || fail "thread wrong: $(cat "$CLOG")"
grep -q "\[reply-to" "$CLOG" && ok "queued message carries coaching" || fail "no coaching in queue payload"

echo "4) timeout is honest (exit 2) and unknown names die"
QUIET="$SOCKDIR/test-ask-quiet-$$.sock"
plant "test-quiet-$$" "$QUIET"
python3 "$PY" mailbox --socket "$QUIET" --name quiet --sessions-dir "$SESS" --mailbox "$STATE/quiet.jsonl" >/dev/null 2>&1 &
for i in $(seq 1 20); do [ -S "$QUIET" ] && break; sleep 0.2; done
"$CLI" ask "test-quiet-$$" --timeout 2 "anyone" >/dev/null 2>&1; [ $? -eq 2 ] && ok "timeout -> exit 2" || fail "timeout exit code"
"$CLI" ask "no-such-agent-$$" --timeout 1 "x" >/dev/null 2>&1; [ $? -eq 1 ] && ok "unknown name -> die" || fail "unknown-name exit code"

echo "5) route --coach and send --from compose"
SINK2="$SOCKDIR/test-ask-sink2-$$.sock"; GOT2="$STATE/got2.txt"
plant "test-sink2-$$" "$SINK2"
python3 "$PY" recv --socket "$SINK2" --timeout 10 >"$GOT2" 2>/dev/null &
for i in $(seq 1 20); do [ -S "$SINK2" ] && break; sleep 0.2; done
"$CLI" route "test-sink2-$$" --coach "coached broadcast" >/dev/null 2>&1
wait %% 2>/dev/null
grep -q "coached broadcast" "$GOT2" && grep -q "\[reply-to router" "$GOT2" && ok "route --coach appends coaching" || fail "route --coach: $(cat "$GOT2")"

echo "6) a reply that misses the timeout is kept, not refused"
LATE="$SOCKDIR/test-ask-late-$$.sock"; LBOX="$STATE/late-target.jsonl"; LERR="$STATE/late.err"
plant "test-late-$$" "$LATE"
python3 "$PY" mailbox --socket "$LATE" --name late --sessions-dir "$SESS" --mailbox "$LBOX" >/dev/null 2>&1 &
for i in $(seq 1 20); do [ -S "$LATE" ] && break; sleep 0.2; done
COMM_ASK_GRACE=20 "$CLI" ask "test-late-$$" --timeout 2 --as late-asker "slow question" >/dev/null 2>"$LERR"
[ $? -eq 2 ] && ok "late ask still times out honestly (exit 2)" || fail "late ask exit code"
RSOCK="$(grep -o 'uds:[^ ]*\.sock' "$LBOX" | head -1 | cut -c5-)"
LFILE="$(grep -o '/[^ ]*ask-late/[^ ]*' "$LERR" | head -1)"
[ -n "$LFILE" ] && ok "timeout message names where a late answer will land" || fail "no late path in: $(cat "$LERR")"
python3 "$PY" send --to "$RSOCK" --text "LATE-ANSWER" --from /tmp/x.sock --name slow-peer >/dev/null 2>&1 \
  && ok "late reply is delivered (the reply socket is still listening)" || fail "late reply refused (socket gone)"
for i in $(seq 1 20); do [ -s "$LFILE" ] && break; sleep 0.2; done
grep -q "LATE-ANSWER" "$LFILE" 2>/dev/null && ok "late answer saved to $LFILE" || fail "late answer not saved"
for i in $(seq 1 20); do [ -S "$RSOCK" ] || break; sleep 0.2; done
[ -S "$RSOCK" ] && fail "late listener still bound after its one reply" || ok "late listener exits after one reply"
NOGRACE_ERR="$STATE/nograce.err"
COMM_ASK_GRACE=0 "$CLI" ask "test-late-$$" --timeout 1 --as nograce "q" >/dev/null 2>"$NOGRACE_ERR"
grep -q 'ask-late' "$NOGRACE_ERR" && fail "COMM_ASK_GRACE=0 still promised a late file" || ok "COMM_ASK_GRACE=0 keeps the old behaviour"

echo "7) the grace listener is detached, and --timeout is honoured as given"
T0=$SECONDS
CAP="$(COMM_ASK_GRACE=5 "$CLI" ask "test-late-$$" --timeout 1 --as "cap/tured" "q" 2>"$STATE/cap.err")"; RC=$?
[ "$RC" -eq 2 ] && [ -z "$CAP" ] && [ $((SECONDS - T0)) -lt 4 ] \
  && ok "\$(ask ...) returns at the timeout, not after the grace period" || fail "captured ask: rc=$RC after $((SECONDS - T0))s, out='$CAP'"
grep -q 'ask-late/cap_tured-' "$STATE/cap.err" && ok "late file name is safe for any --as name" || fail "late path: $(cat "$STATE/cap.err")"
# A peer that answers 0.2 s after the question lands: inside a 0.9 s timeout,
# but too late for a wait that truncates 0.9 to 0. It starts first, so python's
# start-up time is not part of the 0.2 s.
python3 - "$ROOT/lib" "$LBOX" >/dev/null 2>&1 <<'P' &
import re, sys, time
sys.path.insert(0, sys.argv[1]); import cc_peer
for _ in range(500):
    m = re.search(r"reply-to decimal-check uds:(\S+?\.sock)", open(sys.argv[2]).read())
    if m: break
    time.sleep(0.01)
time.sleep(0.2)
cc_peer.deliver(m.group(1), "SLOW-42", "/tmp/x.sock", from_name="slow-peer")
P
OUT="$(COMM_ASK_GRACE=5 "$CLI" ask "test-late-$$" --timeout 0.9 --as decimal-check "slow one" 2>/dev/null)"
case "$OUT" in SLOW-42*) ok "a decimal --timeout waits its full length";; *) fail "decimal timeout got: '$OUT'";; esac
ERR="$(COMM_ASK_GRACE=5 "$CLI" ask "test-late-$$" --timeout 1.5.2 "x" 2>&1 >/dev/null)"; RC=$?
[ "$RC" -eq 1 ] && grep -q -- '--timeout takes seconds' <<<"$ERR" && ok "a malformed --timeout dies" || fail "malformed timeout: rc=$RC, $ERR"

echo "8) the late window is on by default, stopped on failure, and promised honestly"
( unset COMM_ASK_GRACE; "$CLI" ask "test-late-$$" --timeout 1 --as default-grace "q" >/dev/null 2>"$STATE/default.err" )
LEFT="$(grep -o 'next [0-9]*s' "$STATE/default.err" | tr -dc 0-9)"
grep -q 'ask-late/default-grace-' "$STATE/default.err" && [ "${LEFT:-0}" -ge 595 ] && [ "$LEFT" -le 600 ] \
  && ok "the grace period is on by default, for 600s" || fail "default grace: $(cat "$STATE/default.err")"
# One reply ends that listener now rather than ten minutes from now.
SOCK="$("$CLI" whereis default-grace 2>/dev/null | cut -f3)"
[ -n "$SOCK" ] && python3 "$PY" send --to "$SOCK" --text "done" --from /tmp/x.sock --name t >/dev/null 2>&1
plant "test-gone-$$" "$SOCKDIR/test-ask-gone-$$.sock"   # nothing listens there
COMM_ASK_GRACE=5 "$CLI" ask "test-gone-$$" --timeout 1 --as stop-check "x" >/dev/null 2>&1; RC=$?
SOCK="$("$CLI" whereis stop-check 2>/dev/null | cut -f3)"
if [ "$RC" -eq 1 ] && ! { [ -n "$SOCK" ] && python3 "$PY" send --to "$SOCK" --text "x" --from /tmp/x.sock --name t >/dev/null 2>&1; }; then
  ok "a failed delivery stops the grace listener"
else
  fail "failed delivery: rc=$RC, a listener still answers at '$SOCK'"
fi
[ -n "$SOCK" ] && rm -f "$SOCK"   # the stopped listener leaves its socket file behind
mkdir -p "$STATE/slowbin"
cat > "$STATE/slowbin/codex" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" = "--version" ] && { echo "codex-cli 0.151.0-stub"; exit 0; }
sleep 5
STUB
chmod +x "$STATE/slowbin/codex"
T0=$SECONDS
COMM_ASK_GRACE=20 COMM_CODEX_PATH="$STATE/slowbin" COMM_CODEX_INDEX="$IDX" \
  "$CLI" ask cq-target --timeout 3 --as slow-lane "q" >/dev/null 2>"$STATE/slow.err"
DT=$((SECONDS - T0))
[ "$DT" -lt 8 ] && ok "a slow delivery uses up the timeout instead of extending it" || fail "a 5 s delivery with --timeout 3 took ${DT}s"
LEFT="$(grep -o 'next [0-9]*s' "$STATE/slow.err" | tr -dc 0-9)"
[ "${LEFT:-99}" -ge 10 ] && [ "${LEFT:-99}" -le 18 ] \
  && ok "the promised late window allows for a slow delivery" || fail "promised ${LEFT:-no}s of 23 after a 5 s delivery: $(cat "$STATE/slow.err")"
SOCK="$("$CLI" whereis slow-lane 2>/dev/null | cut -f3)"
[ -n "$SOCK" ] && python3 "$PY" send --to "$SOCK" --text "done" --from /tmp/x.sock --name t >/dev/null 2>&1

[ "$fails" -eq 0 ] && echo "PASS: ask + in-band coaching, both lanes" || { echo "FAIL: $fails"; exit 1; }
