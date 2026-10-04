#!/usr/bin/env bash
# A4: com8 spawn / fan / consult — the fabric creates agents. Tested with stub
# bash "agents" (adopt off; real-agent /rename adoption is the dogfood phase).
set -uo pipefail
command -v tmux >/dev/null 2>&1 || { echo "skip: tmux not installed"; exit 0; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-spawn.XXXXXX)"
TMUXSOCK="com8-spawn-$$"
export COMM_STATE="$T/state" COM8_SOCK_DIR="$T/socks" COM8_SESSIONS_DIR="$T/sessions"
export COM8_SELF="spawnhost" COM8_TICK=1 COM8_TMUX_SOCKET="$TMUXSOCK" COM8_SEAT_SESSION="sp"
mkdir -p "$COM8_SESSIONS_DIR"
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup(){ "$COMM" com8 stop >/dev/null 2>&1||true; tmux -L "$TMUXSOCK" kill-server >/dev/null 2>&1||true; rm -rf "$T"; }
trap cleanup EXIT
ID="$COMM_STATE/com8/identities.json"

"$COMM" com8 start >/dev/null 2>&1

echo "== spawn: claim + seat + bind in one verb"
out="$("$COMM" com8 spawn worker1 -- bash --norc --noprofile 2>/dev/null)"
if printf '%s' "$out" | grep -q 'worker1 -> %[0-9]'; then ok "spawn returned name -> seat ($out)"; else bad "spawn output ($out)"; fi
seat="${out##*-> }"
if grep -q '"worker1"' "$ID" 2>/dev/null; then ok "spawn claimed the identity"; else bad "spawn claimed identity"; fi
if grep -q "\"seat\": \"$seat\"" "$ID" 2>/dev/null; then ok "spawn bound the seat to the identity"; else bad "spawn bound seat"; fi
sleep 0.8
if [ "$("$COMM" com8 seat state "$seat" 2>/dev/null)" = "idle" ]; then ok "spawned seat is a live shell"; else bad "spawned seat live"; fi
# the identity is mailable
"$COMM" com8 send worker1 "hello worker" --from tester >/dev/null 2>&1
sleep 0.3
if "$COMM" com8 inbox worker1 2>/dev/null | grep -q 'hello worker'; then ok "spawned identity receives mail"; else bad "spawned identity mailable"; fi

echo "== fan: N agents + an addressable group"
"$COMM" com8 fan --n 3 --prefix team -- bash --norc --noprofile >/dev/null 2>&1
sleep 0.5
c=0
for nm in team-1 team-2 team-3; do grep -q "\"$nm\"" "$ID" && c=$((c+1)); done
if [ "$c" = "3" ]; then ok "fan claimed 3 identities"; else bad "fan identities ($c/3)"; fi
"$COMM" com8 group team-1,team-2,team-3 "fan hello" --from lead >/dev/null 2>&1
sleep 0.3
g=0
for nm in team-1 team-2 team-3; do "$COMM" com8 inbox "$nm" 2>/dev/null | grep -q 'fan hello' && g=$((g+1)); done
if [ "$g" = "3" ]; then ok "the fan group is addressable"; else bad "fan group ($g/3)"; fi

echo "== consult: spawn-or-reuse a private peer"
# First consult spawns consult-bash; the ask times out (stub can't answer) but
# the peer is created. Second consult REUSES it (no second seat).
"$COMM" com8 consult "ping?" --cli bash --timeout 2 --json > "$T/c1.json" 2>/dev/null
r2="$("$COMM" com8 consult "ping again?" --cli bash --timeout 2 --json 2>/dev/null)"
if grep -q '"consult-bash"' "$ID" 2>/dev/null; then ok "consult claimed a private peer identity"; else bad "consult peer identity"; fi
if python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("reused") else 1)' "$r2" 2>/dev/null; then
  ok "second consult reused the peer"; else bad "consult reuse (got: $r2)"; fi
nwin="$(tmux -L "$TMUXSOCK" list-windows -a 2>/dev/null | grep -c consult-bash || true)"
if [ "$nwin" = "1" ]; then ok "reuse spawned no second seat (1 window)"; else bad "consult seat reuse ($nwin windows)"; fi

echo "== spawn records what a restart would need"
"$COMM" com8 spawn sup1 --cwd "$PWD" -- bash --norc --noprofile >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "supervision record persisted (cmd/cwd)" || bad "supervision record"
import json,sys
d=json.load(open(sys.argv[1]))["sup1"]
s=d.get("supervision")
assert s and "bash" in s["cmd"], s
assert s["cwd"], s
assert d["workspace"] is not None, d
PY

