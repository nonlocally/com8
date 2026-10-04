#!/usr/bin/env bash
# anu-account — the subscription a Claude Code pane runs on. Runs the REAL bin
# against stubs for curl (the dashboard), anu-secrets, ssh, tmux, claude.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$here/../lib/harness.sh"
source "$here/../lib/helpers.bash"

t_suite "anu-account"
ACCOUNT="$ANU_ROOT/profiles/runtime/accounts/account"
assert_file "$ACCOUNT" "anu-account bin present"

SD="$(new_stubdir)"; export PATH="$SD:$PATH"
export COM8_ACCOUNT_PANE="$SD/pane"
LOG="$(mktmp)/calls.log"; : > "$LOG"
FIX="$(mktmp)"
export ANU_USAGE_URL="http://dash.test"
export ANU_USAGE_HOST="usage-host" ANU_USAGE_REPO="~/usage-service"
export ANU_SECRETS_BIN="$SD/anu-secrets"
# Hermetic regardless of whether THIS suite happens to run inside a real
# tmux pane: _lock_pane's socket-name scoping reads $TMUX, so an ambient
# value here would make the lock dir name (and this file's own "nosocket-4"
# fixtures below) depend on the environment running the tests. Individual
# tests (e.g. "status") still set $TMUX explicitly where they need it.
unset TMUX
# Same for the launcher's own exports: a pane started by `@ACCOUNT_LAUNCH@ launch`
# carries ANU_ACCOUNT/ANU_PANE, and `launch` treats an inherited ANU_ACCOUNT
# as "already resolved" — so the keychain-fallback cases would silently pick
# the suite runner's own account (seen live: 9 failures under a rotated pane).
unset ANU_ACCOUNT ANU_PROVIDER ANU_PANE
# The real jq binary, captured before anything stubs over it — used to build
# a logging "spy" wrapper for one test below (real behavior, args recorded).
REAL_JQ="$(command -v jq)"
# The real sleep, likewise captured before the suite stubs it out — one test
# below needs a stub that ACTUALLY blocks (to exercise the bounded token
# lookup), and the stubbed `sleep` returns instantly by design.
REAL_SLEEP="$(command -v sleep)"
# A temp default cache for every case that doesn't set its own — so no
# invocation in this whole suite ever touches the real per-device cache at
# ~/.local/state/com8/accounts/tokens.json.
export ANU_ACCOUNT_CACHE="$(mktmp)/tokens.json"
# Same isolation for the mkdir-based pane lock — so no invocation in this
# suite ever touches the real per-device lock dir at
# ~/.local/state/com8/accounts/locks.
export ANU_ACCOUNT_LOCKDIR="$(mktmp)/locks"
# The exact continuation line _relaunch_and_resume sends once a relaunch is
# confirmed idle — kept as one constant so every assertion below reflects
# the real string, not a stale copy of it.
CONT_TEXT="You were resumed on another account after a usage wall. Check the last tool results and the current state of the files before repeating any action, then continue where you left off."

cat > "$FIX/pick-fable.json" <<'JSON'
{"need":"fable","picks":[{"id":"claude-account_alpha","name":"account_alpha","email":"alpha@example.invalid","resetsAt":"2026-09-14T02:59:59Z","remaining":59,"shared":false},
 {"id":"claude-main","name":null,"email":"owner@example.invalid","resetsAt":"2026-09-14T05:59:59Z","remaining":4,"shared":false}],
 "out":[{"id":"claude-account_beta","why":"fable 100"}],"generatedAt":"2026-09-14T00:00:00Z"}
JSON
cat > "$FIX/pick-empty.json" <<'JSON'
{"need":"any","picks":[],"out":[{"id":"claude-account_alpha","why":"week_all 100"}],"generatedAt":"2026-09-14T00:00:00Z"}
JSON
cat > "$FIX/usage.json" <<'JSON'
{"accounts":[
 {"id":"claude-account_alpha","provider":"claude","label":"Claude — account_alpha","name":"account_alpha","shared":false,"email":"alpha@example.invalid","plan":"Max 20×","error":null,
  "windows":[{"key":"session-0","label":"Session · 5h","usedPercent":0,"resetsAt":null},{"key":"weekly_all-1","label":"Week · all models","usedPercent":21,"resetsAt":"2026-09-14T02:59:59Z"},{"key":"weekly_scoped-2","label":"Week · Fable","usedPercent":41,"resetsAt":"2026-09-14T02:59:59Z"}]},
 {"id":"claude-account_beta","provider":"claude","label":"Claude — account_beta","name":"account_beta","shared":true,"email":"beta@example.invalid","plan":"Max","error":"token dead","windows":[]},
 {"id":"codex-main","provider":"openai","label":"ChatGPT","name":null,"shared":false,"email":"c@x","plan":"Pro","error":null,"windows":[]}
],"generatedAt":"2026-09-14T00:00:00Z"}
JSON

# curl stub: serves fixtures by URL, records the URL, honours CURL_FAIL
stub "$SD" curl '
url="${@: -1}"; printf "curl %s\n" "$url" >> "'"$LOG"'"
[ -n "${CURL_FAIL:-}" ] && exit 7
case "$url" in
  *"/api/usage/pick?need=fable"*) cat "'"$FIX"'/pick-fable.json" ;;
  *"/api/usage/pick?need=any"*)   cat "'"$FIX"'/pick-empty.json" ;;
  *"/api/usage")                  cat "'"$FIX"'/usage.json" ;;
  *) exit 22 ;;
esac'
# anu-secrets stub: get prints a token, set records stdin length, env prints
# json. get resolves ACCOUNT_ALPHA (the usual pick winner) and ACCOUNT_BETA (the usual
# "current account" placeholder — a pane can only be running under an
# account whose token once resolved) — GMAIL deliberately has no local
# token, so switch --as gmail exercises the preflight refusal (B2.5).
stub "$SD" anu-secrets '
printf "anu-secrets %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  get)
    case "$3" in
      ACCOUNT_ALPHA) echo "sk-ant-oat01-account_alpha"; exit 0 ;;
      ACCOUNT_BETA)     echo "sk-ant-oat01-account_beta"; exit 0 ;;
      *) exit 1 ;;
    esac ;;
  set)   v="$(cat)"; printf "set-len %s\n" "${#v}" >> "'"$LOG"'"; echo "$2/$3 set" ;;
  mkdir) echo "ok $2" ;;
  env)   echo "{\"ACCOUNT_ALPHA\":\"sk-ant-oat01-account_alpha\",\"ACCOUNT_GAMMA\":\"sk-ant-oat01-account_gamma\"}" ;;
  *) exit 1 ;;
esac'
# ssh stub: logs every call, and routes stdin to a file per call kind — the
# tokens-json sync and the accounts.json register are two DIFFERENT ssh calls
# (see "add + sync"), so each needs its own capture file.
install_ssh_stub() {
  stub "$SD" ssh '
printf "ssh %s\n" "$*" >> "'"$LOG"'"
case "$*" in
  *accounts.json*) cat > "'"$FIX"'/ssh-stdin-register" ;;
  *) cat > "'"$FIX"'/ssh-stdin" ;;
esac'
}
install_ssh_stub

t_section "pick"
out="$("$ACCOUNT" pick --need fable)"; assert_ok $? "pick exits 0"
assert_eq "account_alpha" "$out" "pick prints only named (token-backed) picks, winner first"
assert_contains "$(cat "$LOG")" "curl http://dash.test/api/usage/pick?need=fable&fresh=1" "pick asks for a fresh poll by default"
out="$("$ACCOUNT" pick --need fable --exclude account_alpha --stale)"
assert_contains "$(tail -1 "$LOG")" "fresh=0&exclude=account_alpha" "--exclude and --stale reach the query"
out="$("$ACCOUNT" pick --need any)"; assert_ok $? "an empty pick still exits 0"
assert_eq "" "$out" "an empty pick prints nothing"
out="$("$ACCOUNT" pick --need fable --json)"; assert_contains "$out" '"why":"fable 100"' "--json passes the result through"
out="$("$ACCOUNT" pick --need opus 2>&1)"; assert_fail $? "--need must be any or fable"
out="$(CURL_FAIL=1 "$ACCOUNT" pick 2>&1)"; assert_fail $? "unreachable dashboard fails pick"
assert_contains "$out" "unreachable" "…and says so"

t_section "ls"
out="$("$ACCOUNT" ls)"; assert_ok $? "ls exits 0"
assert_contains "$out" "account_alpha" "ls lists claude accounts"
assert_contains "$out" "token dead" "ls shows the error column"
assert_contains "$out" "shared" "ls marks shared accounts"
assert_not_contains "$out" "ChatGPT" "ls is claude-only"

t_section "token"
out="$("$ACCOUNT" token account_alpha)"; assert_eq "sk-ant-oat01-account_alpha" "$out" "token prints the secret"
assert_contains "$(cat "$LOG")" "anu-secrets get /anu/agents/claude/accounts ACCOUNT_ALPHA --show" "token reads the uppercased key at the accounts path"
out="$("$ACCOUNT" token nope 2>&1)"; assert_fail $? "unknown account fails"

t_section "add + sync"
out="$(printf 'sk-ant-oat01-new\n' | "$ACCOUNT" add my-acct 2>/dev/null)"; assert_ok $? "add exits 0"
assert_contains "$(cat "$LOG")" "anu-secrets mkdir /anu/agents/claude/accounts" "add ensures the accounts folder exists before storing a token"
assert_contains "$(cat "$LOG")" "anu-secrets set /anu/agents/claude/accounts MY_ACCT" "add stores under the normalised key"
mkdir_line="$(grep -n '^anu-secrets mkdir /anu/agents/claude/accounts$' "$LOG" | head -1 | cut -d: -f1)"
set_line="$(grep -n '^anu-secrets set /anu/agents/claude/accounts MY_ACCT$' "$LOG" | head -1 | cut -d: -f1)"
[ -n "$mkdir_line" ] && [ -n "$set_line" ] && [ "$mkdir_line" -lt "$set_line" ]
assert_ok $? "…and mkdir runs before set"
assert_contains "$(cat "$LOG")" "set-len 16" "the token travels on stdin, not argv"
assert_not_contains "$(cat "$LOG")" "sk-ant-oat01-new" "the token never appears in argv"
assert_contains "$(cat "$LOG")" "ssh usage-host" "add syncs to the serving host"
assert_contains "$(cat "$FIX/ssh-stdin")" '"ACCOUNT_GAMMA"' "sync ships every token in the path"
assert_contains "$out" "registered claude-my-acct on usage-host" "add registers on the serving host by default, and says so"
assert_contains "$out" "add-claude -- my-acct" "…and points at the host-side command to add a poll grant, since the token alone can't read limits"
ssh_calls="$(grep -c '^ssh usage-host' "$LOG")"
assert_eq "2" "$ssh_calls" "sync and register are two separate calls to the serving host"
reg="$(cat "$FIX/ssh-stdin-register")"
assert_contains "$reg" '"id":"claude-my-acct"' "register ships the account id"
assert_contains "$reg" '"name":"my-acct"' "…the name"
assert_contains "$reg" '"key":"MY_ACCT"' "…the token key"
assert_not_contains "$reg" '"shared"' "…no --shared given, so shared is left for the host-side default (false for a new entry — see the register-local tests below)"
assert_contains "$reg" '"default_label":"Claude — my-acct"' "…and the default label, used only if the host has no entry yet"
assert_not_contains "$reg" '"label"' "…no --label given, so no label is forced into the host-side merge"
# the register call's remote command is multi-line, so it spans several lines
# of $LOG (each "ssh ..." log entry is one printf of the whole $* — grepping
# a single line would only catch the "ssh <host> set -e" line); check the
# log as a whole instead.
assert_contains "$(cat "$LOG")" "jq" "the register command runs jq on the host"
assert_contains "$(cat "$LOG")" "accounts.json" "…against accounts.json"

: > "$LOG"
out="$(printf 'sk-ant-oat01-lbl\n' | "$ACCOUNT" add labeled --label "Claude — work" --shared --email work@example.com 2>/dev/null)"
assert_ok $? "add --label --shared --email exits 0"
reg="$(cat "$FIX/ssh-stdin-register")"
assert_contains "$reg" '"label":"Claude — work"' "--label reaches the register payload"
assert_contains "$reg" '"shared":true' "--shared reaches the register payload"
assert_contains "$reg" '"email":"work@example.com"' "--email reaches the register payload"
assert_contains "$out" "registered claude-labeled on usage-host" "…and add reports the registration"

: > "$LOG"; rm -f "$FIX/ssh-stdin-register"
out="$(printf 'sk-ant-oat01-nr\n' | "$ACCOUNT" add noreg --no-register 2>/dev/null)"
assert_ok $? "add --no-register exits 0"
ssh_calls="$(grep -c '^ssh usage-host' "$LOG")"
assert_eq "1" "$ssh_calls" "--no-register skips the register call (sync still runs)"
assert_not_contains "$(cat "$LOG")" "accounts.json" "…no accounts.json touched at all"
assert_contains "$out" '"id": "claude-noreg"' "--no-register prints the entry instead"
assert_contains "$out" '"key": "NOREG"' "…with the token key"
assert_contains "$out" '"label": "Claude — noreg"' "…and the default label, for pasting by hand"
assert_not_contains "$out" "add-claude" "…and the limits/add-claude hint is printed only on the registered path, not here"

# malformed host accounts.json: simulate the host-side guard refusing (real
# guard lives in the jq program the register step ships — see the "jq"/
# "accounts.json" assertions above) by making the register ssh call fail.
: > "$LOG"
stub "$SD" ssh '
printf "ssh %s\n" "$*" >> "'"$LOG"'"
case "$*" in
  *accounts.json*) echo "anu account: accounts.json is not valid JSON — refusing to write" >&2; exit 1 ;;
  *) cat > "'"$FIX"'/ssh-stdin" ;;
esac'
out="$(printf 'sk-ant-oat01-bad\n' | "$ACCOUNT" add badhost 2>&1)"; rc=$?
assert_fail $rc "add fails when the register step fails (e.g. a malformed host accounts.json)"
assert_contains "$(cat "$LOG")" "anu-secrets set /anu/agents/claude/accounts BADHOST" "…but the token was already stored before the failure"
assert_contains "$(cat "$LOG")" "ssh usage-host" "…sync to the host still ran"
# the failure message itself must say so — not just the log the test can see
# but what a human running this for real would see on their screen ($out).
assert_contains "$out" "claude-badhost is stored and synced, but registering it on usage-host failed" \
  "…and the printed message says the token IS stored and synced, not just 'failed'"
assert_contains "$out" 're-run `com8-account add badhost`' "…with a concrete remedy: re-run add once the host is fixed"
assert_contains "$out" "accounts.json by hand" "…or paste the printed entry in by hand"
assert_contains "$out" '"id": "claude-badhost"' "…and the entry itself is printed, for pasting"
install_ssh_stub

"$ACCOUNT" sync other-host >/dev/null
sync_line="$(tail -1 "$LOG")"
assert_contains "$sync_line" "ssh other-host" "sync takes a device"
assert_contains "$sync_line" "umask 077" "…the remote write is umask 077"
assert_contains "$sync_line" ".local/state" "…and warms THAT device's own launcher cache under its XDG_STATE_HOME (or \$HOME/.local/state)…"
assert_contains "$sync_line" "com8/accounts/tokens.json" "…at the launcher cache path, not the old dashboard mirror"
assert_contains "$sync_line" "mv -f" "…atomic tmp+mv on the remote"

local_cache="$(mktmp)/tokens.json"
ANU_ACCOUNT_CACHE="$local_cache" "$ACCOUNT" sync local >/dev/null
assert_file "$local_cache" "sync local writes the local launcher cache (the same ANU_ACCOUNT_CACHE write every sync does — local/self touch no device)"
assert_eq "600" "$(stat -c %a "$local_cache" 2>/dev/null || stat -f %Lp "$local_cache")" "…mode 0600"

t_section "register: local/self counterpart (real jq, not the ssh stub)"
: > "$LOG"
repo_local="$(mktmp)"
out="$(printf 'sk-ant-oat01-lr\n' | USAGE_ACCOUNTS_FILE="$repo_local/accounts.json" USAGE_TOKENS_FILE="$repo_local/tokens.json" \
  ANU_USAGE_HOST=local "$ACCOUNT" add loc 2>/dev/null)"
assert_ok $? "add registers locally when the serving host is 'local'"
assert_file "$repo_local/accounts.json" "…writing accounts.json on this machine"
assert_contains "$(cat "$repo_local/accounts.json")" '"id": "claude-loc"' "…with the new entry"
assert_contains "$(cat "$repo_local/accounts.json")" '"shared": false' "…a fresh entry with no --shared defaults to false, for real, host-side"
assert_contains "$out" "registered claude-loc on local" "…and reports the local registration"

# a re-add of the SAME account, still without --shared or --label, must not
# clobber an already-true shared (or a hand-edited label) already on the
# host — this is the real jq merge running end to end, not just the payload
# assertions above (which only see what the CLIENT sends).
: > "$LOG"
repo_shared="$(mktmp)"
cat > "$repo_shared/accounts.json" <<'JSON'
{"accounts":[{"id":"claude-existing","provider":"claude","label":"Hand Edited","name":"existing","token":{"key":"OLDKEY"},"shared":true}]}
JSON
out="$(printf 'sk-ant-oat01-re\n' | USAGE_ACCOUNTS_FILE="$repo_shared/accounts.json" USAGE_TOKENS_FILE="$repo_shared/tokens.json" \
  ANU_USAGE_HOST=local "$ACCOUNT" add existing 2>/dev/null)"
