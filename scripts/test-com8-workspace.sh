#!/usr/bin/env bash
# Workspace axis: recorded always (path + git ref + branch), worktree opt-in.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-ws.XXXXXX)"
export COMM_STATE="$T/state" COM8_SOCK_DIR="$T/socks" COM8_SESSIONS_DIR="$T/sess"
export COM8_SELF=wshost COM8_TICK=1
mkdir -p "$COM8_SESSIONS_DIR"
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup(){ "$COMM" com8 stop >/dev/null 2>&1||true; rm -rf "$T"; }
trap cleanup EXIT
ID="$COMM_STATE/com8/identities.json"

# a real git repo to point at
REPO="$T/repo"; mkdir -p "$REPO"; cd "$REPO"
git init -q .; git config user.email t@t; git config user.name t
echo hello > README.md; git add README.md
git -c commit.gpgsign=false commit -qm "first"
SHA="$(git rev-parse --short HEAD)"; BR="$(git rev-parse --abbrev-ref HEAD)"
cd "$HERE"

"$COMM" com8 start >/dev/null 2>&1

echo "== a git workspace is recorded with ref + branch"
"$COMM" com8 claim ws1 --cwd "$REPO" >/dev/null 2>&1
python3 - "$ID" "$REPO" "$SHA" "$BR" <<'PY' && ok "git workspace recorded (path/ref/branch)" || bad "git workspace record"
import json,sys,os
d=json.load(open(sys.argv[1]))["ws1"]["workspace"]
assert d and os.path.realpath(d["path"])==os.path.realpath(sys.argv[2]), d
assert d["ref"].startswith(sys.argv[3]), d
assert d["branch"]==sys.argv[4], d
assert d["worktree"] is False, d
PY

echo "== a non-git directory still records its path (no ref)"
mkdir -p "$T/plain"
"$COMM" com8 claim ws2 --cwd "$T/plain" >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "non-git workspace records path, ref=None" || bad "non-git workspace"
import json,sys
d=json.load(open(sys.argv[1]))["ws2"]["workspace"]
assert d["path"].endswith("/plain"), d
assert d["ref"] is None and d["branch"] is None, d
PY

echo "== no --cwd means no workspace (never invented)"
"$COMM" com8 claim ws3 >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "workspace stays null when not given" || bad "workspace invented"
import json,sys
assert json.load(open(sys.argv[1]))["ws3"]["workspace"] is None
PY

echo "== --worktree creates a real worktree on its own branch"
"$COMM" com8 claim ws4 --cwd "$REPO" --worktree >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "worktree recorded with worktree:true" || bad "worktree record"
import json,sys,os
d=json.load(open(sys.argv[1]))["ws4"]["workspace"]
assert d["worktree"] is True, d
assert d["branch"]=="com8/ws4", d
assert os.path.isdir(d["path"]), d
PY
( cd "$REPO" && git worktree list | grep -q 'com8/ws4' ) && ok "git agrees the worktree exists" || bad "git worktree list"

echo "== a second agent on the same repo gets its own worktree"
"$COMM" com8 claim ws5 --cwd "$REPO" --worktree >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "two worktree agents do not collide" || bad "worktree collision"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["ws4"]["workspace"]["path"]!=d["ws5"]["workspace"]["path"]
assert d["ws5"]["workspace"]["branch"]=="com8/ws5"
PY

echo "== a removed worktree with its branch intact is re-adopted, not orphaned"
"$COMM" com8 claim ws6 --cwd "$REPO" --worktree >/dev/null 2>&1
WS6_PATH="$(python3 - "$ID" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["ws6"]["workspace"]["path"])
PY
)"
"$COMM" com8 release ws6 >/dev/null 2>&1
# `git worktree remove` is the normal recovery path -- it deliberately leaves
# the branch (com8/ws6) behind, only dropping the worktree's directory + the
# administrative link back to it.
( cd "$REPO" && git worktree remove --force "$WS6_PATH" ) >/dev/null 2>&1
( cd "$REPO" && git worktree list | grep -q 'com8/ws6' ) && bad "worktree remove didn't remove it" || ok "worktree entry removed, branch left behind"
"$COMM" com8 claim ws6 --cwd "$REPO" --worktree >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "re-claim adopts com8/ws6 (branch survives remove + re-claim)" || bad "re-claim orphaned the original branch"
import json,sys,os
d=json.load(open(sys.argv[1]))["ws6"]["workspace"]
assert d["worktree"] is True, d
assert d["branch"]=="com8/ws6", d
assert os.path.isdir(d["path"]), d
PY
( cd "$REPO" && git branch --list ws6 | grep -q . ) && bad "stray branch 'ws6' was created" || ok "no stray branch named ws6"
( cd "$REPO" && git worktree list | grep -q 'com8/ws6' ) && ok "git agrees com8/ws6 worktree exists again" || bad "worktree not recreated"