echo "== spawn --worktree puts the AGENT in the worktree, not the shared repo"
# The defect this covers: _do_claim made <repo>-worktrees/<name> and recorded it
# as the workspace, but the seat was spawned with the ORIGINAL cwd — so four
# fanned agents each got a private branch while all four edited one checkout.
# The only honest check is the pane's OWN cwd, asked of tmux.
WTREPO="$T/wtrepo"; mkdir -p "$WTREPO"
( cd "$WTREPO" && git init -q . && git config user.email t@t && git config user.name t \
  && echo hi > f.txt && git add f.txt && git -c commit.gpgsign=false commit -qm first ) >/dev/null 2>&1
wtout="$("$COMM" com8 spawn wt1 --cwd "$WTREPO" --worktree -- bash --norc --noprofile 2>/dev/null)"
wtseat="${wtout##*-> }"
sleep 0.5
recorded="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["wt1"]["workspace"]["path"])' "$ID" 2>/dev/null)"
actual="$(tmux -L "$TMUXSOCK" display -p -t "$wtseat" '#{pane_current_path}' 2>/dev/null)"
if [ -n "$recorded" ] && [ "$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$actual")" \
     = "$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$recorded")" ]; then
  ok "spawned pane cwd == recorded workspace ($actual)"
else bad "pane cwd ($actual) != recorded workspace ($recorded)"; fi
python3 - "$ID" <<'PY' && ok "supervision.cwd is the worktree (restart lands there too)" || bad "supervision.cwd is not the worktree"
import json,sys,os
d=json.load(open(sys.argv[1]))["wt1"]
assert os.path.realpath(d["supervision"]["cwd"])==os.path.realpath(d["workspace"]["path"]), d
assert d["workspace"]["worktree"] is True, d
PY

echo "== restart brings the agent back on a new seat"
old="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sup1"]["seat"])' "$ID")"
"$COMM" com8 seat kill "$old" >/dev/null 2>&1
sleep 0.5
"$COMM" com8 restart sup1 >/dev/null 2>&1
new="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sup1"]["seat"])' "$ID")"
[ -n "$new" ] && [ "$new" != "$old" ] && ok "restart produced a new live seat ($old -> $new)" || bad "restart ($old -> $new)"

echo "== restart never kills a pane that is no longer this identity's"
# tmux restarts pane ids at %0 when the tmux server does, so a STORED pane id
# is not a fact — after a reboot the id on an identity can name a live pane
# that now belongs to somebody else. Same shape here, deterministically: bind
# k1's seat onto k2's live pane, then restart k1.
# (`tmux display -t <dead pane>` exits 0 with EMPTY output, so liveness is the
#  id coming back, never the exit status.)
pane_alive(){ [ "$(tmux -L "$TMUXSOCK" display -p -t "$1" '#{pane_id}' 2>/dev/null)" = "$1" ]; }
"$COMM" com8 spawn k1 -- bash --norc --noprofile >/dev/null 2>&1
k2out="$("$COMM" com8 spawn k2 -- bash --norc --noprofile 2>/dev/null)"
k2seat="${k2out##*-> }"
"$COMM" com8 seat bind "$k2seat" k1 >/dev/null 2>&1
"$COMM" com8 restart k1 >/dev/null 2>&1
if pane_alive "$k2seat"; then
  ok "restart left the other identity's live pane alone ($k2seat)"
else bad "restart killed $k2seat — a pane bound to another identity"; fi
python3 - "$ID" "$k2seat" <<'PY' && ok "k2 still holds its own seat" || bad "k2 lost its seat"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["k2"]["seat"]==sys.argv[2], d["k2"]
assert d["k1"]["seat"]!=sys.argv[2], d["k1"]
PY

echo "== restart retires the old seat only after the new one is bound"
r2out="$("$COMM" com8 spawn r2 -- bash --norc --noprofile 2>/dev/null)"
r2old="${r2out##*-> }"
"$COMM" com8 restart r2 >/dev/null 2>&1
r2new="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["r2"]["seat"])' "$ID")"
if [ "$r2new" != "$r2old" ] && pane_alive "$r2new" && ! pane_alive "$r2old"; then
  ok "the live old seat was retired once the new one existed ($r2old -> $r2new)"
else bad "restart seat handover ($r2old -> $r2new)"; fi
# The failed-respawn rollback lives in scripts/test-com8-unit.py: tmux
# cannot be made to fail a new-window on demand (it silently falls back to a
# default cwd when the recorded one is gone).

echo "== a successful restart refreshes spawned_at"
"$COMM" com8 spawn r1 -- bash --norc --noprofile >/dev/null 2>&1
t0="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["r1"]["supervision"]["spawned_at"])' "$ID")"
sleep 1.1
"$COMM" com8 restart r1 >/dev/null 2>&1
python3 - "$ID" "$t0" <<'PY' && ok "spawned_at moved forward on restart" || bad "spawned_at is stale after restart"
import json,sys
t=json.load(open(sys.argv[1]))["r1"]["supervision"]["spawned_at"]
assert t > float(sys.argv[2]), (t, sys.argv[2])
PY

"$COMM" com8 stop >/dev/null 2>&1
echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