assert_ok $? "re-adding an existing account (no --shared, no --label) still succeeds"
assert_contains "$(cat "$repo_shared/accounts.json")" '"shared": true' "…and an already-true shared survives a re-add without --shared"
assert_contains "$(cat "$repo_shared/accounts.json")" '"label": "Hand Edited"' "…same for a hand-edited label, without --label"
assert_contains "$(cat "$repo_shared/accounts.json")" '"key": "EXISTING"' "…while the token key still updates to the new one"

# the malformed-host-file guard, exercised for REAL (real jq, real file),
# through the local/self path — not just simulated via the ssh stub above.
: > "$LOG"
repo_bad="$(mktmp)"
printf '{not valid json' > "$repo_bad/accounts.json"
before="$(cat "$repo_bad/accounts.json")"
out="$(printf 'sk-ant-oat01-bl\n' | USAGE_ACCOUNTS_FILE="$repo_bad/accounts.json" USAGE_TOKENS_FILE="$repo_bad/tokens.json" \
  ANU_USAGE_HOST=local "$ACCOUNT" add badlocal 2>&1)"; rc=$?
assert_fail $rc "add fails for real when the LOCAL accounts.json is malformed"
assert_eq "$before" "$(cat "$repo_bad/accounts.json")" "…the malformed file is left byte-for-byte unchanged"
[ ! -f "$repo_bad/accounts.json.tmp" ]; assert_ok $? "…and no .tmp file is left behind"
assert_contains "$out" "claude-badlocal is stored and synced, but registering it on local failed" \
  "…and the human still learns the token is safe, for the local path too"

# an entry with a poll grant (configDir, no token — set up by `npm run
# add-claude` on the serving host) must keep BOTH credentials after a
# re-add adds a launch token: `$existing * $entry` upserts by id and the
# merge no longer deletes configDir just because a token showed up — one
# entry carries both on purpose (task 15b; task 14a/bug 2 had this
# backwards).
: > "$LOG"
repo_legacy="$(mktmp)"
cat > "$repo_legacy/accounts.json" <<'JSON'
{"accounts":[{"id":"claude-x","provider":"claude","label":"old","configDir":"~/.claude-profiles/x"}]}
JSON
out="$(printf 'sk-ant-oat01-lg\n' | USAGE_ACCOUNTS_FILE="$repo_legacy/accounts.json" USAGE_TOKENS_FILE="$repo_legacy/tokens.json" \
  ANU_USAGE_HOST=local "$ACCOUNT" add x 2>/dev/null)"
assert_ok $? "add over an entry with a poll grant (no --label) still succeeds"
merged="$(cat "$repo_legacy/accounts.json")"
assert_contains "$merged" '"key": "X"' "…the merged entry carries the new token key"
assert_contains "$merged" '"configDir": "~/.claude-profiles/x"' "…and the existing configDir (poll grant) is preserved alongside it"
assert_contains "$merged" '"label": "old"' "…while the hand-set label (no --label given) is still preserved"

t_section "status"
stub "$SD" tmux 'case "$1" in
  list-panes) printf "%%4\taccount_alpha\tfable\tcccccccc-1111-2222-3333-444444444444\n%%5\t\t\t\n" ;;
esac'
out="$(TMUX=/tmp/fake-tmux "$ACCOUNT" status)"; assert_ok $? "status exits 0 inside tmux"
assert_contains "$out" "%4" "status lists the stamped pane"
assert_contains "$out" "account_alpha" "…with its account"
assert_not_contains "$out" "%5" "…and skips panes without a session"
assert_contains "$out" "next for fable: account_alpha" "status shows the fable pick"
assert_contains "$out" "next for any: " "status shows the any pick (empty here)"
out="$(env -u TMUX "$ACCOUNT" status)"; assert_ok $? "status exits 0 outside tmux"
assert_not_contains "$out" "PANE" "…without the pane table"
assert_contains "$out" "next for fable: account_alpha" "…but still with the picks"

t_section "help + unknown"
assert_contains "$("$ACCOUNT" help)" "com8-account" "help names the command"
"$ACCOUNT" bogus >/dev/null 2>&1; assert_fail $? "unknown verb fails"

t_section "launch"
# The env line also prints the credential vars a token launch must unset for
# the child (ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN/CLAUDE_CODE_USE_*) — kept
# after TOKEN=... so every existing prefix-only assertion below still matches.
stub "$SD" claude 'printf "claude %s\n" "$*"; printf "env ANU_ACCOUNT=%s TOKEN=%s API_KEY=%s AUTH_TOKEN=%s BEDROCK=%s VERTEX=%s FOUNDRY=%s PROVIDER=%s\n" "${ANU_ACCOUNT:-}" "${CLAUDE_CODE_OAUTH_TOKEN:-}" "${ANTHROPIC_API_KEY:-}" "${ANTHROPIC_AUTH_TOKEN:-}" "${CLAUDE_CODE_USE_BEDROCK:-}" "${CLAUDE_CODE_USE_VERTEX:-}" "${CLAUDE_CODE_USE_FOUNDRY:-}" "${ANU_PROVIDER:-}"'
stub "$SD" uuidgen 'echo 0B7B2A9E-1111-2222-3333-444444444444'
stub "$SD" tmux 'printf "tmux %s\n" "$*" >> "'"$LOG"'"'
: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --dangerously-skip-permissions)"
assert_contains "$out" "claude --session-id 0b7b2a9e-1111-2222-3333-444444444444 --dangerously-skip-permissions" "launch execs claude with a lowercase session id and the args"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha TOKEN=sk-ant-oat01-account_alpha" "the token and account reach the process environment"
assert_contains "$(cat "$LOG")" "tmux set-option -p -t %9 @anu_account account_alpha" "stamps the account"
assert_contains "$(cat "$LOG")" "tmux set-option -p -t %9 @anu_session 0b7b2a9e-1111-2222-3333-444444444444" "stamps the session"
assert_contains "$(cat "$LOG")" "tmux set-option -p -t %9 @anu_need any" "stamps the need"
assert_contains "$(cat "$LOG")" "tmux set-option -p -t %9 @anu_launch --dangerously-skip-permissions" "stamps the args for resume"
assert_contains "$(cat "$LOG")" "tmux set-option -p -t %9 @anu_box 0" "stamps box=0"

# B2.4: a token launch is the credential now — every other credential var
# that could silently outrank or conflict with it is unset for the child.
: > "$LOG"
out="$(ANTHROPIC_API_KEY=sk-plain ANTHROPIC_AUTH_TOKEN=tok-plain CLAUDE_CODE_USE_BEDROCK=1 \
  CLAUDE_CODE_USE_VERTEX=1 CLAUDE_CODE_USE_FOUNDRY=1 \
  TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --dangerously-skip-permissions)"
assert_contains "$out" "API_KEY= AUTH_TOKEN= BEDROCK= VERTEX= FOUNDRY=" \
  "a token launch unsets ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN/CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY for the child"

# …but the keychain-fallback launch (no token at all) leaves every one of
# those vars exactly as the caller's shell had them.
: > "$LOG"
out="$(CURL_FAIL=1 ANTHROPIC_API_KEY=sk-plain ANTHROPIC_AUTH_TOKEN=tok-plain CLAUDE_CODE_USE_BEDROCK=1 \
  TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --dangerously-skip-permissions 2>&1)"
assert_contains "$out" "API_KEY=sk-plain AUTH_TOKEN=tok-plain BEDROCK=1" \
  "the keychain-fallback launch leaves those vars untouched"

: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --need fable -- --dangerously-skip-permissions)"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha" "no --as: the pick decides"
assert_contains "$(cat "$LOG")" "pick?need=fable&fresh=1" "…asking for a fresh pick with the need"

out="$(ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --resume aaaaaaaa-1111-2222-3333-444444444444 --dangerously-skip-permissions)"
assert_contains "$out" "claude --resume aaaaaaaa-1111-2222-3333-444444444444 --dangerously-skip-permissions" "--resume: no session id minted"
assert_not_contains "$out" "--session-id" "…and none added"
assert_contains "$(cat "$LOG")" "@anu_session aaaaaaaa-1111-2222-3333-444444444444" "…the resumed id is stamped"
assert_contains "$(cat "$LOG")" "@anu_launch --dangerously-skip-permissions" "…and @anu_launch drops the --resume pair"

# bare --resume (no id — claude's own session picker, e.g. `cxx --resume`): must
# not hang. A regression here previously spun `_args_for_resume`'s `shift 2` at
# 100% CPU forever, so wrap this one invocation with a hard alarm — a
# regression then shows as a failed/killed assertion, not a wedged suite.
: > "$LOG"
out="$(perl -e 'alarm 10; exec @ARGV' -- env TMUX_PANE=%9 ANU_ACCOUNT=account_alpha "$ACCOUNT" launch --need any -- --resume --dangerously-skip-permissions 2>&1)"
assert_ok $? "bare --resume (no id) does not hang"
assert_contains "$out" "claude --resume --dangerously-skip-permissions" "bare --resume: claude gets it verbatim, no --session-id minted"
assert_not_contains "$out" "--session-id" "…and none added"
assert_contains "$(cat "$LOG")" "@anu_launch --dangerously-skip-permissions" "…@anu_launch drops the bare --resume flag too"

settings="$(mktmp)"; printf '{"model":"fable"}\n' > "$settings/settings.json"
: > "$LOG"
out="$(CLAUDE_CONFIG_DIR="$settings" ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" launch -- --dangerously-skip-permissions)"
assert_contains "$(cat "$LOG")" "@anu_need fable" "--need auto reads the model from settings.json"

# need=auto also honours --model in the claude args (before settings.json)
: > "$LOG"
out="$(ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" launch -- --model fable --dangerously-skip-permissions)"
assert_contains "$(cat "$LOG")" "@anu_need fable" "--need auto honours --model fable in the claude args"

# --continue/-c: claude's own last-session resume — never mint an id, stamp none
: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- -c --dangerously-skip-permissions)"
assert_contains "$out" "claude -c --dangerously-skip-permissions" "--continue (-c): claude gets it verbatim"
assert_not_contains "$out" "--session-id" "…and no session id minted"
line="$(grep -F 'set-option -p -t %9 @anu_session' "$LOG" | tail -1)"
assert_eq "tmux set-option -p -t %9 @anu_session " "$line" "…@anu_session stamped empty, so rotate refuses honestly"

# --session-id <id>: a caller-supplied id — never mint another, stamp the caller's
: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --session-id abc123 --x)"
assert_contains "$out" "claude --session-id abc123 --x" "--session-id: claude gets exactly the caller's id"
cnt="$(printf '%s\n' "$out" | grep -o -- "--session-id" | wc -l | tr -d ' ')"
assert_eq "1" "$cnt" "…only one --session-id reaches claude"
assert_contains "$(cat "$LOG")" "@anu_session abc123" "…and abc123 is stamped, not a minted one"

# @anu_launch is shell-quoted and drops the trailing positional (the prompt is
# already in the transcript) — so rotate's typed relaunch line reparses exactly
: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --dangerously-skip-permissions --append-system-prompt "be terse" "fix the tests")"
assert_contains "$(cat "$LOG")" "@anu_launch --dangerously-skip-permissions --append-system-prompt be\\ terse " \
  "@anu_launch keeps the flag+value pair, %q-quoted, and drops the trailing prompt positional"

# @anu_launch keeps only a WHITELIST of claude flags — a generic "flag then
# non-flag token is that flag's value" heuristic can't tell a boolean flag's
# own trailing prompt apart from a valued flag's real value, and `cxx
# "prompt"` (a boolean flag directly followed by the prompt) is the most
# common alias shape, so guessing would re-send the prompt as extra argv.
: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --dangerously-skip-permissions "continue the refactor")"
log="$(cat "$LOG")"
assert_contains "$log" "@anu_launch --dangerously-skip-permissions " "a boolean flag's trailing positional (the prompt) is dropped, not kept as its value"
assert_not_contains "$log" "continue" "…the prompt text itself never reaches @anu_launch"

: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --model fable "prompt")"
log="$(cat "$LOG")"
assert_contains "$log" "@anu_launch --model fable " "a whitelisted valued flag keeps its value…"
assert_not_contains "$log" "@anu_launch --model fable prompt" "…but not the trailing positional after it"

: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --foo bar --dangerously-skip-permissions)"
line="$(grep -F '@anu_launch' "$LOG" | tail -1)"
assert_eq "tmux set-option -p -t %9 @anu_launch --dangerously-skip-permissions " "$line" \
  "an unrecognized flag and its value are dropped; only the whitelisted flag after it survives"

: > "$LOG"
out="$(CURL_FAIL=1 TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --dangerously-skip-permissions 2>&1)"
assert_contains "$out" "claude --dangerously-skip-permissions" "no pick and no --as: plain claude on the keychain login"
assert_contains "$out" "keychain login" "…with a warning"
assert_not_contains "$out" "TOKEN=sk-" "…and no token"
log="$(cat "$LOG")"
assert_contains "$log" "tmux set-option -pu -t %9 @anu_account" "keychain fallback unsets @anu_account…"
assert_contains "$log" "tmux set-option -pu -t %9 @anu_session" "…@anu_session…"
assert_contains "$log" "tmux set-option -pu -t %9 @anu_launch" "…@anu_launch…"
assert_contains "$log" "tmux set-option -pu -t %9 @anu_need" "…@anu_need…"
assert_contains "$log" "tmux set-option -pu -t %9 @anu_box" "…and @anu_box, so watch/rotate never resume a stale stamp"

out="$("$ACCOUNT" launch --as nope -- --x 2>&1)"; rc=$?
assert_eq "1" "$rc" "unknown account fails before exec, exit exactly 1"
out="$("$ACCOUNT" launch --as account_alpha --dangerously-skip-permissions 2>&1)"; assert_fail $? "claude args must follow --"

# An inherited ANU_ACCOUNT belongs to ONE provider. A shell opened from a
# CODEX pane carries that pane's codex account name; reading it as a claude
# --as would launch Claude Code as an account that does not exist on this
# side. ANU_PROVIDER, exported beside it, is what says whose name it is —
# and its absence means a pre-port launch, which was always claude.
: > "$LOG"
out="$(ANU_ACCOUNT=account_gamma ANU_PROVIDER=codex TMUX_PANE=%9 "$ACCOUNT" launch --need fable -- --x 2>&1)"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha" "a claude launch ignores an inherited CODEX account and picks fresh"
assert_not_contains "$out" "ANU_ACCOUNT=account_gamma" "…never launching Claude Code as a codex account name"
assert_contains "$(cat "$LOG")" "pick?need=fable" "…the dashboard is asked, exactly as if nothing were inherited"

out="$(ANU_ACCOUNT=account_beta TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --x 2>&1)"
assert_contains "$out" "env ANU_ACCOUNT=account_beta" "an inherited account with NO provider is still honoured (a pre-port launch was always claude)"

out="$(ANU_ACCOUNT=account_beta ANU_PROVIDER=claude TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --x 2>&1)"
assert_contains "$out" "env ANU_ACCOUNT=account_beta" "…and so is one that names claude"
assert_contains "$out" "PROVIDER=claude" "a claude launch exports ANU_PROVIDER=claude beside ANU_ACCOUNT"

# The keychain fallback is no account at all — a stale identity must not ride
# into the child and let a nested launch inherit it.
out="$(CURL_FAIL=1 ANU_ACCOUNT=account_gamma ANU_PROVIDER=codex TMUX_PANE=%9 \
  "$ACCOUNT" launch --need any -- --x 2>&1)"
assert_contains "$out" "env ANU_ACCOUNT= " "the keychain fallback clears the inherited account for the child"
assert_contains "$out" "PROVIDER=" "…and its provider"
assert_not_contains "$out" "PROVIDER=codex" "…so nothing downstream inherits a stale identity"

t_section "launch: distinct outcomes (B2.5)"
# Pool exhausted: the dashboard IS reachable but ranked nothing (pick-empty.json,
# served for need=any) — a real, distinguishable fact, never silently folded
# into the keychain fallback.
: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --dangerously-skip-permissions 2>&1)"; rc=$?
assert_eq "3" "$rc" "pool exhausted (dashboard reachable, nothing named) exits 3"
assert_contains "$out" "no account has room for any" "…and says so"
assert_not_contains "$out" "claude " "…and claude never execs without --fallback-keychain"

: > "$LOG"
out="$(TMUX_PANE=%9 "$ACCOUNT" launch --need any --fallback-keychain -- --dangerously-skip-permissions 2>&1)"; rc=$?
assert_ok $rc "…but --fallback-keychain proceeds to the keychain login"
assert_contains "$out" "claude --dangerously-skip-permissions" "…and execs claude"
assert_contains "$out" "keychain login" "…with a warning"
assert_not_contains "$out" "TOKEN=sk-" "…and no token"