echo "== re-claiming an already-claimed name must not litter a second repo"
REPO2="$T/repo2"; mkdir -p "$REPO2"
( cd "$REPO2" && git init -q . && git config user.email t@t && git config user.name t \
  && echo hi > f.txt && git add f.txt && git -c commit.gpgsign=false commit -qm "first" ) >/dev/null 2>&1
"$COMM" com8 claim ws7 --cwd "$REPO" --worktree >/dev/null 2>&1
WS7_BEFORE="$(python3 -c "import json;print(json.load(open('$ID'))['ws7']['workspace']['path'])")"
# ws7 is already claimed; a repeat --worktree claim against a DIFFERENT repo
# must be a true no-op -- no branch, no worktree directory, no change to the
# already-recorded workspace (before the fix this ran `git worktree add`
# against REPO2 and then discarded the result on the "already claimed" path).
"$COMM" com8 claim ws7 --cwd "$REPO2" --worktree >/dev/null 2>&1
python3 - "$ID" "$WS7_BEFORE" <<'PY' && ok "re-claim against a second repo left the recorded workspace untouched" || bad "re-claim overwrote workspace"
import json,sys
d=json.load(open(sys.argv[1]))["ws7"]["workspace"]
assert d["path"]==sys.argv[2], d
assert d["worktree"] is True, d
assert d["branch"]=="com8/ws7", d
PY
( cd "$REPO2" && git branch --list 'com8/ws7' | grep -q . ) && bad "re-claim created a stray com8/ws7 branch in the SECOND repo" || ok "no stray branch in the second repo"
[ -d "$T/repo2-worktrees/ws7" ] && bad "re-claim created a stray worktree dir in the SECOND repo" || ok "no stray worktree directory in the second repo"

echo "== an identity claimed WITHOUT a cwd can still be given one later"
# The freeze this covers: _do_claim is auto-invoked with no cwd on _do_ask and
# on inbound mail, so any identity that got mail before its agent claimed it
# was stuck at workspace:null forever — the later `claim --cwd` printed
# "claimed" and did nothing, which also silently disarmed the move gate (it
# skips whenever ws_path is falsy).
# `com8 ask --from ws8` auto-claims ws8 (the asker must have a home for the
# reply) — with no cwd, exactly as inbound mail does.
"$COMM" com8 ask ws1 "are you there?" --from ws8 --timeout 1 >/dev/null 2>&1
python3 - "$ID" <<'PY' && ok "an auto-claimed identity starts with no workspace" || bad "auto-claim workspace"
import json,sys
d=json.load(open(sys.argv[1]))
assert "ws8" in d, sorted(d)
assert d["ws8"]["workspace"] is None, d["ws8"]
PY
OUT="$("$COMM" com8 claim ws8 --cwd "$REPO" 2>&1)"
python3 - "$ID" "$REPO" <<'PY' && ok "claim --cwd on an already-claimed name records the workspace" || bad "already-claimed workspace never recorded"
import json,os,sys
d=json.load(open(sys.argv[1]))["ws8"]["workspace"]
assert d and os.path.realpath(d["path"])==os.path.realpath(sys.argv[2]), d
PY
printf '%s' "$OUT" | grep -qi 'workspace' && ok "the CLI says what it actually did ($OUT)" || bad "CLI reported a bare success ($OUT)"
# A SECOND cwd must not re-point a live agent's world.
"$COMM" com8 claim ws8 --cwd "$REPO2" >/dev/null 2>&1
python3 - "$ID" "$REPO" <<'PY' && ok "a later claim never re-points an existing workspace" || bad "workspace re-pointed"
import json,os,sys
d=json.load(open(sys.argv[1]))["ws8"]["workspace"]
assert os.path.realpath(d["path"])==os.path.realpath(sys.argv[2]), d
PY
OUT="$("$COMM" com8 claim ws8 --cwd "$REPO2" 2>&1)"
printf '%s' "$OUT" | grep -qi 'already' && ok "a true no-op claim says so instead of 'claimed'" || bad "no-op claim wording ($OUT)"

"$COMM" com8 stop >/dev/null 2>&1
echo; echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