# Dashboard unreachable: falls back to a RECENT (<10min) successful pick for
# the same need, before ever trying the keychain — isolated cache so this
# doesn't interact with any other test's picks.
LP_CACHE="$(mktmp)/tokens.json"
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$LP_CACHE" TMUX_PANE=%9 "$ACCOUNT" launch --need fable -- --dangerously-skip-permissions)"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha" "a real successful pick warms the last-pick record"
LP_FILE="$(dirname "$LP_CACHE")/last-pick.fable"
assert_file "$LP_FILE" "…writing the device-local last-pick file beside the token cache"
assert_contains "$(cat "$LP_FILE")" "account_alpha" "…with the picked account name"
assert_contains "$(cat "$LOG")" "@anu_last_pick fable:account_alpha" "…and the pane option write is need-keyed too (need:name:epoch)"

: > "$LOG"
out="$(CURL_FAIL=1 ANU_ACCOUNT_CACHE="$LP_CACHE" TMUX_PANE=%9 "$ACCOUNT" launch --need fable -- --dangerously-skip-permissions 2>&1)"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha TOKEN=sk-ant-oat01-account_alpha" "dashboard down: launch resumes on the recent last pick, not the keychain"
assert_contains "$out" "last pick" "…and says so"

# B2 fix round 1, item 6: the last-pick record (pane option AND file) is
# keyed by need — a fresh pick for one need (fable, just warmed above) must
# never answer for a DIFFERENT need (any).
: > "$LOG"
out="$(CURL_FAIL=1 ANU_ACCOUNT_CACHE="$LP_CACHE" TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --dangerously-skip-permissions 2>&1)"
assert_contains "$out" "keychain login" "a recent pick for a DIFFERENT need (fable) never answers for this one (any)"
assert_not_contains "$out" "TOKEN=sk-" "…no token"
assert_not_contains "$out" "resuming the last pick" "…and the message never claims a last pick was used"

# B2 fix round 2, item 5a: the mismatch above went through the FILE path
# only (this section's default tmux stub answers no show-options at all,
# so _last_pick_read's pane-OPTION branch never actually ran). Exercise it
# directly with a stub that answers @anu_last_pick for real.
LP_CACHE_OPT="$(mktmp)/tokens.json"
recent_epoch="$(date +%s)"
: > "$LOG"
stub "$SD" tmux '
printf "tmux %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  show-options) case "$*" in
    *@anu_last_pick*) echo "fable:account_alpha:'"$recent_epoch"'" ;;
    *) echo ;;
  esac ;;
esac'
out="$(CURL_FAIL=1 ANU_ACCOUNT_CACHE="$LP_CACHE_OPT" TMUX_PANE=%9 "$ACCOUNT" launch --need any -- --dangerously-skip-permissions 2>&1)"
assert_contains "$out" "keychain login" "a pane-option last pick recorded under a DIFFERENT need (fable) is refused for THIS need (any) — the pane-option branch itself, not just the file"
assert_not_contains "$out" "TOKEN=sk-" "…no token"

# …and the matching-need positive case: the SAME pane option, read for the
# need it was actually recorded under, IS honored.
: > "$LOG"
out="$(CURL_FAIL=1 ANU_ACCOUNT_CACHE="$LP_CACHE_OPT" TMUX_PANE=%9 "$ACCOUNT" launch --need fable -- --dangerously-skip-permissions 2>&1)"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha TOKEN=sk-ant-oat01-account_alpha" "…but the matching need (fable) is honored straight from the pane option"
assert_contains "$out" "last pick" "…and says so"
stub "$SD" tmux 'printf "tmux %s\n" "$*" >> "'"$LOG"'"'

# …but an old (>10min) last pick is not honored — falls through to the keychain.
printf 'account_alpha %s\n' "$(( $(date +%s) - 700 ))" > "$LP_FILE"
: > "$LOG"
out="$(CURL_FAIL=1 ANU_ACCOUNT_CACHE="$LP_CACHE" TMUX_PANE=%9 "$ACCOUNT" launch --need fable -- --dangerously-skip-permissions 2>&1)"
assert_contains "$out" "keychain login" "an old (>10min) last pick is not honored — falls through to the keychain"
assert_not_contains "$out" "TOKEN=sk-" "…no token"

# B2 fix round 1, item 7: a fresh (under 10min), need-matched cached pick
# whose token can no longer be resolved locally falls the REST of the way
# through to the keychain, with a warning — not exit 1. A brand-new pane
# (never stamped @anu_last_pick) so only the FILE path is exercised here.
LP_CACHE3="$(mktmp)/tokens.json"
printf 'ghost %s\n' "$(date +%s)" > "$(dirname "$LP_CACHE3")/last-pick.any"
: > "$LOG"
out="$(CURL_FAIL=1 ANU_ACCOUNT_CACHE="$LP_CACHE3" TMUX_PANE=%50 "$ACCOUNT" launch --need any -- --dangerously-skip-permissions 2>&1)"; rc=$?
assert_ok $rc "a cached pick whose token no longer resolves locally still falls through to the keychain, exit 0"
assert_contains "$out" "keychain login" "…with a warning"
assert_contains "$out" "has no local token" "…naming the reason"
assert_not_contains "$out" "TOKEN=sk-" "…and no token"

t_section "launch --box"
stub "$SD" box 'printf "box %s\n" "$*"; printf "boxenv ANU_ACCOUNT=%s TOKEN=%s\n" "${ANU_ACCOUNT:-}" "${CLAUDE_CODE_OAUTH_TOKEN:-}"'
: > "$LOG"
out="$(ANU_ACCOUNT_BOX_BIN="$SD/box" TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any --box -- --dangerously-skip-permissions)"
assert_contains "$out" "box claude --session-id" "--box runs box claude"
assert_contains "$out" "boxenv ANU_ACCOUNT=account_alpha TOKEN=sk-ant-oat01-account_alpha" "…with the env"
assert_contains "$(cat "$LOG")" "@anu_box 1" "…and stamps box=1"

t_section "hook"
# The SessionStart hook Claude Code itself calls on startup/resume/clear/
# compact/fork — reuses the plain logging tmux stub from "launch" above (no
# case branches needed; every call is just recorded).
stub "$SD" tmux 'printf "tmux %s\n" "$*" >> "'"$LOG"'"'
: > "$LOG"
out="$(printf '{"session_id":"11111111-2222-3333-4444-555555555555","hook_event_name":"SessionStart","source":"startup","cwd":"/tmp","transcript_path":"/tmp/x.jsonl"}' \
  | ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" hook session-start)"; rc=$?
assert_eq "0" "$rc" "the SessionStart hook always exits 0"
assert_eq "" "$out" "…and prints nothing on success"
log="$(cat "$LOG")"
assert_contains "$log" "set-option -p -t %9 @anu_session 11111111-2222-3333-4444-555555555555" "stamps the session id read from stdin"
assert_contains "$log" "set-option -p -t %9 @anu_session_source startup" "…the start reason (this CLI ships it as \"source\" — see the field-name note in cmd_hook_session_start)"
assert_contains "$log" "set-option -p -t %9 @anu_generation" "…and a fresh generation stamp"

# This hook is GLOBAL — every Claude Code process runs it, not just ones anu
# launched — so $ANU_ACCOUNT (exported only by cmd_launch's token-resolved
# path; never by the keychain fallback, which unsets it) is the gate that
# keeps it off a plain, by-hand `claude`. Without it, a manually-started
# pane would gain @anu_session/@anu_generation and pass rotate/switch's
# "was this started by @ACCOUNT_LAUNCH@ launch" check.
: > "$LOG"
out="$(printf '{"session_id":"zzzz","hook_event_name":"SessionStart","source":"startup"}' \
  | env -u ANU_ACCOUNT TMUX_PANE=%9 "$ACCOUNT" hook session-start)"; rc=$?
assert_eq "0" "$rc" "no \$ANU_ACCOUNT (an unmanaged pane): still exits 0"
assert_eq "" "$(cat "$LOG")" "…and stamps nothing at all — no set-option calls"

: > "$LOG"
out="$(printf '{"session_id":"aaaa","hook_event_name":"SessionStart","source":"fork"}' \
  | env -u TMUX_PANE ANU_ACCOUNT=account_alpha ANU_PANE=%7 "$ACCOUNT" hook session-start)"; rc=$?
assert_eq "0" "$rc" "no \$TMUX_PANE: falls back to \$ANU_PANE (exported by launch for exactly this)"
assert_contains "$(cat "$LOG")" "set-option -p -t %7 @anu_session aaaa" "…and stamps the pane ANU_PANE names"

: > "$LOG"
out="$(printf '{"session_id":"bbbb"}' | ANU_ACCOUNT=account_alpha env -u TMUX_PANE -u ANU_PANE "$ACCOUNT" hook session-start)"; rc=$?
assert_eq "0" "$rc" "no pane at all (neither TMUX_PANE nor ANU_PANE): still exits 0"
assert_eq "" "$out" "…silently"
assert_eq "" "$(cat "$LOG")" "…and touches tmux not at all"

: > "$LOG"
out="$(printf 'not json' | ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" hook session-start)"; rc=$?
assert_eq "0" "$rc" "malformed stdin still exits 0 — a hook must never block Claude"
assert_eq "" "$(cat "$LOG")" "…and stamps nothing (no session_id to read)"

: > "$LOG"
out="$(printf '{"hook_event_name":"SessionStart","source":"compact"}' | ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" hook session-start)"; rc=$?
assert_eq "0" "$rc" "valid JSON with no session_id still exits 0"
assert_eq "" "$(cat "$LOG")" "…and stamps nothing"

out="$("$ACCOUNT" hook bogus-sub < /dev/null)"; rc=$?
assert_eq "0" "$rc" "an unknown hook sub-verb still exits 0, never blocks Claude"

# --- StopFailure (B5): the provider's own "this turn died on a rate limit" ---
# Verified against the 2.1.272 binary: the payload carries `error` (an enum
# including "rate_limit"), NOT `error_type`, and the matcher's fieldToMatch is
# `error`; the event is fire-and-forget (output and exit codes ignored). The
# handler re-checks the field anyway, so a matcher-less wiring still only stamps
# on a real wall.
: > "$LOG"
out="$(printf '{"session_id":"s1","hook_event_name":"StopFailure","error":"rate_limit","error_details":"5-hour limit reached"}' \
  | ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" hook stop-failure)"; rc=$?
assert_eq "0" "$rc" "the StopFailure hook always exits 0"
assert_eq "" "$out" "…and prints nothing"
assert_contains "$(cat "$LOG")" "set-option -p -t %9 @anu_wall_event" "a rate_limit failure stamps @anu_wall_event on the pane"

: > "$LOG"
out="$(printf '{"session_id":"s1","hook_event_name":"StopFailure","error_type":"rate_limit"}' \
  | ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" hook stop-failure)"; rc=$?
assert_eq "0" "$rc" "the legacy field name the plan assumed still exits 0"
assert_contains "$(cat "$LOG")" "@anu_wall_event" "…and is accepted as a fallback for .error"

: > "$LOG"
out="$(printf '{"session_id":"s1","hook_event_name":"StopFailure","error":"overloaded"}' \
  | ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" hook stop-failure)"; rc=$?
assert_eq "0" "$rc" "a non-rate_limit API error still exits 0"
assert_eq "" "$(cat "$LOG")" "…and stamps nothing — only a wall corroborates a wall"

: > "$LOG"
out="$(printf '{"session_id":"s1","hook_event_name":"StopFailure","error":"rate_limit"}' \
  | env -u ANU_ACCOUNT TMUX_PANE=%9 "$ACCOUNT" hook stop-failure)"; rc=$?
assert_eq "0" "$rc" "no \$ANU_ACCOUNT (an unmanaged pane): still exits 0"
assert_eq "" "$(cat "$LOG")" "…and stamps nothing — watchd must never be handed a wall event for a pane anu did not launch"

: > "$LOG"
out="$(printf '{"session_id":"s1","error":"rate_limit"}' \
  | ANU_ACCOUNT=account_alpha env -u TMUX_PANE ANU_PANE=%7 "$ACCOUNT" hook stop-failure)"; rc=$?
assert_eq "0" "$rc" "no \$TMUX_PANE: falls back to \$ANU_PANE"
assert_contains "$(cat "$LOG")" "set-option -p -t %7 @anu_wall_event" "…and stamps the pane ANU_PANE names"

: > "$LOG"
out="$(printf 'not json' | ANU_ACCOUNT=account_alpha TMUX_PANE=%9 "$ACCOUNT" hook stop-failure)"; rc=$?
assert_eq "0" "$rc" "malformed stdin still exits 0 — a hook must never block Claude"
assert_eq "" "$(cat "$LOG")" "…and stamps nothing"

: > "$LOG"
out="$("$ACCOUNT" hook stop-failure < /dev/null)"; rc=$?
assert_eq "0" "$rc" "empty stdin with no environment at all still exits 0"
assert_eq "" "$(cat "$LOG")" "…and stamps nothing"

out="$("$ACCOUNT" watch --once 2>&1)"; rc=$?
assert_eq "1" "$rc" "the polling \`watch\` verb is gone — unattended rotation is watchd's limited arm"
assert_contains "$out" "unknown verb" "…and the dispatcher says so"

t_section "token cache"
# Every case below sets its own ANU_ACCOUNT_CACHE (a fresh, unwritten mktmp
# path unless pre-seeded) so each is isolated from the others and from the
# suite-wide default set at the top of this file.

# --- launch: a warm cache is served without ever calling anu-secrets --------
cache_warm="$(mktmp)/tokens.json"
printf '{"ACCOUNT_ALPHA":"sk-ant-oat01-cached"}' > "$cache_warm"
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_warm" TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --dangerously-skip-permissions)"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha TOKEN=sk-ant-oat01-cached" "a warm cache serves the cached token, not a fresh fetch"
assert_not_contains "$(cat "$LOG")" "anu-secrets get" "…without calling anu-secrets get at all"

# --- launch: a cold cache fetches once and warms the file -------------------
# A logging jq "spy" (real jq underneath, args recorded to $LOG) proves the
# token value itself never reaches jq's argv — argv is ps-visible, unlike a
# pipe/redirect/heredoc, so this is where a leak would actually show up.
cache_cold="$(mktmp)/tokens.json"
: > "$LOG"
stub "$SD" jq '
printf "jq %s\n" "$*" >> "'"$LOG"'"
exec "'"$REAL_JQ"'" "$@"'
out="$(ANU_ACCOUNT_CACHE="$cache_cold" TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --dangerously-skip-permissions 2>&1)"
rm -f "$SD/jq"
assert_contains "$out" "env ANU_ACCOUNT=account_alpha TOKEN=sk-ant-oat01-account_alpha" "a cold cache still launches, fetched from anu-secrets"
assert_not_contains "$out" "cached $cache_cold" "warming the cache along the way is silent outside sync — only sync narrates a 'cached' line"
calls="$(grep -c '^anu-secrets get /anu/agents/claude/accounts ACCOUNT_ALPHA --show$' "$LOG")"
assert_eq "1" "$calls" "…calling anu-secrets get exactly once"
assert_file "$cache_cold" "…and the miss writes the cache file"
assert_jq "$cache_cold" '.ACCOUNT_ALPHA' "sk-ant-oat01-account_alpha" "…with the fetched token under the uppercased key"
assert_eq "600" "$(stat -c %a "$cache_cold" 2>/dev/null || stat -f %Lp "$cache_cold")" "…mode 0600"
assert_not_contains "$(grep '^jq ' "$LOG")" "sk-ant-oat01-account_alpha" \
  "the cached token itself never reaches jq's argv (ps-visible) — it travels through a scratch file"

# --- token --refresh: bypasses a warm cache and re-caches the fresh value ---
cache_stale="$(mktmp)/tokens.json"
printf '{"ACCOUNT_ALPHA":"sk-ant-oat01-stale"}' > "$cache_stale"
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_stale" "$ACCOUNT" token account_alpha --refresh)"
assert_eq "sk-ant-oat01-account_alpha" "$out" "--refresh ignores the stale cached value"
assert_contains "$(cat "$LOG")" "anu-secrets get /anu/agents/claude/accounts ACCOUNT_ALPHA --show" "…and calls anu-secrets get even though the cache was warm"
assert_jq "$cache_stale" '.ACCOUNT_ALPHA' "sk-ant-oat01-account_alpha" "…updating the cache with the fresh value"

# plain `token` (no --refresh) is cache-first too, same as launch
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_warm" "$ACCOUNT" token account_alpha)"
assert_eq "sk-ant-oat01-cached" "$out" "token without --refresh reads the warm cache"
assert_not_contains "$(cat "$LOG")" "anu-secrets get" "…without touching anu-secrets"

# --- sync: always warms the cache first, even when the device write fails ---
cache_sync="$(mktmp)/tokens.json"
: > "$LOG"
stub "$SD" ssh 'printf "ssh %s\n" "$*" >> "'"$LOG"'"; cat >/dev/null; exit 9'
out="$(ANU_ACCOUNT_CACHE="$cache_sync" "$ACCOUNT" sync some-broken-host 2>&1)"; rc=$?
assert_fail $rc "sync still fails when the device write itself fails"
assert_contains "$out" "cached $cache_sync" "…but reports the cache write on stderr"
assert_jq "$cache_sync" '.ACCOUNT_ALPHA' "sk-ant-oat01-account_alpha" "…and the cache was written despite the ssh failure — sync writes it before the device call"
install_ssh_stub

# --- sync cache: writes only the cache, no device touched at all ------------
cache_only="$(mktmp)/tokens.json"
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_only" "$ACCOUNT" sync cache 2>&1)"
assert_ok $? "sync cache exits 0"
assert_contains "$out" "cached $cache_only" "…and reports the cache write"
assert_jq "$cache_only" '.ACCOUNT_GAMMA' "sk-ant-oat01-account_gamma" "…the cache is written with every token under the path"
assert_not_contains "$(cat "$LOG")" "ssh" "…without touching any device (no ssh call at all)"

# --- add: warms the cache with the just-pasted token immediately ------------
cache_add="$(mktmp)/tokens.json"
: > "$LOG"
out="$(printf 'sk-ant-oat01-warm\n' | ANU_ACCOUNT_CACHE="$cache_add" "$ACCOUNT" add warm-acct 2>/dev/null)"
assert_ok $? "add exits 0 (cache scenario)"
assert_jq "$cache_add" '.WARM_ACCT' "sk-ant-oat01-warm" "add warms the cache with the token it just stored, so the minting device is warm immediately"

# --- a cache miss for one key never disturbs the others ---------------------
cache_partial="$(mktmp)/tokens.json"
printf '{"ACCOUNT_ALPHA":"sk-ant-oat01-existing","OTHER":"sk-ant-oat01-other"}' > "$cache_partial"
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_partial" TMUX_PANE=%9 "$ACCOUNT" launch --as account_alpha --need any -- --dangerously-skip-permissions)"
assert_contains "$out" "TOKEN=sk-ant-oat01-existing" "a present key is served straight from cache"
assert_jq "$cache_partial" '.OTHER' "sk-ant-oat01-other" "…and an unrelated cached key is left untouched"
out2="$(ANU_ACCOUNT_CACHE="$cache_partial" "$ACCOUNT" token nope 2>&1)"; rc2=$?
assert_fail $rc2 "a miss for an account with no token anywhere still fails"
assert_jq "$cache_partial" '.ACCOUNT_ALPHA' "sk-ant-oat01-existing" "…and the failed miss did not disturb the existing account_alpha entry"
assert_jq "$cache_partial" '.OTHER' "sk-ant-oat01-other" "…nor the other unrelated entry"

# --- the cache is always best-effort: a local write failure never aborts ----
# the real operation. ANU_ACCOUNT_CACHE points inside a path whose PARENT is
# a plain file, so every mkdir/open under it fails with ENOTDIR no matter
# what — a stand-in for a read-only/misconfigured state dir.
blocker="$(mktmp)/blocker"; : > "$blocker"
cache_broken="$blocker/tokens.json"

: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_broken" "$ACCOUNT" token account_alpha --refresh 2>&1)"; rc=$?
assert_eq "0" "$rc" "token --refresh still exits 0 when the cache write fails"
assert_contains "$out" "sk-ant-oat01-account_alpha" "…and still prints the fresh token"
assert_contains "$out" "cache write failed" "…with a warning that the cache write failed"

: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_broken" "$ACCOUNT" sync other-host 2>&1)"; rc=$?
assert_eq "0" "$rc" "sync still exits 0 (the device write still runs) when the local cache write fails"
assert_contains "$(cat "$LOG")" "ssh other-host" "…and the device write still happened"
assert_contains "$out" "cache write failed" "…with a warning that the cache write failed"

: > "$LOG"
out="$(printf 'sk-ant-oat01-brk\n' | ANU_ACCOUNT_CACHE="$cache_broken" "$ACCOUNT" add broken-acct 2>&1)"; rc=$?
assert_eq "0" "$rc" "add still exits 0 when the local cache write fails"
assert_contains "$out" "registered claude-broken-acct on usage-host" "…and it still registers on the serving host"
assert_contains "$out" "cache write failed" "…with a warning that the cache write failed"

# --- a corrupt cache file is moved aside, never silently replaced -----------
cache_corrupt="$(mktmp)/tokens.json"
printf '{not valid json' > "$cache_corrupt"
before_corrupt="$(cat "$cache_corrupt")"
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_corrupt" "$ACCOUNT" token account_alpha 2>&1)"; rc=$?
assert_eq "0" "$rc" "token still succeeds when the existing cache file is corrupt"
assert_contains "$out" "sk-ant-oat01-account_alpha" "…and prints the freshly-fetched token"
assert_contains "$out" "was not valid JSON" "…with a warning that the old cache was corrupt"
corrupt_file="$(ls "$(dirname "$cache_corrupt")"/tokens.json.corrupt-* 2>/dev/null | head -1)"
assert_ne "" "$corrupt_file" "…the corrupt file was moved aside to a .corrupt-<epoch> path, not just dropped"
if [ -n "$corrupt_file" ]; then
  assert_eq "$before_corrupt" "$(cat "$corrupt_file")" "…with its original bytes intact"
fi
assert_jq "$cache_corrupt" '.ACCOUNT_ALPHA' "sk-ant-oat01-account_alpha" "…and the new cache holds the freshly-fetched key"

# --- a nested, not-yet-existing cache directory is created 0700 ------------
cache_nested="$(mktmp)/deep/nested/dir/tokens.json"
: > "$LOG"
out="$(ANU_ACCOUNT_CACHE="$cache_nested" "$ACCOUNT" sync cache 2>&1)"
assert_ok $? "sync cache creates a nested cache directory that doesn't exist yet"
dirmode="$(stat -c %a "$(dirname "$cache_nested")" 2>/dev/null || stat -f %Lp "$(dirname "$cache_nested")")"
assert_eq "700" "$dirmode" "…the newly created cache directory is 0700"

t_section "rotate"
# pane stub: state resolves the target (echoing it back as "pane", mapping any
# non-%id target to %4) and reports the wall; send is logged. %99 is wired to
# fail resolution, to exercise the "no such pane" path. `state` answers plain
# text OR --json depending on the caller, and flips from limited to
# ${PANE_STATE_AFTER-idle} once the relaunch line has been typed — so the
# post-relaunch idle-wait has something to observe (PANE_STATE_AFTER=limited
# simulates a relaunch whose resumed transcript re-renders the old wall; PANE_STATE_AFTER=booting one that never comes up, for the timeout case below).
stub "$SD" pane '
printf "pane %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  state)
    shift
    json=0; t=""
    for a in "$@"; do case "$a" in --json) json=1 ;; *) [ -z "$t" ] && t="$a" ;; esac; done
    [ "$t" = "%99" ] && exit 1
    case "$t" in %*) p="$t" ;; *) p="%4" ;; esac
    if grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'"; then
      st="${PANE_STATE_AFTER-idle}"
    elif [ -n "${PANE_STATE_RECHECK:-}" ] && grep -q "set-option.*@anu_rotating" "'"$LOG"'"; then
      # B2.2: _relaunch_and_resume re-reads pane state right before leaving —
      # after @anu_rotating is stamped but before any relaunch line is typed.
      # This simulates a pane whose state changed between the callers own
      # initial check and that recheck (a race).
      st="$PANE_STATE_RECHECK"
    else
      st="${PANE_STATE:-limited}"
    fi
    if [ "$json" = 1 ]; then
      echo "{\"pane\":\"$p\",\"state\":\"$st\",\"cli\":\"claude\",\"wall\":\"${PANE_WALL:-fable}\"}"
    else
      echo "$st"
    fi
    ;;
  send)  [ -n "${PANE_SEND_FAIL:-}" ] && exit 1; exit 0 ;;
esac'
# tmux stub: options from env; pane_current_command is claude/bash-flipping
# (via COUNT) until the launch line is typed, then flips straight back to
# claude (shell -> claude) so _wait_running has something to observe. The
# human's own shell prompt glyph ("❯ ") sits on screen the WHOLE time once the
# launch line is typed — proving neither wait reads the screen to decide.
COUNT="$FIX/count"; : > "$COUNT"
stub "$SD" tmux '
printf "tmux %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  show-options) case "$*" in
    *@anu_account*) echo "${OPT_ACCOUNT-account_beta}" ;; *@anu_session*) echo "${OPT_SESSION-cccccccc-1111-2222-3333-444444444444}" ;;
    *@anu_need*) echo "${OPT_NEED-fable}" ;; *@anu_launch*) echo "--dangerously-skip-permissions" ;;
    *@anu_box*) echo "${OPT_BOX-0}" ;; *@anu_rotating*) echo "${OPT_ROTATING-}" ;;
    *@anu_tried*) echo "${OPT_TRIED-}" ;;
    *@anu_generation*)
      # Default: a constant generation on every read (before AND after the
      # relaunch line is typed) — the pre/re-check inside _relaunch_and_resume
      # always match (no false "pane changed under me") and the post-relaunch value
      # is never newer (no false hook-ready path): every existing rotate/
      # switch test below exercises the running/idle fallback, exactly as
      # before this stub grew a generation case at all.
      #   OPT_GENERATION_DRIFT=1  — the read taken once @anu_rotating has
      #     been stamped (i.e. the pre-type recheck, not the very first read)
      #     comes back different, simulating a pane whose generation changed
      #     underneath rotate/switch — the "pane changed under me" guard.
      #   OPT_GENERATION_AFTER=<n> — once the relaunch line itself is in the
      #     log, report this (higher) generation instead — simulating the
      #     SessionStart hook stamping a fresh one once Claude is back up —
      #     the hook-stamped readiness path.
      if [ "${OPT_GENERATION_DRIFT:-0}" = 1 ] && grep -q "set-option.*@anu_rotating" "'"$LOG"'"; then
        echo "${OPT_GENERATION:-1000}-drifted"
      elif grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'"; then
        echo "${OPT_GENERATION_AFTER-${OPT_GENERATION:-1000}}"
      else
        echo "${OPT_GENERATION:-1000}"
      fi ;;
    *) echo ;; esac ;;
  display-message)
    case "$*" in
      *socket_path*)
        # _socket_name: a fixed, deterministic socket path for every pane,
        # independent of this process own $TMUX — the whole point of the
        # fix (the caller $TMUX must never decide the lock name).
        echo "/tmp/tmux-test/default" ;;
      *)
        if grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'"; then
          # What the pane is RUNNING once the relaunch line has been typed.
          # claude by default (the happy path); PANE_CMD_AFTER=bash is a
          # relaunch that died before claude ever started (box failed, the
          # token was rejected, claude crashed) and left a bare shell;
          # PANE_CMD_AFTER=container is a boxed relaunch.
          echo "${PANE_CMD_AFTER-claude}"
        else
          n=$(wc -l < "'"$COUNT"'"); echo x >> "'"$COUNT"'"
          [ "$n" -ge 2 ] && echo bash || echo claude
        fi ;;
    esac ;;
  capture-pane) grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'" && echo "❯ " || echo "$ " ;;
  list-panes) printf "%%4\t%s\t%s\t%s\n%%5\t\t\t\n" "${OPT_SESSION-cccccccc-1111-2222-3333-444444444444}" "${OPT_ACCOUNT-account_beta}" "${OPT_ROTATING-}" ;;
esac'
stub "$SD" sleep 'exit 0'
: > "$LOG"; : > "$COUNT"
out="$("$ACCOUNT" rotate %4)"; rc=$?
assert_ok $rc "rotate exits 0"
log="$(cat "$LOG")"
assert_contains "$log" "pane state %4 --json" "rotate confirms the wall from the pane"
assert_contains "$log" "pick?need=fable&fresh=1&exclude=account_beta" "fable wall: picks for fable, excluding the walled account"
assert_contains "$log" "@anu_rotating" "stamps the rotation"
assert_match "$log" "send-keys -t %4 Escape.*send-keys -t %4 C-c.*send-keys -t %4 C-c" "escape then two ctrl-c to leave"
assert_contains "$log" "send-keys -t %4 printf '\033[2J\033[3J\033[H' && @ACCOUNT_LAUNCH@ launch --as account_alpha --need fable -- --resume cccccccc-1111-2222-3333-444444444444 --dangerously-skip-permissions C-m" "relaunch line clears the screen first, then resumes the session under the pick"
assert_contains "$log" "pane send %4 $CONT_TEXT" "one line restarts the task"
assert_contains "$out" "account_beta → account_alpha" "reports the move"
assert_contains "$out" "ready via fallback" "…and with no hook stamps at all (older CLI, no SessionStart hook), the report names the fallback readiness path"
# the relaunch line is typed only after the shell is back — count only the
# display-message polls up to and including the launch line; the later ones
# (from _wait_running, after the launch line) don't speak to this ordering.
launch_line="$(printf '%s' "$log" | grep -n "@ACCOUNT_LAUNCH@ launch" | head -1 | cut -d: -f1)"
pre_checks="$(printf '%s\n' "$log" | sed -n "1,${launch_line}p" | grep -c "display-message")"
[ "$pre_checks" -ge 1 ]; assert_ok $? "the launch line follows the shell check"
# "continue" is sent only once the pane is confirmed running AND idle — never
# from screen text (the stub's own shell-prompt glyph "❯ " is on screen the
# whole time post-relaunch and must never trigger it, unlike the old
# _wait_prompt, which scanned the screen for exactly that glyph).
idle_line="$(printf '%s' "$log" | grep -n '^pane state %4$' | tail -1 | cut -d: -f1)"
send_line="$(printf '%s' "$log" | grep -n 'pane send %4 You were resumed' | head -1 | cut -d: -f1)"
assert_ne "" "$idle_line" "the idle wait polls pane state without --json"
[ -n "$idle_line" ] && [ "$idle_line" -lt "$send_line" ]; assert_ok $? "continue is sent only after the idle check passed"

# The SessionStart hook's own readiness signal: once the relaunch line is in
# the log, @anu_generation reads higher than the pre-relaunch value (and
# @anu_session already reads the resumed session, same as every other case
# here) — rotate takes that INSTEAD OF the _wait_running poll. It does not
# replace the idle wait: `cmd_launch` stamps both options itself before it
# execs claude, so the stamps alone never prove claude came up (see the
# bare-shell case below).
: > "$LOG"; : > "$COUNT"
out="$(OPT_GENERATION=1000 OPT_GENERATION_AFTER=2000 "$ACCOUNT" rotate %4)"; rc=$?
assert_ok $rc "rotate succeeds via the hook-stamped readiness path"
assert_contains "$out" "ready via hook" "…and the report names the hook path, not the fallback"
assert_contains "$(cat "$LOG")" "pane send %4 $CONT_TEXT" "…continuation is still sent once ready"

# …but the hook stamps are NOT proof that claude is up. `@ACCOUNT_LAUNCH@ launch`
# stamps @anu_session/@anu_generation itself, before `exec claude`, so a
# relaunch that dies right after the line is typed (box failing before it
# starts claude, a replacement token Claude Code rejects and exits on, a
# crash) satisfies the hook predicate while the pane is still a bare shell —
# which `pane state` reads as `idle`. The continuation must never be typed
# into that shell, and the rotation must never be reported as a success.
: > "$LOG"; : > "$COUNT"
out="$(OPT_GENERATION=1000 OPT_GENERATION_AFTER=2000 PANE_CMD_AFTER=bash "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "hook stamps on a pane still sitting at a bare shell do NOT count as ready"
assert_contains "$out" "did not come back up" "…the wait times out into the relaunch-died path"
assert_contains "$out" "session cccccccc-1111-2222-3333-444444444444" "…naming the session so a human can go look"
assert_not_contains "$(cat "$LOG")" "pane send %4 You were resumed" "…and the continuation is never typed into the shell"
assert_contains "$(cat "$LOG")" "capture-pane -p -t %4" "…with the pane's last lines dumped"
assert_not_contains "$out" "ready via" "…nothing is reported as a rotation"

# the same hook stamps WITH claude actually running are the happy path.
: > "$LOG"; : > "$COUNT"
out="$(OPT_GENERATION=1000 OPT_GENERATION_AFTER=2000 PANE_CMD_AFTER=claude "$ACCOUNT" rotate %4)"; rc=$?
assert_ok $rc "the same hook stamps with claude actually running still rotate"
assert_contains "$(cat "$LOG")" "pane send %4 $CONT_TEXT" "…and the continuation is delivered"

# boxed: a `box claude` pane's foreground process is the container runtime,
# never "claude" — not a shell, so it reads exactly like the happy path.
: > "$LOG"; : > "$COUNT"
out="$(OPT_BOX=1 OPT_GENERATION=1000 OPT_GENERATION_AFTER=2000 PANE_CMD_AFTER=container "$ACCOUNT" rotate %4)"; rc=$?
assert_ok $rc "a boxed relaunch (foreground process 'container') is not a shell — it rotates"
assert_contains "$(cat "$LOG")" "pane send %4 $CONT_TEXT" "…and is continued"

# item 9: @anu_need is the one value interpolated unquoted into the typed
# line, and any process on this machine can set a pane option — anything
# outside any|fable is replaced with `any`, loudly, before anything is typed.
: > "$LOG"; : > "$COUNT"
out="$(OPT_NEED='fable; echo pwned' "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "a pane whose @anu_need is not any|fable still rotates"
assert_contains "$out" "invalid @anu_need" "…with a warning naming the bad stamp"
assert_contains "$(cat "$LOG")" "@ACCOUNT_LAUNCH@ launch --as account_alpha --need any --" "…and the typed line carries the validated need"
assert_not_contains "$(cat "$LOG")" "pwned" "…never the junk the option carried"

# B2 fix round 1, item 11: the pane IS up and confirmed idle at this point —
# a failing `pane send` of the continuation must never read as a silent
# success (the human would think the task resumed on its own).
: > "$LOG"; : > "$COUNT"
out="$(PANE_SEND_FAIL=1 "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "a failing continuation delivery is a hard failure, not silent success"
assert_contains "$out" "continuation not delivered" "…and says so"

# The generation guard: a pane whose @anu_generation changed between the
# facts rotate read before its pick/preflight and the recheck
# _relaunch_and_resume runs right before leaving (someone else relaunched
# it, or its own SessionStart hook fired for an unrelated reason) aborts
# instead of typing a resume line for a session that may already be stale.
# B2's recheck runs BEFORE any leave keystroke, so nothing is typed at all —
# not even Escape/ctrl-c.
: > "$LOG"; : > "$COUNT"
out="$(OPT_GENERATION_DRIFT=1 "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "a pane whose generation changed underneath rotate aborts, not relaunches"
assert_contains "$out" "pane changed under me" "…and says so"
assert_not_contains "$(cat "$LOG")" "@ACCOUNT_LAUNCH@ launch" "…nothing typed: the resume line itself is never sent"
assert_not_contains "$(cat "$LOG")" "send-keys" \
  "…and nothing is typed at all, not even the leave keystrokes — the recheck runs before any of them"
assert_contains "$(cat "$LOG")" "set-option -pu -t %4 @anu_rotating" \
  "…and @anu_rotating (stamped optimistically right before the recheck) is cleared back off again — item 8, _abort_relaunch"

: > "$LOG"; : > "$COUNT"
out="$(OPT_NEED=any PANE_WALL=fable "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "0" "$rc" "an any-pane at a fable wall still rotates (the pane is stuck either way)"
assert_contains "$(cat "$LOG")" "pick?need=fable" "…but asks for fable room, since the wall was fable"

: > "$LOG"; : > "$COUNT"
out="$(OPT_ACCOUNT=account_alpha OPT_NEED=any PANE_WALL=weekly "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "2" "$rc" "nothing has room: exit 2"
assert_contains "$out" "parked" "…and says the pane stays parked"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

# dashboard unreachable during pick is a DIFFERENT failure than "nothing has
# room" — exit 1, not 2, and never touches the pane.
: > "$LOG"; : > "$COUNT"
out="$(CURL_FAIL=1 "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "1" "$rc" "pick failing (dashboard unreachable) is distinct from exit 2 'nothing has room'"
assert_contains "$out" "unreachable" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

out="$(PANE_STATE=idle "$ACCOUNT" rotate %4 2>&1)"; assert_fail $? "not at a wall: refuses"
out="$(OPT_SESSION= "$ACCOUNT" rotate %4 2>&1)"; assert_fail $? "no session stamp: refuses"
assert_contains "$out" "@anu_session" "…and names the missing stamp"

: > "$LOG"; : > "$COUNT"
out="$(OPT_ACCOUNT= "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "rotate refuses a pane with a session but no @anu_account (not anu-launched)"
assert_contains "$out" "@anu_account" "…and names the missing stamp"
assert_contains "$out" "not started by com8-account launch" "…with the same message the missing-session guard uses"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

: > "$LOG"; : > "$COUNT"
out="$(OPT_BOX=1 "$ACCOUNT" rotate %4)"
assert_contains "$(cat "$LOG")" "@ACCOUNT_LAUNCH@ launch --as account_alpha --need fable --box --" "a boxed pane relaunches boxed"

: > "$LOG"; : > "$COUNT"
out="$("$ACCOUNT" rotate agent-2)"; rc=$?
assert_ok $rc "a non-%id target resolves through pane state"
log="$(cat "$LOG")"
assert_contains "$log" "pane state agent-2 --json" "…asking pane state for the target as given"
assert_match "$log" "send-keys -t %4 Escape" "…and acting on the resolved %4 pane"
assert_contains "$out" "account_beta → account_alpha" "…reports the move for the resolved pane"

: > "$LOG"; : > "$COUNT"
out="$("$ACCOUNT" rotate %99 2>&1)"; rc=$?
assert_fail $rc "an unresolvable target fails"
assert_contains "$out" "no such pane" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching anything"

# the relaunch runs but the pane never comes idle (a booting Claude that
# never finishes, or a wedged relaunch) -> times out, never sends "continue",
# names the pane and session so a human can go look.
: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE_AFTER=booting "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "a relaunch that never leaves 'booting' times out"
assert_not_contains "$(cat "$LOG")" "You were resumed on another account" "…continue is never sent"
assert_contains "$out" "%4" "…the message names the pane"
assert_contains "$out" "session cccccccc-1111-2222-3333-444444444444" "…and the session"

# A `--resume` re-renders the transcript, so the walled turn's text is the last
# turn on the fresh pane and `pane state` reads `limited` until a new turn
# exists (B7, live). That is "up", not "still walled": the continuation is
# sent and the rotation is reported.
: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE_AFTER=limited "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "0" "$rc" "a relaunched pane that re-renders the old wall counts as up"
assert_contains "$(cat "$LOG")" "You were resumed on another account" "…and the continuation is sent"
assert_contains "$out" "→" "…and the rotation is reported"

# a manual rotate on a pane already mid-rotation (within the last 120s)
# refuses instead of racing the in-flight one — unless told to --force.
: > "$LOG"; : > "$COUNT"
out="$(OPT_ROTATING=$(date +%s) "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "manual rotate refuses a pane already rotating within 120s"
assert_contains "$out" "already rotating" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

: > "$LOG"; : > "$COUNT"
out="$(OPT_ROTATING=$(date +%s) "$ACCOUNT" rotate %4 --force)"; rc=$?
assert_ok $rc "…but --force overrides"
assert_contains "$out" "account_beta → account_alpha" "…and it rotates"

# `pane` missing from PATH entirely: rotate/switch lean on it for every pane
# read and every keystroke, so it is checked first — otherwise `pane state`
# just fails and the verb reports "no such pane", pointing the reader at the
# pane instead of at their install. PATH here holds NO stubs at all, so
# nothing else can run either; the die must come from the binary check.
NOPANE="$(mktmp)/bin"; mkdir -p "$NOPANE"
: > "$LOG"
out="$(COM8_ACCOUNT_PANE="$NOPANE/pane" PATH="$NOPANE:/usr/bin:/bin" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "rotate without the anu plugin bin on PATH fails"
assert_contains "$out" "configured pane helper" "…naming what is actually missing"
assert_not_contains "$out" "no such pane" "…not masquerading as a missing pane"
out="$(COM8_ACCOUNT_PANE="$NOPANE/pane" PATH="$NOPANE:/usr/bin:/bin" "$ACCOUNT" switch %4 --as account_alpha 2>&1)"; rc=$?
assert_fail $rc "switch without the anu plugin bin on PATH fails the same way"
assert_contains "$out" "configured pane helper" "…naming what is actually missing"

t_section "lock (B2.1)"
# A real mkdir-based lock, held for the whole relaunch — distinct from the
# softer @anu_rotating cooldown exercised above. Isolated per test via a
# fresh ANU_ACCOUNT_LOCKDIR so pre-seeding a lock dir here can't leak into
# any other test in this suite. The active tmux stub answers #{socket_path}
# with a FIXED "/tmp/tmux-test/default" for every pane and every caller env
# (see the display-message case above), so every lock in this section lives
# at "default-4" — never a name derived from THIS process's own $TMUX.
LOCKS="$(mktmp)/locks"
: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/default-4"
printf '%s %s\n' "$BASHPID" "$(date +%s)" > "$LOCKS/default-4/holder"
out="$(ANU_ACCOUNT_LOCKDIR="$LOCKS" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "a pane already locked by a live holder refuses"
assert_contains "$out" "another rotation holds %4" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"
rm -rf "$LOCKS/default-4"

# B2 fix round 1, item 1: the lock name is derived from the TARGET pane's
# own socket (queried via `tmux display-message -t <pane>`), never this
# process's own $TMUX — otherwise `watch` (no $TMUX) and a human inside
# tmux would hold DIFFERENT locks for the very same pane. A lock dir seeded
# under a name derived from a misleading $TMUX is simply irrelevant…
: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/totally-wrong-socket-4"
printf '%s %s\n' "$BASHPID" "$(date +%s)" > "$LOCKS/totally-wrong-socket-4/holder"
out="$(TMUX=/tmp/totally-wrong-socket,999,0 ANU_ACCOUNT_LOCKDIR="$LOCKS" "$ACCOUNT" rotate %4)"; rc=$?
assert_ok $rc "a lock dir named after the CALLER's own \$TMUX is irrelevant — rotate ignores it"
rm -rf "$LOCKS/totally-wrong-socket-4"

# …but the lock at the pane's REAL socket name blocks it, even with no
# caller $TMUX at all (e.g. `watch`, running outside any tmux client).
: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/default-4"
printf '%s %s\n' "$BASHPID" "$(date +%s)" > "$LOCKS/default-4/holder"
out="$(env -u TMUX ANU_ACCOUNT_LOCKDIR="$LOCKS" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "the lock named after the TARGET PANE's real socket blocks it regardless of the caller's own \$TMUX"
assert_contains "$out" "another rotation holds %4" "…and says so"
rm -rf "$LOCKS/default-4"

# A stale lock (>10 min old, holder pid no longer alive) is reclaimed
# automatically instead of wedging the pane forever, and the lock dir is
# gone again once the rotation completes — never left wedged.
: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/default-4"
printf '999999 %s\n' "$(( $(date +%s) - 700 ))" > "$LOCKS/default-4/holder"
out="$(ANU_ACCOUNT_LOCKDIR="$LOCKS" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "a stale lock (>10min old, dead holder pid) is reclaimed"
assert_contains "$out" "account_beta → account_alpha" "…and rotation proceeds normally"
[ ! -d "$LOCKS/default-4" ]; assert_ok $? "…and the lock dir is gone again once the rotation completes"

# A lock young enough but with a dead pid is NOT reclaimed (only the >10min
# rule frees it) — refuses just like the live-holder case above.
: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/default-4"
printf '999999 %s\n' "$(date +%s)" > "$LOCKS/default-4/holder"
out="$(ANU_ACCOUNT_LOCKDIR="$LOCKS" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "a young lock is not reclaimed even with a dead holder pid"
assert_contains "$out" "another rotation holds %4" "…and says so"
rm -rf "$LOCKS/default-4"

# B2 fix round 1, item 3: a holder-less lock dir (crashed before it could
# even write the holder file) falls back to the DIRECTORY's own mtime for
# the same >10min age rule.
: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/default-4"
old_stamp="$(date -v-15M +%Y%m%d%H%M.%S 2>/dev/null || date -d '-15 minutes' +%Y%m%d%H%M.%S)"
touch -t "$old_stamp" "$LOCKS/default-4"
out="$(ANU_ACCOUNT_LOCKDIR="$LOCKS" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "a holder-less lock dir older than 10 minutes (by its own mtime) is reclaimed"
assert_contains "$out" "account_beta → account_alpha" "…and rotation proceeds normally"

: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/default-4"
out="$(ANU_ACCOUNT_LOCKDIR="$LOCKS" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "…but a FRESH holder-less lock dir (just created) is not reclaimed"
assert_contains "$out" "another rotation holds %4" "…and says so"
rm -rf "$LOCKS/default-4"

# switch acquires the same lock (before reading any pane fact) too.
: > "$LOG"; : > "$COUNT"
mkdir -p "$LOCKS/default-4"
printf '%s %s\n' "$BASHPID" "$(date +%s)" > "$LOCKS/default-4/holder"
out="$(ANU_ACCOUNT_LOCKDIR="$LOCKS" PANE_STATE=idle "$ACCOUNT" switch %4 --as gmail 2>&1)"; rc=$?
assert_fail $rc "switch refuses a pane already locked by a live holder too"
assert_contains "$out" "another rotation holds %4" "…and says so"
rm -rf "$LOCKS/default-4"

# The lock is released even when the relaunch aborts partway through — e.g.
# the generation-drift guard (nothing typed at all) — never left wedged for
# a rotation that never actually touched the pane.
: > "$LOG"; : > "$COUNT"
out="$(ANU_ACCOUNT_LOCKDIR="$LOCKS" OPT_GENERATION_DRIFT=1 "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "a mid-relaunch abort (generation drift) still fails as before"
[ ! -d "$LOCKS/default-4" ]; assert_ok $? "…and the lock dir is gone afterward too, not left wedged"

# A REAL two-process race: exactly one of two truly concurrent lock
# attempts on the SAME pane wins — an actual fork/exec race (not a
# single-process simulation), via the hidden test-only __test_lock_hold
# verb (gated on ANU_ACCOUNT_TEST=1 — see B2 fix round 2, item 3). Uses the
# real `sleep` (not the "exit 0" stub used everywhere else in this suite)
# so the two attempts genuinely overlap in wall-clock time.
: > "$LOG"; : > "$COUNT"
rm -f "$SD/sleep"
RACE_LOCKS="$(mktmp)/locks"
race1="$(mktmp)/race1"; race2="$(mktmp)/race2"
( ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_LOCKDIR="$RACE_LOCKS" "$ACCOUNT" __test_lock_hold %4 1 > "$race1" 2>&1; echo $? >> "$race1" ) &
( ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_LOCKDIR="$RACE_LOCKS" "$ACCOUNT" __test_lock_hold %4 1 > "$race2" 2>&1; echo $? >> "$race2" ) &
wait
r1_out="$(sed -n '1p' "$race1")"; r1_rc="$(sed -n '2p' "$race1")"
r2_out="$(sed -n '1p' "$race2")"; r2_rc="$(sed -n '2p' "$race2")"
one_wins=0
if { [ "$r1_out" = locked ] && [ "$r1_rc" = 0 ] && [ "$r2_out" = busy ] && [ "$r2_rc" = 1 ]; } || \
   { [ "$r2_out" = locked ] && [ "$r2_rc" = 0 ] && [ "$r1_out" = busy ] && [ "$r1_rc" = 1 ]; }; then
  one_wins=1
fi
[ "$one_wins" = 1 ]
assert_ok $? "exactly one of two truly concurrent lock attempts on the same pane wins (r1=$r1_out/$r1_rc r2=$r2_out/$r2_rc)"

# The __test_lock_hold verb itself refuses outside a test run.
out="$(env -u ANU_ACCOUNT_TEST "$ACCOUNT" __test_lock_hold %4 2>&1)"; rc=$?
assert_fail $rc "__test_lock_hold refuses without ANU_ACCOUNT_TEST=1"
assert_contains "$out" "test-only" "…and says so"
out="$(ANU_ACCOUNT_TEST=1 "$ACCOUNT" __test_lock_hold 2>&1)"; rc=$?
assert_fail $rc "__test_lock_hold refuses with no pane argument (not a raw set -u error)"
assert_contains "$out" "usage:" "…and prints usage, not a bash internal error"

# B2 fix round 2, item 1: a DETERMINISTIC two-process race at a STALE lock,
# reproducing the exact bug the reviewer found — A reclaims and re-locks
# first; B, paused (ANU_ACCOUNT_TEST_PAUSE_AFTER_STALE_CHECK) between its
# OWN staleness verdict and its OWN `mv`, wakes only after A's fresh lock
# is already in place, so B's `mv` grabs A's LIVE directory (the path
# exists again — `mv` doesn't know who owns it). Without the fix-round-2
# re-validation, B would then discard A's fresh holder file and relock
# over it too — two winners on one pane. With the fix, B must notice the
# holder line it moved away no longer matches the stale one it judged, put
# it back, and lose honestly — so A's own holder file must survive intact.
rm -f "$SD/sleep"
DET_LOCKS="$(mktmp)/locks"
mkdir -p "$DET_LOCKS/default-4"
printf '999999 %s\n' "$(( $(date +%s) - 700 ))" > "$DET_LOCKS/default-4/holder"
detA="$(mktmp)/detA"; detB="$(mktmp)/detB"
# Synchronize at the EXISTING test hook via the fixture's sleep command.
# Starting A and B together is insufficient: B can win the initial mkdir
# during A's rename-away gap and never reach its stale-check pause at all.
# B must have judged the old lock stale BEFORE A starts, and A must still
# hold its fresh lock until B's result and the restored holder are checked.
DET_GATE="$(mktmp)/barrier"; mkdir "$DET_GATE"
export DET_GATE REAL_SLEEP
_det_wait() {
  local n
  for ((n=0; n<1000; n++)); do
    [ -f "$1" ] && return 0
    "$REAL_SLEEP" 0.01
  done
  return 1
}
apid=""; bpid=""
_det_cleanup() {
  # Release only these two bounded fixture barriers, then reap our children
  # before the enclosing harness removes their directories. No signals.
  touch "$DET_GATE/release-B" "$DET_GATE/release-A"
  [ -z "$bpid" ] || wait "$bpid" 2>/dev/null || true
  [ -z "$apid" ] || wait "$apid" 2>/dev/null || true
}
trap '_det_cleanup; _anu_cleanup' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
stub "$SD" sleep '
case "$1" in
  stale-barrier) side=B ;;
  holder-barrier) side=A ;;
  *) exec "$REAL_SLEEP" "$@" ;;
esac
: > "$DET_GATE/ready-$side"
for ((n=0; n<4000; n++)); do
  [ -f "$DET_GATE/release-$side" ] && exit 0
  "$REAL_SLEEP" 0.01
done
echo "fixture barrier timed out: $side" >&2
exit 99'
( ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_TEST_PAUSE_AFTER_STALE_CHECK=stale-barrier ANU_ACCOUNT_LOCKDIR="$DET_LOCKS" "$ACCOUNT" __test_lock_hold %4 0.2 > "$detB" 2>&1; echo $? >> "$detB" ) &
bpid=$!
_det_wait "$DET_GATE/ready-B"; b_ready=$?
assert_ok "$b_ready" "B reaches its stale verdict before A starts"
fresh_holder=""
if [ "$b_ready" = 0 ]; then
  ( ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_LOCKDIR="$DET_LOCKS" "$ACCOUNT" __test_lock_hold %4 holder-barrier > "$detA" 2>&1; echo $? >> "$detA" ) &
  apid=$!
  _det_wait "$DET_GATE/ready-A"; a_ready=$?
  assert_ok "$a_ready" "A holds its fresh lock before B is released"
  fresh_holder="$(cat "$DET_LOCKS/default-4/holder" 2>/dev/null)"
  assert_match "$fresh_holder" '^[0-9]+ [0-9]+$' "A published a complete holder line"
fi
touch "$DET_GATE/release-B"
wait "$bpid"
detB_out="$(sed -n '1p' "$detB")"; detB_rc="$(sed -n '2p' "$detB")"
assert_eq "busy" "$detB_out" "the paused racer (B), waking after A already re-locked, loses honestly"
assert_eq "1" "$detB_rc" "…exit 1, not a second winner"
# A stays at its barrier while B exits and we inspect the restored lock.
assert_file "$DET_LOCKS/default-4/holder" "A's holder file survives — B restored it instead of discarding it"
assert_eq "$fresh_holder" "$(cat "$DET_LOCKS/default-4/holder" 2>/dev/null)" "…and it is exactly A's fresh holder line"
_det_cleanup
trap _anu_cleanup EXIT
trap - INT TERM
unset -f _det_wait _det_cleanup
unset DET_GATE
stub "$SD" sleep 'exit 0'
detA_out="$(sed -n '1p' "$detA")"; detA_rc="$(sed -n '2p' "$detA")"
assert_eq "locked" "$detA_out" "the non-paused racer (A) wins the stale lock"
assert_eq "0" "$detA_rc" "…exit 0"

t_section "preflight before leaving A (B2.3)"
# A ranked list of three named candidates; "nope" has no locally-resolvable
# token, so a plain preflight walk should skip it and land on "account_alpha".
# Isolated cache + curl fixture so the suite-wide anu-secrets/pick stubs are
# untouched for every other test.
cat > "$FIX/pick-multi.json" <<'JSON'
{"need":"any","picks":[{"id":"claude-nope","name":"nope","email":"nope@x","resetsAt":"2026-09-14T01:00:00Z","remaining":90,"shared":false},
 {"id":"claude-account_alpha","name":"account_alpha","email":"alpha@example.invalid","resetsAt":"2026-09-14T02:00:00Z","remaining":50,"shared":false},
 {"id":"claude-gmail","name":"gmail","email":"g@x","resetsAt":"2026-09-14T03:00:00Z","remaining":40,"shared":false}],
 "out":[],"generatedAt":"2026-09-14T00:00:00Z"}
JSON
stub "$SD" curl '
url="${@: -1}"; printf "curl %s\n" "$url" >> "'"$LOG"'"
[ -n "${CURL_FAIL:-}" ] && exit 7
case "$url" in
  *"/api/usage/pick?need=any"*)   cat "'"$FIX"'/pick-multi.json" ;;
  *"/api/usage/pick?need=fable"*) cat "'"$FIX"'/pick-fable.json" ;;
  *"/api/usage")                  cat "'"$FIX"'/usage.json" ;;
  *) exit 22 ;;
esac'
stub "$SD" anu-secrets '
printf "anu-secrets %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  get)
    case "$3" in
      ACCOUNT_ALPHA) echo "sk-ant-oat01-account_alpha"; exit 0 ;;
      GMAIL)    echo "sk-ant-oat01-gmail"; exit 0 ;;
      *) exit 1 ;;
    esac ;;
  set)   v="$(cat)"; echo "$2/$3 set" ;;
  mkdir) echo "ok $2" ;;
  env)   echo "{}" ;;
  *) exit 1 ;;
esac'
PF_CACHE="$(mktmp)/tokens.json"

: > "$LOG"; : > "$COUNT"
out="$(ANU_ACCOUNT_CACHE="$PF_CACHE" OPT_NEED=any PANE_WALL=weekly "$ACCOUNT" rotate %4)"; rc=$?
assert_ok $rc "rotate skips a ranked candidate with no locally-resolvable token"
assert_contains "$out" "account_beta → account_alpha" "…and lands on the first candidate that actually has one (nope has none)"
assert_contains "$(cat "$LOG")" "tmux set-option -p -t %4 @anu_tried account_beta:" "…and appends the LEFT account to @anu_tried, stamped with the current epoch"

# an already-tried-recently candidate is skipped even though it has a token
: > "$LOG"; : > "$COUNT"
out="$(ANU_ACCOUNT_CACHE="$PF_CACHE" OPT_NEED=any PANE_WALL=weekly OPT_TRIED="account_alpha:$(date +%s)" "$ACCOUNT" rotate %4)"; rc=$?
assert_ok $rc "an already-tried candidate is skipped"
assert_contains "$out" "account_beta → gmail" "…landing on the next ranked candidate instead"

# an @anu_tried entry older than 30 minutes no longer excludes the candidate
: > "$LOG"; : > "$COUNT"
old_epoch=$(( $(date +%s) - 2000 ))
out="$(ANU_ACCOUNT_CACHE="$PF_CACHE" OPT_NEED=any PANE_WALL=weekly OPT_TRIED="account_alpha:$old_epoch" "$ACCOUNT" rotate %4)"; rc=$?
assert_contains "$out" "account_beta → account_alpha" "an @anu_tried entry older than 30 minutes no longer excludes the candidate"

# every ranked candidate exhausted (no token, or already tried) exits 2 and
# leaves the pane parked, same as an empty pick.
: > "$LOG"; : > "$COUNT"
tried_all="nope:$(date +%s) account_alpha:$(date +%s) gmail:$(date +%s)"
out="$(ANU_ACCOUNT_CACHE="$PF_CACHE" OPT_NEED=any PANE_WALL=weekly OPT_TRIED="$tried_all" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "2" "$rc" "every ranked candidate exhausted by preflight exits 2"
assert_contains "$out" "parked" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

# @anu_tried stays bounded at 3 entries — the oldest is dropped.
: > "$LOG"; : > "$COUNT"
out="$(ANU_ACCOUNT_CACHE="$PF_CACHE" OPT_NEED=any PANE_WALL=weekly OPT_TRIED="a:1000 b:2000 c:3000" "$ACCOUNT" rotate %4)"; rc=$?
newval="$(grep '@anu_tried' "$LOG" | tail -1)"
assert_contains "$newval" "b:2000" "…keeps the second-oldest of the pre-existing entries"
assert_contains "$newval" "c:3000" "…keeps the newest of the pre-existing entries"
assert_not_contains "$newval" "a:1000" "…and drops the oldest, bounding the list at 3"
cnt="$(printf '%s' "$newval" | sed "s/.*@anu_tried //" | wc -w | tr -d ' ')"
assert_eq "3" "$cnt" "@anu_tried never grows past 3 entries"

# a dashboard failure during preflight is still distinguishable from "nothing has room"
: > "$LOG"; : > "$COUNT"
out="$(ANU_ACCOUNT_CACHE="$PF_CACHE" CURL_FAIL=1 OPT_NEED=any PANE_WALL=weekly "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "1" "$rc" "a dashboard failure during preflight is exit 1, not 2"
assert_contains "$out" "unreachable" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

# switch's own (no --as) picking path runs the same preflight.
: > "$LOG"; : > "$COUNT"
out="$(ANU_ACCOUNT_CACHE="$PF_CACHE" OPT_NEED=any PANE_STATE=idle "$ACCOUNT" switch %4)"; rc=$?
assert_ok $rc "switch's own picking path also preflights"
assert_contains "$out" "account_beta → account_alpha" "…skipping the token-less candidate the same way rotate does"

# item 7: the store fallback inside _token_for is BOUNDED. `anu-secrets get`
# is a network call to Infisical, made once per ranked candidate during the
# preflight walk — an unreachable store or a wedged CLI would otherwise park
# the rotation for ever while it holds the pane's lock. A timeout reads
# exactly like a miss ("no token locally") and the walk moves on.
stub "$SD" anu-secrets '
printf "anu-secrets %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  get)
    case "$3" in
      NOPE)     exec "'"$REAL_SLEEP"'" 30 ;;
      ACCOUNT_ALPHA) echo "sk-ant-oat01-account_alpha"; exit 0 ;;
      GMAIL)    echo "sk-ant-oat01-gmail"; exit 0 ;;
      *) exit 1 ;;
    esac ;;
  set)   v="$(cat)"; echo "$2/$3 set" ;;
  mkdir) echo "ok $2" ;;
  env)   echo "{}" ;;
  *) exit 1 ;;
esac'
: > "$LOG"; : > "$COUNT"
t0=$SECONDS
out="$(ANU_ACCOUNT_CACHE="$(mktmp)/tokens.json" ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_TEST_TOKEN_TIMEOUT=1 \
       OPT_NEED=any PANE_WALL=weekly "$ACCOUNT" rotate %4)"; rc=$?
el=$((SECONDS - t0))
assert_ok $rc "a hung anu-secrets during preflight does not park the rotation"
assert_contains "$out" "account_beta → account_alpha" "…the bounded lookup counts as 'no token locally' and the walk moves to the next candidate"
[ "$el" -lt 15 ]; assert_ok $? "…giving up on the bound instead of waiting out the store (took ${el}s, the stub blocks for 30)"

# restore the suite-wide fixtures/stubs for every test below.
stub "$SD" curl '
url="${@: -1}"; printf "curl %s\n" "$url" >> "'"$LOG"'"
[ -n "${CURL_FAIL:-}" ] && exit 7
case "$url" in
  *"/api/usage/pick?need=fable"*) cat "'"$FIX"'/pick-fable.json" ;;
  *"/api/usage/pick?need=any"*)   cat "'"$FIX"'/pick-empty.json" ;;
  *"/api/usage")                  cat "'"$FIX"'/usage.json" ;;
  *) exit 22 ;;
esac'
stub "$SD" anu-secrets '
printf "anu-secrets %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  get)
    case "$3" in
      ACCOUNT_ALPHA) echo "sk-ant-oat01-account_alpha"; exit 0 ;;
      ACCOUNT_BETA)     echo "sk-ant-oat01-account_beta"; exit 0 ;;
      *) exit 1 ;;
    esac ;;
  set)   v="$(cat)"; printf "set-len %s\n" "${#v}" >> "'"$LOG"'"; echo "$2/$3 set" ;;
  mkdir) echo "ok $2" ;;
  env)   echo "{\"ACCOUNT_ALPHA\":\"sk-ant-oat01-account_alpha\",\"ACCOUNT_GAMMA\":\"sk-ant-oat01-account_gamma\"}" ;;
  *) exit 1 ;;
esac'

t_section "pre-leave state recheck (B2.2)"
# rotate: the recheck right before leaving must still see a wall — a race
# where the pane recovered between the initial check and the relaunch stops
# rotate cold, with nothing typed.
: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE_RECHECK=idle "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "rotate's pre-leave recheck catches a pane no longer at a wall"
assert_contains "$out" "no longer at a wall" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…nothing typed"

# switch (no --force): the recheck must still see idle or limited.
: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=idle PANE_STATE_RECHECK=busy "$ACCOUNT" switch %4 --as account_alpha 2>&1)"; rc=$?
assert_fail $rc "switch's pre-leave recheck catches a pane that became busy in the meantime"
assert_contains "$out" "%4 is now busy" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…nothing typed"

# switch --force skips the recheck entirely — the human already forced it.
: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=idle PANE_STATE_RECHECK=dead "$ACCOUNT" switch %4 --as account_alpha --force)"; rc=$?
assert_ok $rc "switch --force skips the pre-leave recheck entirely"
assert_contains "$out" "account_beta → account_alpha" "…and relaunches anyway"

t_section "switch"
# reuses the rotate section's pane/tmux/sleep stubs above — same PANE_STATE/
# OPT_* knobs, same %4/%99 wiring. Unlike rotate, switch needs no wall, but
# by default it only moves an idle or limited pane (busy/approval/booting/
# dead/unknown all refuse unless --force) — the stub's own default
# PANE_STATE (limited) and OPT_ACCOUNT (account_beta) are fine as they are unless a
# case below overrides them. And unlike rotate, switch continues the
# session afterward ONLY when the pane was limited or --continue was
# passed — an idle pane is reopened and left idle. When it DOES pick (no
# --as), switch threads .wall through exactly like rotate: a limited pane
# at a fable wall picks for fable regardless of its own stamped need; any
# other state (no wall to read) falls back to that stamped need.

# B2 fix round 1, item 5: switch --as must preflight the named account too
# (the default stub resolves only ACCOUNT_ALPHA/ACCOUNT_BETA) — nothing typed for one
# this device can't even authenticate as.
: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=idle "$ACCOUNT" switch %4 --as gmail 2>&1)"; rc=$?
assert_eq "1" "$rc" "switch --as an account with no local token fails, exit 1"
assert_contains "$out" "no token for gmail" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…nothing typed"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=idle "$ACCOUNT" switch %4 --as account_alpha)"; rc=$?
assert_ok $rc "switch --as exits 0 on an idle pane"
log="$(cat "$LOG")"
assert_contains "$log" "send-keys -t %4 printf '\033[2J\033[3J\033[H' && @ACCOUNT_LAUNCH@ launch --as account_alpha --need fable -- --resume cccccccc-1111-2222-3333-444444444444 --dangerously-skip-permissions C-m" \
  "switch --as types the relaunch line under the named account"
assert_not_contains "$log" "pane send %4 $CONT_TEXT" "…an idle pane is reopened and left idle — no continue is sent"
assert_contains "$out" "account_beta → account_alpha" "…reporting the move"
assert_contains "$out" "switch, session cccccccc-1111-2222-3333-444444444444" "…naming it a switch, not a wall rotation"
assert_contains "$out" "ready via fallback" "…switch reports its readiness path too, same as rotate"
assert_not_contains "$log" "pick?need=" "--as skips the dashboard pick entirely"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=idle "$ACCOUNT" switch %4 --as account_alpha --continue)"; rc=$?
assert_ok $rc "switch --as --continue exits 0 on an idle pane"
assert_contains "$(cat "$LOG")" "pane send %4 $CONT_TEXT" "…--continue sends the continuation line even though the pane was idle"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=limited "$ACCOUNT" switch %4)"; rc=$?
assert_ok $rc "switch with no --as exits 0 on a limited pane"
log="$(cat "$LOG")"
assert_contains "$log" "pick?need=fable&fresh=1&exclude=account_beta" "no --as: picks via the dashboard, excluding the current account"
assert_contains "$out" "account_beta → account_alpha" "…and reports the picked move"
assert_contains "$log" "pane send %4 $CONT_TEXT" "…a limited pane (mid-task, walled) is continued, same as rotate"

: > "$LOG"; : > "$COUNT"
out="$(OPT_NEED=any PANE_WALL=fable "$ACCOUNT" switch %4)"; rc=$?
assert_ok $rc "switch on a limited pane at a fable wall exits 0 even though the stamped need is any"
assert_contains "$(cat "$LOG")" "pick?need=fable&fresh=1&exclude=account_beta" \
  "…switch threads the wall through exactly like rotate: a fable wall picks for fable regardless of the pane's own stamped need"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=idle OPT_NEED=any PANE_WALL=fable "$ACCOUNT" switch %4)"; rc=$?
# the pick-empty fixture (served for need=any) has no named picks, so this
# is expected to report "nothing has room" — the point of the test is the
# QUERY switch sent, not the pick result.
assert_eq "2" "$rc" "switch on an idle pane at a fable wall still asks for need=any (nothing has room in this fixture for 'any')"
assert_contains "$(cat "$LOG")" "pick?need=any&fresh=1&exclude=account_beta" \
  "…an idle pane has no wall to read (it isn't walled) — the pick falls back to the pane's own stamped need, not the fable wall"
assert_contains "$out" "nothing has room for any" "…and says so"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=busy "$ACCOUNT" switch %4 2>&1)"; rc=$?
assert_fail $rc "switch refuses a busy pane (a mid-generation pane is not interrupted by accident)"
assert_contains "$out" "busy" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=busy "$ACCOUNT" switch %4 --force)"; rc=$?
assert_ok $rc "…but --force proceeds on a busy pane"
assert_contains "$out" "account_beta → account_alpha" "…and it switches"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=approval "$ACCOUNT" switch %4 2>&1)"; rc=$?
assert_fail $rc "switch refuses a pane awaiting approval by default"
assert_contains "$out" "approval" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…nothing typed"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=approval "$ACCOUNT" switch %4 --force)"; rc=$?
assert_ok $rc "…but --force proceeds on a pane awaiting approval"
assert_contains "$out" "account_beta → account_alpha" "…and it switches"

: > "$LOG"; : > "$COUNT"
out="$(PANE_STATE=booting "$ACCOUNT" switch %4 2>&1)"; rc=$?
assert_fail $rc "switch refuses a booting pane by default too — only idle/limited move without --force"
assert_contains "$out" "booting" "…and says so"

: > "$LOG"; : > "$COUNT"
out="$("$ACCOUNT" switch %4 --as account_beta)"; rc=$?
assert_ok $rc "naming the pane's own current account is a no-op, exit 0"
assert_contains "$out" "already on account_beta" "…and says so"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

: > "$LOG"; : > "$COUNT"
out="$(OPT_ROTATING=$(date +%s) "$ACCOUNT" switch %4 --as account_beta)"; rc=$?
assert_ok $rc "the same-account no-op check runs BEFORE the rotating-cooldown guard — switch --as <current> is still a no-op mid-cooldown"
assert_contains "$out" "already on account_beta" "…and says so, not 'already rotating'"
assert_not_contains "$out" "already rotating" "…the cooldown guard is never reached"
assert_not_contains "$(cat "$LOG")" "send-keys" "…without touching the pane"

: > "$LOG"; : > "$COUNT"
out="$(OPT_ROTATING=$(date +%s) "$ACCOUNT" switch %4 --as account_beta --force)"; rc=$?
assert_ok $rc "…but --force turns the same-account no-op into a real relaunch"
assert_contains "$out" "account_beta → account_beta" "…relaunching under the same account"
assert_contains "$(cat "$LOG")" "send-keys" "…and this time the pane is actually touched"

: > "$LOG"; : > "$COUNT"
out="$(OPT_SESSION= "$ACCOUNT" switch %4 --as gmail 2>&1)"; rc=$?
assert_fail $rc "switch refuses without @anu_session"
assert_contains "$out" "@anu_session" "…and names the missing stamp"

# The live incident: a PATH with /usr/bin ahead of Homebrew's bin hands the bin
# macOS's /bin/bash 3.2, and switch died on `BASHPID: unbound variable` inside
# _lock_pane. Run by 3.2 explicitly, it must re-exec under bash 4+ and move the
# pane exactly as above — default candidates, so this is the real Homebrew bash.
if /bin/bash -c '[ "${BASH_VERSINFO[0]}" -lt 4 ]' 2>/dev/null; then
  : > "$LOG"; : > "$COUNT"
  out="$(PANE_STATE=idle /bin/bash "$ACCOUNT" switch %4 --as account_alpha 2>&1)"; rc=$?
  assert_ok $rc "switch run by /bin/bash 3.2 re-execs under bash 4+ and exits 0"
  assert_not_contains "$out" "BASHPID" "…never reaching \$BASHPID under 3.2"
  assert_contains "$out" "account_beta → account_alpha" "…and moves the pane"
  assert_contains "$(cat "$LOG")" "@ACCOUNT_LAUNCH@ launch --as account_alpha" "…typing the relaunch line under the named account"
fi

t_section "rebalance"
# An IDLE pane moves to the ranked best account when the best is clearly
# better. Everything here runs the REAL bin — the rule, every guard, the
# ranking cache, the preflight fall-through, the dry-run table and (for the
# move itself) the real `switch`, which the verb runs as its own process —
# against stubs of its own: a tmux with a SERVER-option store (so the ranking
# cache really round-trips), one-call pane facts, a fleet listing, clients;
# and a `pane state` whose second plain read can differ from its first (the
# race guard).
RB="$(mktmp)"; RBSTORE="$RB/gopts"; RBCNT="$RB/statecount"; RBCURL="$RB/curl.log"
mkdir -p "$RBSTORE"; : > "$RBCNT"; : > "$RBCURL"
# The clock is pinned (a gated test knob) to real time at the start of the
# section, never ahead of it: `switch`, run by the verb, reads the real clock.
NOW="$(date +%s)"
export ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_TEST_NOW="$NOW"
# A cache of its own: an earlier section's `sync cache` may have warmed names
# this one relies on NOT resolving (gmail has no token anywhere here).
export ANU_ACCOUNT_CACHE="$RB/tokens.json"
# The dashboard's own shapes: Claude windows carry microseconds and an offset,
# Codex ones milliseconds and a Z. jq's fromdateiso8601 reads neither.
iso_c() { date -u -r "$1" +%Y-%m-%dT%H:%M:%S.474272+00:00 2>/dev/null || date -u -d "@$1" +%Y-%m-%dT%H:%M:%S.474272+00:00; }
iso_x() { date -u -r "$1" +%Y-%m-%dT%H:%M:%S.000Z 2>/dev/null || date -u -d "@$1" +%Y-%m-%dT%H:%M:%S.000Z; }
# One pick. $1 name, $2 remaining, $3 reset epoch ("null" for none), $4 x = the Codex shape.
pick() {
  local r
  if [ "$3" = null ]; then r=null
  elif [ "${4:-}" = x ]; then r="\"$(iso_x "$3")\""
  else r="\"$(iso_c "$3")\""; fi
  printf '{"id":"claude-%s","name":"%s","email":"%s@x.com","resetsAt":%s,"remaining":%s,"shared":false}' "$1" "$1" "$1" "$r" "$2"
}
# The ranking the dashboard serves this case — and a clean slate: no cached
# ranking, no curl log, no state reads, no call log.
ranking() {
  local first=1
  { printf '{"need":"any","picks":['
    while [ $# -gt 0 ]; do [ "$first" = 1 ] || printf ','; first=0; printf '%s' "$1"; shift; done
    printf '],"out":[],"generatedAt":"2026-09-23T00:00:00Z"}'; } > "$FIX/pick-reb.json"
  rm -f "$RBSTORE"/* 2>/dev/null; : > "$RBCNT"; : > "$RBCURL"; : > "$LOG"; : > "$COUNT"
}
# A field of the %4 row (the table is aligned with spaces; no field has one).
row()     { printf '%s\n' "$1" | awk -v p="${3:-%4}" -v n="$2" '$1 == p {print $n; exit}'; }
best()    { row "$1" 4 "${2:-%4}"; }
reason()  { row "$1" 5 "${2:-%4}"; }
outcome() { row "$1" 6 "${2:-%4}"; }
curls()   { grep -c . "$RBCURL" | tr -d ' '; }
dry()     { "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null; }

stub "$SD" curl '
url="${@: -1}"; printf "%s\n" "$url" >> "'"$RBCURL"'"; printf "curl %s\n" "$url" >> "'"$LOG"'"
[ -n "${CURL_FAIL:-}" ] && exit 7
case "$url" in
  *"/api/usage/pick?need="*) cat "'"$FIX"'/pick-reb.json" ;;
  *) exit 22 ;;
esac'
stub "$SD" tmux '
printf "tmux %s\n" "$*" >> "'"$LOG"'"
store="'"$RBSTORE"'"
t=""; prev=""; for a in "$@"; do [ "$prev" = -t ] && t="$a"; prev="$a"; done
case "$1" in
  show-options)
    if [ "$2" = -gqv ]; then
      case "$3" in
        @anu_rebalance) printf "%s\n" "${OPT_REBGATE-}" ;;
        *) if [ -f "$store/$3" ]; then cat "$store/$3"; echo; else echo; fi ;;
      esac
      exit 0
    fi
    case "$*" in
      *@anu_account*)    echo "${OPT_ACCOUNT-account_beta}" ;;
      *@anu_session*)    echo "${OPT_SESSION-cccccccc-1111-2222-3333-444444444444}" ;;
      *@anu_need*)       echo "${OPT_NEED-any}" ;;
      *@anu_launch*)     echo "--dangerously-skip-permissions" ;;
      *@anu_box*)        echo "${OPT_BOX-0}" ;;
      *@anu_rotating*)   echo "${OPT_ROTATING-}" ;;
      *@anu_provider*)   echo "${OPT_PROVIDER-}" ;;
      *@anu_generation*) echo 1000 ;;
      *) echo ;;
    esac ;;
  set-option) [ "$2" = -g ] && printf "%s" "$4" > "$store/$3" ;;
  display-message)
    case "$*" in
      *"#{@anu_account}|"*)
        v=""; eval "v=\${RB_FACTS_${t#%}-}"
        if [ -n "$v" ]; then printf "%s\n" "$v"
        else printf "%s|%s|%s|%s|%s|%s|%s|%s|%s\n" "${OPT_ACCOUNT-account_beta}" "${OPT_PROVIDER-}" "${OPT_NEED-any}" \
          "${OPT_SESSION-cccccccc-1111-2222-3333-444444444444}" "${OPT_BOX-0}" "${OPT_REBAT-}" "${OPT_ROTATING-}" \
          "${RB_CMD-2.1.281}" "'"$RB"'"; fi ;;
      *socket_path*) echo "/tmp/tmux-test/default" ;;
      *pane_current_path*) echo "'"$RB"'" ;;
      *pane_current_command*)
        n="$(grep -c "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'")"
        if [ "$n" -ge 2 ]; then echo "${RB_CMD_AFTER2-claude}"
        elif [ "$n" -ge 1 ]; then echo "${RB_CMD_AFTER-claude}"
        else c=$(wc -l < "'"$COUNT"'"); echo x >> "'"$COUNT"'"; [ "$c" -ge 2 ] && echo bash || echo claude; fi ;;
      *) echo ;;
    esac ;;
  list-panes)   printf "%s\n" "${RB_PANES-%4|2.1.281|account_beta|0}" ;;
  list-clients) printf "%s\n" "${RB_CLIENTS-}" ;;
  capture-pane) grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'" && echo "❯ " || echo "$ " ;;
esac
exit 0'
stub "$SD" pane '
printf "pane %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  state)
    shift; json=0; t=""
    for a in "$@"; do case "$a" in --json) json=1 ;; *) [ -z "$t" ] && t="$a" ;; esac; done
    [ "$t" = "%99" ] && exit 1
    case "$t" in %*) p="$t" ;; *) p="%4" ;; esac
    if grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'"; then st=idle
    elif [ "$json" = 1 ]; then st="${RB_STATE_JSON:-idle}"
    else
      n=0; [ -r "'"$RBCNT"'" ] && n="$(cat "'"$RBCNT"'")"
      case "$n" in ""|*[!0-9]*) n=0 ;; esac
      printf "%s" "$((n+1))" > "'"$RBCNT"'"
      if [ "$n" = 0 ]; then st="${RB_STATE:-idle}"; else st="${RB_STATE2:-${RB_STATE:-idle}}"; fi
    fi
    if [ "$json" = 1 ]; then echo "{\"pane\":\"$p\",\"state\":\"$st\",\"cli\":\"claude\"}"; else echo "$st"; fi ;;
  send) exit 0 ;;
esac'

# --- the rule: two conditions, and the boundaries that define them ----------
# (1) an imminent wall: the current account has <= 15 left, the best >= 30.
# Identical resets, so (2) cannot be what fires.
ranking "$(pick account_alpha 30 $((NOW + 200000)))" "$(pick account_beta 15 $((NOW + 200000)))"
out="$("$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"; rc=$?
assert_ok $rc "rebalance --dry-run exits 0"
assert_match "$(printf '%s' "$out" | head -1)" "^PANE +PROVIDER +ACCOUNT +BEST +REASON +OUTCOME$" "…under the header the brief names"
assert_eq "imminent-wall" "$(reason "$out")" "C at 15 left, the best at 30: an imminent wall"
assert_eq "would-move" "$(outcome "$out")" "…and --dry-run only says it would move"
assert_eq "account_alpha" "$(best "$out")" "…naming the destination"
assert_eq "claude" "$(row "$out" 2)" "…on the pane's provider (absent @anu_provider is claude)"
assert_eq "account_beta" "$(row "$out" 3)" "…from the pane's account"
assert_not_contains "$(cat "$LOG")" "send-keys" "…and touches nothing"
assert_not_contains "$(cat "$LOG")" "@anu_rebalance_seen" "…writes nothing to the pane, not even its memo"
ranking "$(pick account_alpha 90 $((NOW + 200000)))" "$(pick account_beta 16 $((NOW + 200000)))"
assert_eq "no-condition" "$(reason "$(dry)")" "16 left is not an imminent wall — the line is <= 15"
ranking "$(pick account_alpha 29 $((NOW + 200000)))" "$(pick account_beta 15 $((NOW + 200000)))"
assert_eq "no-condition" "$(reason "$(dry)")" "…and a best with 29 left is not room — the line is >= 30"
# (2) a sooner weekly reset: 24h exactly, one second under it. The current
# account is barely touched (99 left): usage of C is not a condition.
ranking "$(pick account_alpha 40 $((NOW + 3600)))" "$(pick account_beta 99 $((NOW + 3600 + 86400)))"
out="$(dry)"
assert_eq "sooner-reset" "$(reason "$out")" "a weekly reset 24h sooner moves the pane, however little of C is spent"
assert_eq "account_alpha" "$(best "$out")" "…to the ranked best"
ranking "$(pick account_alpha 40 $((NOW + 3600)))" "$(pick account_beta 99 $((NOW + 3600 + 86399)))"
assert_eq "no-condition" "$(reason "$(dry)")" "…one second under 24h is not clearly better"
# The Codex shape reads just as well as the Claude one (a bare
# fromdateiso8601 reads neither, and would make every reset +infinity).
ranking "$(pick account_alpha 40 $((NOW + 3600)) x)" "$(pick account_beta 99 $((NOW + 3600 + 86400)) x)"
assert_eq "sooner-reset" "$(reason "$(dry)")" "…with a Codex-shaped resetsAt (.000Z) as with a Claude-shaped one (+00:00)"
# null is +infinity: never the sooner one.
ranking "$(pick account_alpha 99 null)" "$(pick account_beta 40 $((NOW + 3600)))"
assert_eq "no-condition" "$(reason "$(dry)")" "a best that names no reset is never sooner — +infinity, not zero"
ranking "$(pick account_alpha 40 $((NOW + 3600)))" "$(pick account_beta 99 null)"
assert_eq "sooner-reset" "$(reason "$(dry)")" "…and a current account that names none is beaten by any best that does"
ranking "$(pick account_alpha 99 null)" "$(pick account_beta 40 null)"
assert_eq "no-condition" "$(reason "$(dry)")" "…two unknown resets: neither is sooner"
# The two skips that are not guards.
ranking "$(pick account_beta 99 $((NOW + 3600)))" "$(pick account_alpha 40 $((NOW + 200000)))"
out="$(dry)"
assert_eq "already-best" "$(reason "$out")" "best == current: already on the ranked best"
assert_eq "account_beta" "$(best "$out")" "…which BEST names"
assert_eq "skipped" "$(outcome "$out")" "…and nothing is proposed"
ranking "$(pick account_alpha 99 $((NOW + 3600)))" "$(pick account_gamma 40 $((NOW + 200000)))"
out="$(dry)"
assert_eq "current-gated" "$(reason "$out")" "the current account absent from the picks is gated — a wall, rotation's job"
assert_eq "skipped" "$(outcome "$out")" "…so rebalance leaves it rather than race the rotation"
ranking
out="$(dry)"
assert_eq "current-gated" "$(reason "$out")" "…and an empty ranking gates everyone, the current account too"
assert_eq "-" "$(best "$out")" "…with no best to name"
# The BEST must be the one that beats C. gmail would (99 left against C's 10),
# but the ranked best, account_alpha, does not (20 left, 2h sooner) — so nothing moves.
ranking "$(pick account_alpha 20 $((NOW + 3600)))" "$(pick gmail 99 $((NOW + 7200)))" "$(pick account_beta 10 $((NOW + 10800)))"
out="$(dry)"
assert_eq "no-condition" "$(reason "$out")" "a pick below the best is never reached by the best simply not being better"
assert_eq "account_alpha" "$(best "$out")" "…BEST names the ranked best whatever the answer"

# --- the preflight, and the fall-through to the next ranked pick ------------
# gmail is ranked first and beats account_beta but has no token on this device (the
# suite's anu-secrets resolves only ACCOUNT_ALPHA and ACCOUNT_BETA). The next ranked pick is
# tried under the SAME rule.
ranking "$(pick gmail 99 $((NOW + 3600)))" "$(pick account_alpha 99 $((NOW + 3700)))" "$(pick account_beta 40 $((NOW + 200000)))"
out="$("$ACCOUNT" rebalance %4 --dry-run 2>&1)"
assert_eq "account_alpha" "$(best "$out")" "a best that fails preflight is passed over for the next ranked pick"
assert_eq "would-move" "$(outcome "$out")" "…and the move is still on"
assert_contains "$out" "skipping gmail — no token" "…saying which account it passed over, and why"
ranking "$(pick gmail 99 $((NOW + 3600)))" "$(pick account_beta 40 $((NOW + 200000)))" "$(pick account_alpha 99 $((NOW + 200001)))"
out="$(dry)"
assert_eq "preflight-failed" "$(reason "$out")" "the next pick must still beat C: one ranked below it that does not is never a destination"
assert_eq "gmail" "$(best "$out")" "…and the row names the best that could not be landed on"
assert_eq "skipped" "$(outcome "$out")" "…and nothing moves"

# --- every guard -----------------------------------------------------------
GOOD=("$(pick account_alpha 99 $((NOW + 3600)))" "$(pick account_beta 40 $((NOW + 200000)))")
ranking "${GOOD[@]}"
out="$(OPT_SESSION= "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "no-session" "$(reason "$out")" "no @anu_session: nothing to resume"
ranking "${GOOD[@]}"
out="$(OPT_BOX=1 "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "boxed" "$(reason "$out")" "a boxed pane is out of scope"
ranking "${GOOD[@]}"
out="$(RB_CMD=zsh "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "at-shell" "$(reason "$out")" "a pane whose human exited to a shell is never relaunched — pane state reads a bare shell as idle"
ranking "${GOOD[@]}"
out="$(OPT_REBAT=$((NOW - 21599)) "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "rebalanced-359m-ago" "$(reason "$out")" "a pane rebalanced inside the last 6h is left alone"
ranking "${GOOD[@]}"
out="$(OPT_REBAT=$((NOW - 21600)) "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "sooner-reset" "$(reason "$out")" "…and is a candidate again at 6h"
ranking "${GOOD[@]}"
out="$(OPT_ROTATING=$((NOW - 1799)) "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "relaunched-29m-ago" "$(reason "$out")" "a pane a rotation (or any switch) relaunched in the last 30m is left where it landed"
ranking "${GOOD[@]}"
out="$(OPT_ROTATING=$((NOW - 1800)) "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "sooner-reset" "$(reason "$out")" "…and is a candidate again at 30m"
ranking "${GOOD[@]}"
out="$(RB_STATE=busy "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "working" "$(reason "$out")" "a working pane is never moved"
assert_eq "0" "$(curls)" "…and costs no dashboard call"
for s in limited approval booting; do
  ranking "${GOOD[@]}"
  assert_eq "$s" "$(reason "$(RB_STATE=$s "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)")" "a pane reading $s is not idle, and is left alone"
done
ranking "${GOOD[@]}"
out="$(RB_CLIENTS="$((NOW - 10))|%4" "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "in-use" "$(reason "$out")" "the pane a human is typing in (a client's current pane, a key 10s ago) is not moved"
ranking "${GOOD[@]}"
out="$(RB_CLIENTS="$((NOW - 301))|%4" "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "sooner-reset" "$(reason "$out")" "…a client idle for 5 minutes is not"
ranking "${GOOD[@]}"
out="$(RB_CLIENTS="$((NOW - 10))|%7" "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "sooner-reset" "$(reason "$out")" "…nor is one typing in another pane"
ranking "${GOOD[@]}"
out="$(CURL_FAIL=1 "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "dashboard-unreachable" "$(reason "$out")" "with no ranking to be had, nothing moves and the row says why"
# The gate is the unattended arm's. The human's verb says it is off, and
# still answers — the way `rotate` ignores @anu_autorotate.
ranking "${GOOD[@]}"
out="$(OPT_REBGATE=0 "$ACCOUNT" rebalance %4 --dry-run 2>&1)"
assert_contains "$out" "@anu_rebalance is 0" "@anu_rebalance 0: the verb says the arm is paused"
assert_eq "sooner-reset" "$(reason "$out")" "…and still answers for the human who asked"

# --- the ranking cache: one dashboard call, reused for 15 minutes -----------
ranking "${GOOD[@]}"
dry >/dev/null
assert_eq "1" "$(curls)" "the first look asks the dashboard once"
assert_contains "$(cat "$RBCURL")" "pick?need=any&fresh=0&provider=claude" "…for its cached reading (the dashboard polls every 150s itself), on the pane's provider"
CACHED="$(cat "$RBSTORE/@anu_rebalance_rank_claude" 2>/dev/null)"
assert_match "$CACHED" "^$NOW $((NOW + 900)) account_alpha \\{\"picks\":" "…and caches it in the server option @anu_rebalance_rank_claude: fetched, expires (15 min), best, JSON"
: > "$RBCURL"
out="$(dry)"
assert_eq "0" "$(curls)" "a second look inside 15 minutes asks nothing"
assert_eq "sooner-reset" "$(reason "$out")" "…and decides identically from the cache"
: > "$RBCURL"
ANU_ACCOUNT_TEST_NOW=$((NOW + 899)) dry >/dev/null
assert_eq "0" "$(curls)" "…still nothing at 14:59"
ANU_ACCOUNT_TEST_NOW=$((NOW + 900)) dry >/dev/null
assert_eq "1" "$(curls)" "…and one call once the ranking is 15 minutes old"
# A window it describes RESETTING makes it wrong by a week, not stale by
# minutes: a reset still ahead at fetch time brings the expiry forward.
ranking "$(pick account_alpha 99 $((NOW + 100)))" "$(pick account_beta 40 $((NOW + 200000)))"
dry >/dev/null
assert_match "$(cat "$RBSTORE/@anu_rebalance_rank_claude")" "^$NOW $((NOW + 100)) " "a cached reset due before 15 minutes is the expiry"
: > "$RBCURL"
ANU_ACCOUNT_TEST_NOW=$((NOW + 99)) dry >/dev/null
assert_eq "0" "$(curls)" "…reused until that reset"
ANU_ACCOUNT_TEST_NOW=$((NOW + 100)) dry >/dev/null
assert_eq "1" "$(curls)" "…and re-fetched the moment it passes"
ranking "$(pick account_alpha 99 $((NOW - 10)))" "$(pick account_beta 40 $((NOW + 200000)))"
dry >/dev/null
assert_match "$(cat "$RBSTORE/@anu_rebalance_rank_claude")" "^$NOW $((NOW + 900)) " "…while a reset already past when it was fetched is not (that poll saw it)"
# A stale ranking and a dead dashboard: no decision on evidence that may be a
# week wrong, and the old ranking is kept, not overwritten.
ranking "${GOOD[@]}"
dry >/dev/null
: > "$RBCURL"
out="$(ANU_ACCOUNT_TEST_NOW=$((NOW + 1000)) CURL_FAIL=1 "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "dashboard-unreachable" "$(reason "$out")" "a stale ranking is never decided on when the dashboard is down"
assert_match "$(cat "$RBSTORE/@anu_rebalance_rank_claude")" "^$NOW " "…and the cached one is left as it was"
# need=fable is its own ranking, in its own option.
ranking "${GOOD[@]}"
OPT_NEED=fable dry >/dev/null
assert_contains "$(cat "$RBCURL")" "pick?need=fable&fresh=0&provider=claude" "a fable pane asks for the fable ranking"
assert_file "$RBSTORE/@anu_rebalance_rank_claude_fable" "…and caches it apart, in @anu_rebalance_rank_claude_fable"

# --- the fleet: one row per managed pane, a count of the unmanaged ----------
# %4 twice (a linked session lists it once per session), %5 a claude and %6 a
# codex nobody launched through anu, %7 a plain shell (not an agent at all),
# %8 a managed pane whose human quit to a shell.
ranking "${GOOD[@]}"
out="$(RB_PANES="$(printf '%s\n' '%4|2.1.281|account_beta|0' '%5|2.1.281||' '%6|codex||' '%7|zsh||' '%8|zsh|account_beta|0' '%4|2.1.281|account_beta|0')" \
  RB_FACTS_8="account_beta||any|dddddddd|0|||zsh|/tmp" "$ACCOUNT" rebalance --dry-run 2>/dev/null)"
assert_eq "1" "$(printf '%s\n' "$out" | grep -c '^%4 ')" "every managed pane gets ONE row, a linked session's repeat included"
assert_eq "sooner-reset" "$(reason "$out")" "…%4's decision"
assert_eq "at-shell" "$(reason "$out" %8)" "…%8 is managed, so it has a row, and says why it stays"
assert_not_contains "$out" "%5" "an unmanaged pane gets no row"
assert_eq "2 unmanaged panes skipped" "$(printf '%s\n' "$out" | tail -1)" "…just the one trailing count — two agent panes, the shell not among them"
assert_eq "1" "$(curls)" "the whole fleet costs one dashboard call"
ranking "${GOOD[@]}"
out="$(CURL_FAIL=1 RB_PANES="$(printf '%s\n' '%4|2.1.281|account_beta|0' '%9|2.1.281|account_beta|0')" "$ACCOUNT" rebalance --dry-run 2>/dev/null)"
assert_eq "1" "$(curls)" "…and a dashboard that is down costs one failed call, not one per pane"
assert_eq "dashboard-unreachable" "$(reason "$out" %9)" "…each row still saying why"
ranking "${GOOD[@]}"
out="$(OPT_ACCOUNT= "$ACCOUNT" rebalance %4 --dry-run 2>/dev/null)"
assert_eq "PANE PROVIDER ACCOUNT BEST REASON OUTCOME|1 unmanaged pane skipped" "$(printf '%s\n' "$out" | tr -s ' ' | paste -sd'|' -)" \
  "a target with no @anu_account: no row, one pane counted"

# --- the move is the real `switch` -----------------------------------------
ranking "${GOOD[@]}"
out="$("$ACCOUNT" rebalance %4 2>&1)"; rc=$?
assert_ok $rc "rebalance (no --dry-run) exits 0 when it moved"
assert_eq "moved" "$(outcome "$out")" "…and the row says moved"
log="$(cat "$LOG")"
stamp_line="$(printf '%s\n' "$log" | grep -n "set-option -p -t %4 @anu_rebalance_at $NOW" | head -1 | cut -d: -f1)"
keys_line="$(printf '%s\n' "$log" | grep -n "send-keys" | head -1 | cut -d: -f1)"
[ -n "$stamp_line" ] && [ -n "$keys_line" ] && [ "$stamp_line" -lt "$keys_line" ]
assert_ok $? "@anu_rebalance_at is stamped BEFORE the pane is touched, so a refused move still costs its 6h"
assert_contains "$log" "@ACCOUNT_LAUNCH@ launch --as account_alpha --need any -- --resume cccccccc-1111-2222-3333-444444444444 --dangerously-skip-permissions" \
  "…and the move is the switch transaction: the SAME session resumed under the best"
assert_not_contains "$log" "pane send %4" "an idle pane is reopened and left idle — no continuation"
assert_contains "$out" "%4: account_beta → account_alpha (switch, session cccccccc-1111-2222-3333-444444444444" "…and switch's own report reaches the human"
assert_eq "2" "$(cat "$RBCNT")" "the pane was read idle twice by the verb: at the decision and right before the move"
# The race guard: idle at the decision, working just before the move.
ranking "${GOOD[@]}"
out="$(RB_STATE=idle RB_STATE2=busy "$ACCOUNT" rebalance %4 2>/dev/null)"
assert_eq "raced-working" "$(reason "$out")" "a pane that went to work between the decision and the move is not moved"
assert_not_contains "$(cat "$LOG")" "send-keys" "…nothing is typed into it"
assert_not_contains "$(cat "$LOG")" "set-option -p -t %4 @anu_rebalance_at" "…and no cooldown is spent on a move that never started"
ranking "${GOOD[@]}"
dry >/dev/null
assert_eq "1" "$(cat "$RBCNT")" "--dry-run reads the pane once: there is no move to race"
# The memo: a no-move answer is remembered against the ranking and the account.
ranking "$(pick account_alpha 40 $((NOW + 3600)))" "$(pick account_beta 99 $((NOW + 3600 + 3600)))"
"$ACCOUNT" rebalance %4 >/dev/null 2>&1
assert_contains "$(cat "$LOG")" "set-option -p -t %4 @anu_rebalance_seen $NOW account_beta" "a no-move answer is stamped with the ranking it was made on (@anu_rebalance_seen)"
# A move that dies at a shell is resumed where it was.
ranking "${GOOD[@]}"
out="$(RB_CMD_AFTER=bash RB_CMD_AFTER2=claude "$ACCOUNT" rebalance %4 2>&1)"; rc=$?
assert_eq "1" "$rc" "a move whose relaunch died exits 1"
assert_eq "failed" "$(outcome "$out")" "…the row says failed"
assert_contains "$(cat "$LOG")" "@ACCOUNT_LAUNCH@ launch --as account_beta --need any -- --resume cccccccc-1111-2222-3333-444444444444" \
  "…and the session, left at a shell, is resumed on the account it was on"
assert_contains "$out" "resumed on account_beta" "…which the verb reports"
ranking "${GOOD[@]}"
out="$(RB_CMD_AFTER=bash RB_CMD_AFTER2=bash "$ACCOUNT" rebalance %4 2>&1)"
assert_contains "$out" "pane left at a shell (com8-account switch %4 --as account_beta --force)" "…and when even that fails, it says how to resume it by hand"

# --- --tick: the watchd worker ---------------------------------------------
ranking "${GOOD[@]}"
out="$("$ACCOUNT" rebalance %4 --tick 2>/dev/null)"; rc=$?
assert_eq "0" "$rc" "--tick exits 0 when it moved"
assert_eq "1" "$(printf '%s\n' "$out" | grep -c .)" "…with exactly one line"
assert_match "$out" "^\[[0-9:]+\] rebalance %4 \(sooner-reset\): %4: account_beta → account_alpha \(switch, session cccccccc-1111-2222-3333-444444444444, ready via [a-z]+\)$" \
  "…the decision and switch's own report, which is where the reap reads the destination"
ranking "$(pick account_alpha 40 $((NOW + 3600)))" "$(pick account_beta 99 $((NOW + 7200)))"
out="$("$ACCOUNT" rebalance %4 --tick 2>&1)"; rc=$?
assert_eq "2" "$rc" "--tick exits 2 when the pane stays"
assert_eq "" "$out" "…and says NOTHING when the answer is 'nothing better' (no-condition)"
ranking "$(pick account_beta 99 $((NOW + 3600)))"
assert_eq "" "$("$ACCOUNT" rebalance %4 --tick 2>&1)" "…nor when it is already on the best"
ranking "$(pick account_alpha 99 $((NOW + 3600)))"
out="$("$ACCOUNT" rebalance %4 --tick 2>&1)"
assert_match "$out" "rebalance %4: stays on account_beta — current-gated \(best account_alpha\)$" "…but a block for any other reason is one line"
ranking "${GOOD[@]}"
out="$(CURL_FAIL=1 "$ACCOUNT" rebalance %4 --tick 2>&1)"; rc=$?
assert_eq "3" "$rc" "--tick exits 3 when the dashboard did not answer (watchd backs off)"
assert_contains "$out" "stays on account_beta — dashboard-unreachable" "…in one line"
ranking "${GOOD[@]}"
out="$(OPT_ACCOUNT= "$ACCOUNT" rebalance %4 --tick 2>&1)"; rc=$?
assert_eq "2" "$rc" "an unmanaged pane: exit 2…"
assert_eq "" "$out" "…and not a word"
ranking "${GOOD[@]}"
out="$(OPT_REBGATE=0 "$ACCOUNT" rebalance %4 --tick 2>&1)"; rc=$?
assert_eq "2" "$rc" "--tick with @anu_rebalance 0 moves nothing"
assert_eq "" "$out" "…says nothing"
assert_eq "0" "$(curls)" "…and asks the dashboard nothing"
ranking "${GOOD[@]}"
out="$(OPT_REBGATE=1 "$ACCOUNT" rebalance %4 --tick 2>&1)"; rc=$?
assert_eq "0" "$rc" "@anu_rebalance 1 is on"
ranking "${GOOD[@]}"
out="$(RB_CMD_AFTER=bash RB_CMD_AFTER2=claude "$ACCOUNT" rebalance %4 --tick 2>/dev/null)"; rc=$?
assert_eq "1" "$rc" "--tick exits 1 when the move failed"
assert_match "$out" "rebalance %4 \(sooner-reset\): account_beta → account_alpha failed — switch --as account_alpha exited 1; resumed on account_beta$" "…one line, with what became of the session"

# --- usage ---------------------------------------------------------------
out="$("$ACCOUNT" rebalance %4 extra 2>&1)"; assert_fail $? "a second pane is a usage error"
out="$("$ACCOUNT" rebalance %4 --bogus 2>&1)"; assert_fail $? "an unknown flag is a usage error"
out="$("$ACCOUNT" rebalance %4 --dry-run --tick 2>&1)"; assert_fail $? "--dry-run and --tick do not mix"
out="$("$ACCOUNT" rebalance --tick 2>&1)"; assert_fail $? "--tick needs a pane"
out="$("$ACCOUNT" rebalance %99 2>&1)"; rc=$?
assert_fail $rc "an unresolvable target is refused"
assert_contains "$out" "no such pane" "…by name"
out="$("$ACCOUNT" help)"
assert_contains "$out" "com8-account rebalance [<pane>] [--dry-run]" "help documents the verb"
assert_contains "$out" "@anu_rebalance 0" "…and how to pause it"
unset ANU_ACCOUNT_TEST ANU_ACCOUNT_TEST_NOW

t_done
