#!/usr/bin/env bash
# anu-account, the CODEX branch: per-device account homes (a directory, never a
# token), the config.toml hook block and its pre-computed trust hashes, the
# launch that points the CLI at a home, the two lifecycle hooks, and the
# rotate/switch handoff. Runs the REAL bin against stubs for curl (the
# dashboard), tmux, pane, ps and codex itself.
#
# Isolation (same conventions as account_test.sh, plus the codex ones):
#   ANU_USAGE_URL         the dashboard, served by the curl stub
#   ANU_SECRETS_BIN       never reached on the codex path — a codex account has
#                         no token at all — but pointed at the stub anyway so a
#                         regression that DID reach for one is visible
#   ANU_ACCOUNT_CACHE     the token cache, so nothing touches ~/.local/state
#   ANU_ACCOUNT_LOCKDIR   the pane lock dir, same reason
#   ANU_ACCOUNT_CODEX_DIR the codex account homes
#   HOME                  a temp dir, so the ~/.codex every home is a VIEW of
#                         (and that add/launch now create and write into) is a
#                         fixture — never the runner's own
#   ANU_ACCOUNT_HOOK_CMD  pins the hook command string, so the trust-hash
#                         vectors below are stable and independent of install
#                         path
#   ANU_CODEX_BASE_CONFIG the human own ~/.codex/config.toml, which every
#                         account home is now RENDERED from. Set EMPTY here, so
#                         a suite run on a real machine never splices the
#                         runner own Codex configuration into a fixture home;
#                         section 2g sets it per-case to a fixture instead
#   # TMUX_PANE too: a suite run from inside a real pane would otherwise let the
# runner's own pane stand in for "no pane to stamp", and that case would assert
# nothing but exit 0.
unset TMUX TMUX_PANE ANU_ACCOUNT ANU_PROVIDER ANU_PANE ANU_LAUNCH_NONCE
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$here/../lib/harness.sh"
source "$here/../lib/helpers.bash"

t_suite "anu-account (codex)"
ACCOUNT="$ANU_ROOT/profiles/runtime/accounts/account"
assert_file "$ACCOUNT" "anu-account bin present"

SD="$(new_stubdir)"; export PATH="$SD:$PATH"
export COM8_ACCOUNT_PANE="$SD/pane"
LOG="$(mktmp)/calls.log"; : > "$LOG"
FIX="$(mktmp)"
export ANU_USAGE_URL="http://dash.test"
export ANU_USAGE_HOST="usage-host" ANU_USAGE_REPO="~/usage-service"
export ANU_SECRETS_BIN="$SD/anu-secrets"
unset TMUX ANU_ACCOUNT ANU_PANE ANU_LAUNCH_NONCE
export ANU_ACCOUNT_CACHE="$(mktmp)/tokens.json"
export ANU_ACCOUNT_LOCKDIR="$(mktmp)/locks"
export ANU_ACCOUNT_CODEX_DIR="$(mktmp)/codex"
export ANU_CODEX_BASE_CONFIG=""
# Before ANY anu-account runs: add and launch fold state into $HOME/.codex.
export HOME="$(mktmp)/home"; mkdir -p "$HOME"
unset CODEX_HOME XDG_STATE_HOME
# The hook command the config.toml block installs. An ABSOLUTE path to a real
# executable, because that is what `add` writes now (Codex 0.156.0 stopped
# handing its hooks the launching shell's PATH) and because the preflight
# checks an absolute program really is there. Pinned here so the installed
# hashes are a function of THIS string, not of where the repo happens to sit.
HOOKBIN="$SD/anu-account-hook"
stub "$SD" anu-account-hook 'exit 0'
export ANU_ACCOUNT_HOOK_CMD="$HOOKBIN"

cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_alpha","name":"account_alpha","email":"alpha@example.invalid","resetsAt":"2026-09-20T02:59:59Z","remaining":71,"shared":false},
 {"id":"codex-account_gamma","name":"account_gamma","email":"t@x.com","resetsAt":"2026-09-21T02:59:59Z","remaining":40,"shared":false}],
 "out":[{"id":"codex-account_beta","why":"week_all 100"}],"generatedAt":"2026-09-16T00:00:00Z"}
JSON
cat > "$FIX/pick-claude.json" <<'JSON'
{"need":"any","picks":[{"id":"claude-gmail","name":"gmail","email":"g@x.com","resetsAt":"2026-09-20T02:59:59Z","remaining":9,"shared":false}],
 "out":[],"generatedAt":"2026-09-16T00:00:00Z"}
JSON
cat > "$FIX/usage.json" <<'JSON'
{"accounts":[
 {"id":"claude-account_alpha","provider":"claude","label":"Claude — account_alpha","name":"account_alpha","shared":false,"email":"alpha@example.invalid","plan":"Max 20×","error":null,
  "windows":[{"key":"session-0","label":"Session · 5h","usedPercent":3,"resetsAt":null},{"key":"weekly_all-1","label":"Week · all models","usedPercent":21,"resetsAt":"2026-09-20T02:59:59Z"},{"key":"weekly_scoped-2","label":"Week · Fable","usedPercent":41,"resetsAt":"2026-09-20T02:59:59Z"}]},
 {"id":"codex-account_alpha","provider":"openai","label":"ChatGPT — account_alpha","name":"account_alpha","shared":false,"email":"alpha@example.invalid","plan":"Pro","error":null,
  "windows":[{"key":"session-0","label":"Session · 5h","usedPercent":11,"resetsAt":null},{"key":"weekly_all-1","label":"Week · all models","usedPercent":29,"resetsAt":"2026-09-20T02:59:59Z"}]},
 {"id":"codex-account_beta","provider":"openai","label":"ChatGPT — account_beta","name":"account_beta","shared":true,"email":"d@x.com","plan":"Pro","error":"no poll grant","windows":[]}
],"generatedAt":"2026-09-16T00:00:00Z"}
JSON

stub "$SD" curl '
url="${@: -1}"; printf "curl %s\n" "$url" >> "'"$LOG"'"
printf "%s\n" "$*" >> "'"$LOG"'.args"
[ -n "${CURL_FAIL:-}" ] && exit 28
case "$url" in
  *"/api/usage/pick?"*"provider=openai"*) cat "'"$FIX"'/pick-codex.json" ;;
  *"/api/usage/pick?"*)                   cat "'"$FIX"'/pick-claude.json" ;;
  *"/api/usage")                          cat "'"$FIX"'/usage.json" ;;
  *) exit 22 ;;
esac'
stub "$SD" anu-secrets '
printf "anu-secrets %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  get) case "$3" in GMAIL) echo "sk-ant-oat01-gmail"; exit 0 ;; *) exit 1 ;; esac ;;
  *) exit 1 ;;
esac'

# ---------------------------------------------------------------------------
t_section "1. the chooser — pick/ls carry a provider"

: > "$LOG"
out="$("$ACCOUNT" pick --provider codex)"; rc=$?
assert_ok $rc "pick --provider codex exits 0"
assert_eq "account_alpha
account_gamma" "$out" "…and prints the openai picks in rank order"
assert_contains "$(cat "$LOG")" "provider=openai" "…having asked the dashboard for the openai provider"

: > "$LOG"
out="$("$ACCOUNT" pick)"; assert_ok $? "pick with no --provider still exits 0"
assert_eq "gmail" "$out" "…and defaults to claude"
assert_contains "$(cat "$LOG")" "provider=claude" "…naming claude explicitly in the query"

out="$("$ACCOUNT" pick --provider codex --need fable 2>&1)"; rc=$?
assert_fail $rc "--need fable with --provider codex is a usage error"
assert_contains "$out" "fable" "…and says which flag is wrong"
out="$("$ACCOUNT" pick --provider gemini 2>&1)"; assert_fail $? "an unknown provider is refused"

: > "$LOG"
out="$("$ACCOUNT" pick --provider codex --exclude account_beta --stale)" || true
assert_contains "$(cat "$LOG")" "fresh=0&exclude=account_beta" "--exclude/--stale still reach the query on the codex path"

: > "$LOG"
out="$("$ACCOUNT" ls --provider codex)"; assert_ok $? "ls --provider codex exits 0"
assert_contains "$out" "NAME" "…prints a header"
assert_not_contains "$out" "FABLE" "…without a FABLE column (ChatGPT has no Fable window)"
assert_contains "$out" "account_alpha" "…lists an openai account"
assert_contains "$out" "no poll grant" "…and carries each row's error state"
assert_not_contains "$out" "Max 20×" "…and never a claude row"

# ---------------------------------------------------------------------------
t_section "2. credentials are a DIRECTORY, per device"

# `codex login` is interactive (the human finishes in a browser); the stub
# stands in for it and writes the auth.json a real login would leave behind.
stub "$SD" codex '
printf "codex %s\n" "$*" >> "'"$LOG"'"
printf "CODEX_HOME=%s\n" "${CODEX_HOME:-}" >> "'"$LOG"'"
case "$1 $2" in
  "login --device-auth") [ -n "${CODEX_LOGIN_FAIL:-}" ] && exit 1
                         printf "{\"tokens\":\"x\"}\n" > "$CODEX_HOME/auth.json" ;;
esac
exit 0'
stub "$SD" ssh 'printf "ssh %s\n" "$*" >> "'"$LOG"'"; cat >/dev/null; exit 0'
stub "$SD" tmux 'printf "tmux %s\n" "$*" >> "'"$LOG"'"'

: > "$LOG"
out="$("$ACCOUNT" add --provider codex account_alpha 2>&1)"; rc=$?
assert_ok $rc "add --provider codex exits 0"
HOME_K="$ANU_ACCOUNT_CODEX_DIR/account_alpha"
assert_eq "1" "$([ -d "$HOME_K" ] && echo 1 || echo 0)" "the account home is created"
assert_eq "700" "$(stat -c %a "$HOME_K" 2>/dev/null || stat -f %Lp "$HOME_K")" "…0700, like the credential it holds"
assert_contains "$(cat "$LOG")" "codex login --device-auth" "…and codex login --device-auth ran"
assert_contains "$(cat "$LOG")" "CODEX_HOME=$HOME_K" "…pointed at that home, never the human own ~/.codex"
assert_contains "$out" "PER DEVICE" "the note says codex logins do not travel"
# Nothing is synced anywhere: a codex credential is a directory on THIS box.
assert_not_contains "$(cat "$LOG")" "ssh " "add --provider codex never touches the serving host"
assert_not_contains "$(cat "$LOG")" "anu-secrets" "…and never reaches for Infisical"

t_section "2b. the home is a view of the human's own ~/.codex"
assert_eq "1" "$([ -L "$HOME_K/sessions" ] && echo 1 || echo 0)" "the home sessions is a symlink"
assert_eq "$HOME/.codex/sessions" "$(readlink "$HOME_K/sessions")" \
  "…at ~/.codex/sessions itself, so every account AND plain codex resume the same rollouts"
assert_eq "$HOME/.codex/sessions" "$(readlink "$ANU_ACCOUNT_CODEX_DIR/sessions")" \
  "the old shared store path is a link to it too, for homes that still point there"
assert_eq "700" "$(stat -c %a "$HOME/.codex" 2>/dev/null || stat -f %Lp "$HOME/.codex")" \
  "a ~/.codex anu had to create is 0700"
assert_eq "0" "$([ -L "$HOME_K/auth.json" ] && echo 1 || echo 0)" "auth.json is the home's own"
assert_eq "0" "$([ -L "$HOME_K/config.toml" ] && echo 1 || echo 0)" "…and so is config.toml"

t_section "2c. the hook block and its pre-computed trust"
CFG="$HOME_K/config.toml"
assert_file "$CFG" "config.toml written"
cfg="$(cat "$CFG")"
assert_contains "$cfg" "[[hooks.SessionStart]]" "SessionStart table"
assert_contains "$cfg" "[[hooks.UserPromptSubmit]]" "UserPromptSubmit table"
assert_contains "$cfg" "command = \"$HOOKBIN hook codex session-start\"" "…running anu own hook verb"
assert_contains "$cfg" "command = \"$HOOKBIN hook codex user-prompt\"" "…for both events"
# Only what anu READS is registered: a usage-limit turn fires no Stop at all
# (the record lands in the rollout instead), so a Stop/SessionEnd table would
# be a hook that never says anything.
assert_not_contains "$cfg" "[[hooks.Stop]]" "no Stop table — anu reads the rollout, not a Stop payload"
assert_not_contains "$cfg" "[[hooks.SessionEnd]]" "no SessionEnd table either"

# The trust hash, two independent ways. (1) recomputed from the recipe here,
# in the test, out of the exact canonical JSON the spike record verified
# against four hashes Codex itself wrote.
CANON='{"event_name":"%s","hooks":[{"async":false,"command":"%s","timeout":%s,"type":"command"}]}'
recipe() {  # $1 = event_snake, $2 = command, $3 = timeout
  printf "$CANON" "$1" "$2" "$3" | shasum -a 256 | cut -d" " -f1
}
h_ss="$(recipe session_start "$HOOKBIN hook codex session-start" 600)"
assert_contains "$cfg" "trusted_hash = \"sha256:$h_ss\"" "the SessionStart trust hash IS the recipe, recomputed"
# (2) a FIXED vector, pinned as a literal and computed over a literal absolute
# command — so a change to the recipe itself (a key order, a default timeout,
# the `sha256:` prefix) fails here even if both the bin and this test moved
# together. It needs no installed file: it pins the recipe, not the install.
assert_eq "b8c6347ed0decc2d818a80ff34907f57bdf16583bd8cb983085885209d781551" \
  "$(recipe session_start "/opt/anu/config/bash/bin/anu-account hook codex session-start" 600)" \
  "…and the recipe still matches the hand-computed literal vector"
h_up="$(recipe user_prompt_submit "$HOOKBIN hook codex user-prompt" 600)"
assert_contains "$cfg" "trusted_hash = \"sha256:$h_up\"" "the UserPromptSubmit trust hash likewise"
assert_contains "$cfg" "[hooks.state.\"$CFG:session_start:0:0\"]" \
  "the trust KEY is <config path>:<event>:<matcher idx>:<hook idx>"

t_section "2d. add is idempotent, and honest about a failed login"
before="$(cat "$CFG")"
: > "$LOG"
out="$("$ACCOUNT" add --provider codex account_alpha 2>&1)"; assert_ok $? "a re-add exits 0"
assert_eq "$before" "$(cat "$CFG")" "…and writes the same bytes (no drift, no second block)"
assert_eq "1" "$(grep -c "anu-codex-hooks-begin" "$CFG")" "…exactly one owned block"

: > "$LOG"
out="$(CODEX_LOGIN_FAIL=1 "$ACCOUNT" add --provider codex nobody 2>&1)"; rc=$?
assert_fail $rc "a login that failed fails the add"
assert_eq "0" "$([ -f "$ANU_ACCOUNT_CODEX_DIR/nobody/config.toml" ] && echo 1 || echo 0)" \
  "…and no hook block is written for a home that cannot authenticate"

t_section "2f. the installed hook command is an ABSOLUTE path, never a PATH lookup"
# Codex 0.156.0 stopped handing the hooks it spawns the launching shell's
# PATH: an installed `com8-account hook codex session-start` resolved to the
# wrong `anu` and did nothing at all — silently, because a hook that fails is
# a hook that writes no stamp. So `add` writes the absolute path of the
# anu-account that ran it, and nothing about the hook depends on anyone's PATH.
stub "$SD" anu 'printf "anu %s\n" "$*"'      # on PATH, and deliberately ignored
out="$(ANU_ACCOUNT_HOOK_CMD= "$ACCOUNT" add --provider codex abspath 2>&1)"; rc=$?
assert_ok $rc "add works with no ANU_ACCOUNT_HOOK_CMD override"
cfg_abs="$(cat "$ANU_ACCOUNT_CODEX_DIR/abspath/config.toml")"
assert_not_contains "$cfg_abs" 'command = "com8-account hook' \
  "the dispatcher on PATH is NOT what gets written"
assert_contains "$cfg_abs" "command = \"$ACCOUNT hook codex session-start\"" \
  "…the absolute path of the anu-account that ran the add is"
assert_contains "$cfg_abs" "command = \"$ACCOUNT hook codex user-prompt\"" "…for both events"
assert_match "$cfg_abs" 'command = "/' "…and it really is absolute"
# …and the trust hash is the hash of THAT string, so Codex will run it.
h_abs="$(recipe session_start "$ACCOUNT hook codex session-start" 600)"
assert_contains "$cfg_abs" "trusted_hash = \"sha256:$h_abs\"" \
  "the trust hash is the hash of the absolute command, so the hook is trusted as written"

# A symlinked invocation resolves to the same real file, so a home installed
# through a link and one installed directly agree — and the hook still points
# at something that exists after the link is gone.
LINKDIR="$(mktmp)/link"; mkdir -p "$LINKDIR"; ln -s "$ACCOUNT" "$LINKDIR/anu-account"
out="$(ANU_ACCOUNT_HOOK_CMD= "$LINKDIR/anu-account" add --provider codex vialink 2>&1)"; rc=$?
assert_ok $rc "add through a symlink works"
assert_contains "$(cat "$ANU_ACCOUNT_CODEX_DIR/vialink/config.toml")" \
  "command = \"$ACCOUNT hook codex session-start\"" \
  "…and records the resolved real path, not the link it was reached through"

t_section "2e. the human config.toml is not ours to rewrite"
mk_home() {  # $1 = account name, $2 = the config.toml to plant first
  rm -rf "$ANU_ACCOUNT_CODEX_DIR/$1"
  mkdir -p "$ANU_ACCOUNT_CODEX_DIR/$1"
  printf '%s' "$2" > "$ANU_ACCOUNT_CODEX_DIR/$1/config.toml"
}
mk_home mine 'model = "gpt-5"

[projects."/Users/x/repo"]
trust_level = "trusted"

[[hooks.SessionStart]]
hooks = [{ type = "command", command = "echo mine" }]
'
out="$("$ACCOUNT" add --provider codex mine 2>&1)"; assert_ok $? "a home with the human own tables still installs"
cfg="$(cat "$ANU_ACCOUNT_CODEX_DIR/mine/config.toml")"
assert_contains "$cfg" 'model = "gpt-5"' "…their model survives"
assert_contains "$cfg" '[projects."/Users/x/repo"]' "…their project trust survives"
assert_contains "$cfg" 'command = "echo mine"' "…and their own hook survives"
assert_contains "$cfg" ":session_start:1:0" \
  "the matcher index counts THEIR SessionStart table ahead of ours"
assert_contains "$cfg" ":user_prompt_submit:0:0" "…and is 0 for an event they do not subscribe to"

mk_home halfway 'model = "x"
# anu-codex-hooks-begin (anu account — do not edit between the markers)
[[hooks.SessionStart]]
'
out="$("$ACCOUNT" add --provider codex halfway 2>&1)"; rc=$?
assert_fail $rc "a begin marker with no end marker refuses"
assert_contains "$out" "refusing to overwrite everything below it" "…saying why"
assert_contains "$(cat "$ANU_ACCOUNT_CODEX_DIR/halfway/config.toml")" "model = \"x\"" "…and writes nothing"

mk_home dup "[[hooks.SessionStart]]
hooks = [{ type = \"command\", command = \"$HOOKBIN hook codex session-start\" }]
"
out="$("$ACCOUNT" add --provider codex dup 2>&1)"; rc=$?
assert_fail $rc "a hand-installed copy of our own hook refuses rather than running it twice"
assert_contains "$out" "refusing to install a duplicate" "…saying why"

# A human who trusted their OWN SessionStart hook through /settings owns
# `…:session_start:0:0`; ours is `…:session_start:1:0`, precisely because
# their table pushed the matcher index along. Only the EXACT key collides.
CFG_OWN="$ANU_ACCOUNT_CODEX_DIR/theirs/config.toml"
mk_home theirs "[[hooks.SessionStart]]
hooks = [{ type = \"command\", command = \"echo theirs\" }]

[hooks.state.\"$CFG_OWN:session_start:0:0\"]
trusted_hash = \"sha256:whatever-the-tui-computed\"
"
out="$("$ACCOUNT" add --provider codex theirs 2>&1)"; rc=$?
assert_ok $rc "a home where the human trusted their own hook still installs"
cfg="$(cat "$CFG_OWN")"
assert_contains "$cfg" "sha256:whatever-the-tui-computed" "…their trust entry survives"
assert_contains "$cfg" ":session_start:1:0" "…and ours takes the next matcher index"

# …but the exact key really does collide when it is ours.
CFG_CL="$ANU_ACCOUNT_CODEX_DIR/collide/config.toml"
mk_home collide "[hooks.state.\"$CFG_CL:session_start:0:0\"]
trusted_hash = \"sha256:planted\"
"
out="$("$ACCOUNT" add --provider codex collide 2>&1)"; rc=$?
assert_fail $rc "a trust entry at the exact key anu would write still refuses"
assert_contains "$out" "refusing to write a duplicate key" "…saying why (Codex drops the WHOLE file on invalid TOML)"

mk_home opaque '[hooks]
SessionStart = []
'
out="$("$ACCOUNT" add --provider codex opaque 2>&1)"; rc=$?
assert_fail $rc "a hooks table anu cannot attribute refuses rather than guessing a matcher index"
assert_contains "$out" "refusing to guess" "…saying why"

t_section "2g. the home config.toml is RENDERED from the human own ~/.codex"

# A CODEX_HOME reads exactly ONE configuration file, and it is the one in that
# home: verified on 0.156.1 — `codex doctor` names that single path, there is
# no include directive, no second config env var, no config.d, and the only
# other layer at all is the system-wide /etc/codex/managed_config.toml
# enterprise policy. So a home holding only anu's hook block is a Codex with
# NONE of the human's configuration — no model, no reasoning effort, no MCP
# servers — which is exactly the regression `cdx` shipped. The home is
# therefore RENDERED: base, then anu's hooks, then whatever Codex wrote here.
BASE="$(mktmp)/base.toml"
cat > "$BASE" <<'TOML'
notify = ["x", "turn-ended"]
model = "gpt-6-astra"
model_reasoning_effort = "max"

[features]
js_repl = false

[mcp_servers.anu]
command = "python3"
args = ["/x/server.py"]

[[hooks.SessionStart]]
hooks = [{ type = "command", command = "echo a-foreign-hook" }]

[hooks.state."somewhere-else:session_start:0:0"]
trusted_hash = "sha256:not-ours"

[tui]
status_line = ["model-with-reasoning"]
TOML

mk_home rendered 'service_tier = "flex"
model = "gpt-5-stale"

[projects."/Users/x/only-here"]
trust_level = "trusted"

[tui]
screen_reader_detection_done = true

[tui.model_availability_nux]
gpt-6-astra = 4
'
out="$(ANU_CODEX_BASE_CONFIG="$BASE" "$ACCOUNT" add --provider codex rendered 2>&1)"; rc=$?
assert_ok $rc "add renders a home from the human own config.toml"
R="$ANU_ACCOUNT_CODEX_DIR/rendered/config.toml"
cfg="$(cat "$R")"

# (a) the base, whole — this is the regression the render exists to fix.
assert_contains "$cfg" 'model = "gpt-6-astra"' "their model follows the account"
assert_contains "$cfg" 'model_reasoning_effort = "max"' "…and their reasoning effort ('default' in the status bar was the visible half of the bug)"
assert_contains "$cfg" '[mcp_servers.anu]' "…and their MCP servers, which were silently absent"
assert_contains "$cfg" '[features]' "…and every other table they set"
assert_contains "$cfg" 'notify = ["x", "turn-ended"]' "…preamble keys included"

# …but never their hooks. A hook in the base is either a duplicate of ours —
# which would run anu's hook twice an event — or a foreign command carrying no
# launch nonce; and either one moves the matcher index every trust hash is
# keyed on.
assert_not_contains "$cfg" "echo a-foreign-hook" "a hooks table in the base is stripped, never copied"
assert_not_contains "$cfg" "somewhere-else:session_start" "…and so is its trust entry"

# (b) anu's block, with the hashes the launch depends on.
assert_contains "$cfg" "[[hooks.SessionStart]]" "anu's own hook tables are there"
assert_contains "$cfg" "trusted_hash = \"sha256:$h_ss\"" "…pre-trusted with the same recipe as ever"
assert_contains "$cfg" "[hooks.state.\"$R:session_start:0:0\"]" \
  "…at matcher index 0, because the base contributed no hooks table to push it along"

# (c) what Codex itself wrote into THIS home, minus anything the base defines.
assert_contains "$cfg" '[projects."/Users/x/only-here"]' "a trust row only this home has survives"
assert_contains "$cfg" 'service_tier = "flex"' "…and a preamble key only this home has"
assert_contains "$cfg" '[tui.model_availability_nux]' "…and a table the base leaves alone"

# Base wins a collision, both for a table and for a bare key. The home's copy
# is Codex's own state, and Codex rewrites it the moment it needs it again;
# the human's file is the one that has to be obeyed.
assert_not_contains "$cfg" 'gpt-5-stale' "a preamble key the base also sets loses"
assert_contains "$cfg" 'status_line = ["model-with-reasoning"]' "a table the base also defines is the BASE's"
assert_not_contains "$cfg" "screen_reader_detection_done" "…and the home's colliding copy of it is gone"
assert_eq "1" "$(grep -c '^\[tui\]$' "$R")" "…exactly once, because two [tui] tables is invalid TOML"

# Order is the contract: base first (so bare keys precede every header),
# then anu's block, then the home's own tables.
first_hdr="$(awk '/^\[/ { print NR; exit }' "$R")"
first_key="$(awk '/^[A-Za-z_]+[ \t]*=/ { print NR; exit }' "$R")"
assert_eq "1" "$([ "$first_key" -lt "$first_hdr" ] && echo 1 || echo 0)" \
  "every preamble key comes before the first table header"
assert_eq "1" "$(awk -v b="$(awk '/anu-codex-hooks-begin/ { print NR }' "$R")" \
  '/^\[mcp_servers\.anu\]/ { print (NR < b) ? 1 : 0 }' "$R")" "the base sits above anu's block"
assert_eq "1" "$(awk -v e="$(awk '/anu-codex-hooks-end/ { print NR }' "$R")" \
  '/^\[projects\."\/Users\/x\/only-here"\]/ { print (NR > e) ? 1 : 0 }' "$R")" \
  "…and the home's own tables below it"

# Rendering twice is a no-op, byte for byte. A render that drifted would
# rewrite every home on every launch and make "unchanged" a lie.
before="$(cat "$R")"
out="$(ANU_CODEX_BASE_CONFIG="$BASE" "$ACCOUNT" add --provider codex rendered 2>&1)"
assert_ok $? "a second add exits 0"
assert_eq "$before" "$(cat "$R")" "…and the render is byte-identical the second time"
assert_eq "1" "$(grep -c 'anu-codex-hooks-begin' "$R")" "…still exactly one owned block"
assert_eq "600" "$(stat -c %a "$R" 2>/dev/null || stat -f %Lp "$R")" "the rendered file is 0600"

# The base is READ, never written. It is the human's file.
assert_not_contains "$(cat "$BASE")" "anu-codex-hooks-begin" "~/.codex/config.toml is never touched"
assert_not_contains "$(cat "$BASE")" "hook codex session-start" "…not one byte of anu goes into it"

# No base at all is the old behaviour, exactly: hooks plus home state.
mk_home nobase '[projects."/Users/x/kept"]
trust_level = "trusted"
'
out="$(ANU_CODEX_BASE_CONFIG= "$ACCOUNT" add --provider codex nobase 2>&1)"; rc=$?
assert_ok $rc "a human with no ~/.codex/config.toml still gets a working home"
NB="$ANU_ACCOUNT_CODEX_DIR/nobase/config.toml"
assert_contains "$(cat "$NB")" "[[hooks.SessionStart]]" "…with the hook block"
assert_contains "$(cat "$NB")" '[projects."/Users/x/kept"]' "…and its own state"
assert_not_contains "$(cat "$NB")" "gpt-6-astra" "…and nothing invented from a base that is not there"

# A base pointing at the home itself is not a base: rendering a file from
# itself would double every table it holds.
before="$(cat "$NB")"
out="$(ANU_CODEX_BASE_CONFIG="$NB" "$ACCOUNT" add --provider codex nobase 2>&1)"
assert_ok $? "a base that IS the home renders as no base at all"
assert_eq "$before" "$(cat "$NB")" "…leaving the home exactly as it was"

# The hook hash is a function of the COMMAND, never of the base: a human who
# edits their config.toml must not silently untrust every hook anu installed.
h_before="$(sed -n 's/^trusted_hash = "\(.*\)"$/\1/p' "$R" | head -1)"
printf '\n[mcp_servers.another]\ncommand = "true"\n' >> "$BASE"
out="$(ANU_CODEX_BASE_CONFIG="$BASE" "$ACCOUNT" add --provider codex rendered 2>&1)"
assert_ok $? "a base that grew a table re-renders"
assert_contains "$(cat "$R")" "[mcp_servers.another]" "…picking the new table up"
assert_eq "$h_before" "$(sed -n 's/^trusted_hash = "\(.*\)"$/\1/p' "$R" | head -1)" \
  "…and the trust hash is unchanged, so Codex still runs the hook"

# Every LAUNCH re-renders, which is what makes an edit to ~/.codex live in the
# next pane rather than at the next `add`.
printf '\n[mcp_servers.after_launch]\ncommand = "true"\n' >> "$BASE"
out="$(TMUX_PANE=%9 ANU_CODEX_BASE_CONFIG="$BASE" "$ACCOUNT" launch --provider codex --as rendered --need any -- --full-auto)"; rc=$?
assert_ok $rc "launch --provider codex exits 0"
assert_contains "$(cat "$R")" "[mcp_servers.after_launch]" \
  "…and the home was re-rendered on the way, so the human's edit is live in this pane"

# ---------------------------------------------------------------------------
t_section "2h. one ~/.codex, every account: a home is a VIEW of it"
# A home IS the human's own ~/.codex with a different auth.json and a rendered
# config.toml; everything else is a link into ~/.codex, so a managed pane and
# plain `codex` resume the same conversations. Each case below runs against a
# HOME and an account dir of its own, so the suite's homes never see them.
va() { HOME="$VH" ANU_ACCOUNT_CODEX_DIR="$VA" "$ACCOUNT" "$@"; }
vnew() {  # a fresh HOME whose ~/.codex holds one of every kind of entry
  local d; d="$(mktmp)"; VH="$d/home"; VA="$d/acct"; VC="$VH/.codex"
  mkdir -p "$VC/sessions/2026/09/01" "$VC/archived_sessions" "$VC/dictation-history" "$VA"
  printf '{"r":"canon"}\n' > "$VC/sessions/2026/09/01/rollout-canon.jsonl"
  printf '{"r":"archived"}\n' > "$VC/archived_sessions/rollout-archived.jsonl"
  printf '{"session_id":"a","ts":1,"text":"one"}\n{"session_id":"a","ts":2,"text":"two"}\n' > "$VC/history.jsonl"
  printf 'canon-db' > "$VC/memories_1.sqlite"; printf 'canon-wal' > "$VC/memories_1.sqlite-wal"
  printf 'canon-shm' > "$VC/memories_1.sqlite-shm"
  printf 'agents\n' > "$d/AGENTS.md"; ln -s "$d/AGENTS.md" "$VC/AGENTS.md"
  printf 'v1\n' > "$VC/.sandbox_migration"; printf 'odd\n' > "$VC/..odd-name"
  printf '{"human":"own login"}\n' > "$VC/auth.json"; printf 'model = "mine"\n' > "$VC/config.toml"
}
vhome() {  # $1 = an account with a login and nothing else yet
  mkdir -p "$VA/$1"; printf '{"tokens":"x"}\n' > "$VA/$1/auth.json"
}
vlaunch() { va launch --provider codex --as "$1" -- --full-auto; }   # stdout = the stub's exec line
old_mtime() { touch -t 202001010000 "$@"; }                          # "sat unchanged for a minute"
lsr() { (cd "$1" && ls -laR . 2>/dev/null); }

# --- a fresh add links every entry of ~/.codex but auth.json and config.toml --
vnew
out="$(va add --provider codex fresh 2>&1)"; rc=$?
assert_ok $rc "add into a populated ~/.codex exits 0"
H="$VA/fresh"
for n in sessions archived_sessions dictation-history history.jsonl memories_1.sqlite AGENTS.md .sandbox_migration ..odd-name; do
  assert_eq "$VC/$n" "$(readlink "$H/$n" 2>/dev/null)" "fresh add: $n is a link to ~/.codex/$n"
done
assert_eq "0" "$(ls -a "$H" | grep -c -e '-wal$' -e '-shm$')" \
  "a database's -wal/-shm are never linked (SQLite keeps them beside the REAL file)"
assert_eq "0" "$([ -L "$H/auth.json" ] && echo 1 || echo 0)" "the human's own auth.json is never linked in"
assert_eq '{"tokens":"x"}' "$(cat "$H/auth.json")" "…the home keeps the login it was given"
assert_eq '{"human":"own login"}' "$(cat "$VC/auth.json")" "…and ~/.codex/auth.json is untouched"
assert_eq "0" "$([ -L "$H/config.toml" ] && echo 1 || echo 0)" "config.toml is never linked — it is rendered"
assert_contains "$(cat "$H/config.toml")" "anu-codex-hooks-begin" "…and carries anu's hook block"
assert_eq 'model = "mine"' "$(cat "$VC/config.toml")" "…while ~/.codex/config.toml is untouched"
assert_contains "$out" "is a view of ~/.codex" "add says what the home is"
assert_not_contains "$out" "merged" "a fresh home is linked, not repaired: no fold-back lines"

out="$(va add --provider codex sessions 2>&1)"; rc=$?
assert_fail $rc "an account named 'sessions' is refused — it would BE the old store"
assert_eq "0" "$([ -e "$VC/sessions/auth.json" ] && echo 1 || echo 0)" "…and nothing is written inside ~/.codex/sessions"

# --- no ~/.codex at all: it is created 0700, with an empty sessions ----------
d="$(mktmp)"; VH="$d/home"; VA="$d/acct"; VC="$VH/.codex"; mkdir -p "$VH"
out="$(va add --provider codex first 2>&1)"; rc=$?
assert_ok $rc "add with no ~/.codex at all exits 0"
assert_eq "1" "$([ -d "$VC/sessions" ] && echo 1 || echo 0)" "~/.codex is created, with a sessions/ — it is still the canonical store"
assert_eq "700" "$(stat -c %a "$VC" 2>/dev/null || stat -f %Lp "$VC")" "…0700"
assert_eq "700" "$(stat -c %a "$VC/sessions" 2>/dev/null || stat -f %Lp "$VC/sessions")" "…and so is its sessions/"
assert_eq "$VC/sessions" "$(readlink "$VA/first/sessions")" "…and the home links it"
assert_eq "0" "$([ -e "$VC/auth.json" ] && echo 1 || echo 0)" "…and the account's login stays in its home"

# --- a home from before the rule: real sessions/, history, a database --------
vnew; vhome old
mkdir -p "$VA/old/sessions/2026/09/02"
printf '{"r":"old"}\n' > "$VA/old/sessions/2026/09/02/rollout-old.jsonl"
mkdir -p "$VA/old/sessions/2026/09/01"
printf '{"r":"a different canon"}\n' > "$VA/old/sessions/2026/09/01/rollout-canon.jsonl"
printf '{"session_id":"a","ts":2,"text":"two"}\n{"session_id":"b","ts":3,"text":"three"}' > "$VA/old/history.jsonl"
printf 'home-db' > "$VA/old/memories_1.sqlite"; printf 'home-wal' > "$VA/old/memories_1.sqlite-wal"
printf 'home-shm' > "$VA/old/memories_1.sqlite-shm"
ln -s /nowhere/else "$VA/old/archived_sessions"
: > "$LOG"
out="$(vlaunch old 2>&1)"; rc=$?
assert_ok $rc "a launch of a home from before the rule exits 0"
assert_contains "$(cat "$LOG")" "CODEX_HOME=$VA/old" "…and still execs codex in that home"
# sessions: merged, copy-if-absent, the original kept, the link in place
assert_eq '{"r":"old"}' "$(cat "$VC/sessions/2026/09/02/rollout-old.jsonl" 2>/dev/null)" \
  "the home's rollout is merged into ~/.codex/sessions"
assert_eq '{"r":"canon"}' "$(cat "$VC/sessions/2026/09/01/rollout-canon.jsonl")" \
  "…copy-if-absent: a rollout ~/.codex already has is never overwritten"
assert_eq "$VC/sessions" "$(readlink "$VA/old/sessions")" "…and the home's sessions is now the link"
BK="$(ls -d "$VA/old/sessions.pre-link."* 2>/dev/null | head -1)"
assert_ne "" "$BK" "…with the original kept beside it as sessions.pre-link.<stamp>"
assert_eq '{"r":"a different canon"}' "$(cat "$BK/2026/09/01/rollout-canon.jsonl" 2>/dev/null)" \
  "…holding every byte, the conflicting rollout included"
assert_eq "1" "$([ "$BK/2026/09/02/rollout-old.jsonl" -ef "$VC/sessions/2026/09/02/rollout-old.jsonl" ] && echo 1 || echo 0)" \
  "the merged rollout is a HARD LINK, so a live pane's appends keep landing where the view sees them"
assert_contains "$out" "merged" "…and the launch says what it folded back"
# history.jsonl: the missing line appended once, the duplicate not doubled
assert_eq '{"session_id":"a","ts":1,"text":"one"}
{"session_id":"a","ts":2,"text":"two"}
{"session_id":"b","ts":3,"text":"three"}' "$(cat "$VC/history.jsonl")" \
  "history.jsonl: the line ~/.codex lacked is appended, the one it had is not doubled"
assert_eq "$VC/history.jsonl" "$(readlink "$VA/old/history.jsonl")" "…and the home links it"
assert_contains "$(cat "$VA/old/history.jsonl.pre-link."*)" '"text":"three"' "…with its original kept"
# the database: never merged, kept whole with its sidecars, the link in place
assert_eq "canon-db" "$(cat "$VC/memories_1.sqlite")" "a database is never merged: ~/.codex's is untouched"
assert_eq "canon-wal" "$(cat "$VC/memories_1.sqlite-wal")" "…its WAL too"
assert_eq "$VC/memories_1.sqlite" "$(readlink "$VA/old/memories_1.sqlite")" "…and the home links it"
DBK="$(ls "$VA/old" | sed -n 's/^\(memories_1\.sqlite\.pre-link\.[0-9-]*\)$/\1/p' | head -1)"
assert_ne "" "$DBK" "the home's database is kept as memories_1.sqlite.pre-link.<stamp>"
assert_eq "home-db"  "$(cat "$VA/old/$DBK" 2>/dev/null)" "…whole"
assert_eq "home-wal" "$(cat "$VA/old/$DBK-wal" 2>/dev/null)" "…with its -wal under the backup's own name, where SQLite looks for it"
assert_eq "home-shm" "$(cat "$VA/old/$DBK-shm" 2>/dev/null)" "…and its -shm"
assert_eq "0" "$(_e() { [ -e "$1" ] || [ -L "$1" ]; }; _e "$VA/old/memories_1.sqlite-wal" && echo 1 || echo 0)" \
  "…and no stray -wal is left beside the link"
# a link to somewhere else: replaced, and kept
assert_eq "$VC/archived_sessions" "$(readlink "$VA/old/archived_sessions")" "a link pointing elsewhere is replaced by the view's"
assert_eq "/nowhere/else" "$(readlink "$VA/old/archived_sessions.pre-link."* 2>/dev/null)" "…and the old link is kept"
# the rest of ~/.codex is linked in on the way
assert_eq "$VC/AGENTS.md" "$(readlink "$VA/old/AGENTS.md")" "an entry the old home never had is linked in"

# --- idempotent: a second launch changes nothing and prints nothing ----------
before_c="$(lsr "$VC")"; before_a="$(lsr "$VA")"
err="$(vlaunch old 2>&1 >/dev/null)"; rc=$?
assert_ok $rc "a second launch exits 0"
assert_eq "" "$err" "…and prints nothing on stderr"
assert_eq "$before_c" "$(lsr "$VC")" "…and ~/.codex is byte-for-byte the same listing (ls -laR)"
assert_eq "$before_a" "$(lsr "$VA")" "…and so are the homes and the store"
out="$(va doctor --provider codex)"; rc=$?
assert_ok $rc "doctor: every home here is a view — exit 0"
assert_contains "$out" "old          ok" "…and says so for the home"

# --- the old store, a real directory, is merged and becomes a link -----------
vnew; vhome k
mkdir -p "$VA/sessions/2026/09/03"; printf '{"r":"store"}\n' > "$VA/sessions/2026/09/03/rollout-store.jsonl"
ln -s "$VA/sessions" "$VA/k/sessions"                    # how every home used to be made
out="$(va doctor --provider codex)"; rc=$?
assert_eq "1" "$rc" "doctor: a home whose state is outside ~/.codex exits 1"
assert_contains "$out" "is not a link to ~/.codex/sessions" "…naming the old store"
assert_contains "$out" "linked elsewhere: sessions" "…and the home, whose sessions link reaches the store rather than ~/.codex"
out="$(vlaunch k 2>&1)"
assert_eq "$VC/sessions" "$(readlink "$VA/sessions")" "a launch turns the old store into a link to ~/.codex/sessions"
assert_eq '{"r":"store"}' "$(cat "$VC/sessions/2026/09/03/rollout-store.jsonl" 2>/dev/null)" "…having merged its rollouts in"
assert_eq '{"r":"store"}' "$(cat "$VA"/sessions.pre-link.*/2026/09/03/rollout-store.jsonl 2>/dev/null)" "…and kept the original"
assert_eq "1" "$([ "$VA/k/sessions" -ef "$VC/sessions" ] && echo 1 || echo 0)" \
  "a home still linked at the old store resolves to ~/.codex/sessions"
assert_eq "$VA/sessions" "$(readlink "$VA/k/sessions")" "…and is left as it is — it already reads the view"

# --- a name only the home has is MOVED in, once it has settled ---------------
vnew; vhome m
printf '{"models":[]}\n' > "$VA/m/models_cache.json"; old_mtime "$VA/m/models_cache.json"
mkdir -p "$VA/m/.tmp/git-x/objects"; printf 'o\n' > "$VA/m/.tmp/git-x/objects/o1"; old_mtime "$VA/m/.tmp"
printf 'db' > "$VA/m/goals_1.sqlite"; printf 'wal' > "$VA/m/goals_1.sqlite-wal"; old_mtime "$VA/m/goals_1.sqlite" "$VA/m/goals_1.sqlite-wal"
printf 'mid-write\n' > "$VA/m/.tmpAbC123"                  # a temp file a live Codex is about to rename
out="$(vlaunch m 2>&1)"
assert_eq '{"models":[]}' "$(cat "$VC/models_cache.json" 2>/dev/null)" "a file only the home had is MOVED into ~/.codex"
assert_eq "$VC/models_cache.json" "$(readlink "$VA/m/models_cache.json")" "…and linked back"
assert_eq "o" "$(cat "$VC/.tmp/git-x/objects/o1" 2>/dev/null)" "a directory only the home had is moved in whole"
assert_eq "$VC/.tmp" "$(readlink "$VA/m/.tmp")" "…and linked back"
assert_eq "db" "$(cat "$VC/goals_1.sqlite" 2>/dev/null)" "a database only the home had moves in"
assert_eq "wal" "$(cat "$VC/goals_1.sqlite-wal" 2>/dev/null)" "…with its WAL beside it"
assert_eq "0" "$([ -e "$VA/m/goals_1.sqlite-wal" ] && echo 1 || echo 0)" "…and none left behind in the home"
assert_eq "0" "$([ -L "$VA/m/.tmpAbC123" ] && echo 1 || echo 0)" "a file written in the last minute is left where it is"
assert_eq "0" "$([ -e "$VC/.tmpAbC123" ] && echo 1 || echo 0)" "…never carried off mid-write"
assert_contains "$(va doctor --provider codex)" ".tmpAbC123" "…and the doctor still names it"
old_mtime "$VA/m/.tmpAbC123"
err="$(vlaunch m 2>&1 >/dev/null)"
assert_eq "$VC/.tmpAbC123" "$(readlink "$VA/m/.tmpAbC123")" "…until it has sat for a minute; then a later launch moves it"

# --- repair on every launch: a link a writer replaced is folded back ---------
vnew; vhome r
vlaunch r >/dev/null 2>&1
rm -f "$VA/r/history.jsonl"; printf '{"session_id":"r","ts":9,"text":"written while unlinked"}\n' > "$VA/r/history.jsonl"
err="$(vlaunch r 2>&1 >/dev/null)"
assert_contains "$(cat "$VC/history.jsonl")" "written while unlinked" \
  "a link replaced by a real file (a rename-rewrite) is merged back at the next launch"
assert_eq "$VC/history.jsonl" "$(readlink "$VA/r/history.jsonl")" "…and relinked"
assert_contains "$err" "appended" "…and the launch says so — the divergence is never silent"
printf 'new\n' > "$VC/rules.txt"
err="$(vlaunch r 2>&1 >/dev/null)"
assert_eq "$VC/rules.txt" "$(readlink "$VA/r/rules.txt")" "an entry ~/.codex grew since the last launch is linked at the next"
assert_eq "" "$err" "…quietly: tracking the view is not a repair"

# --- without perl the fold is the same; across filesystems it copies --------
# perl makes the merge one process and a few steps exact; every step has a
# plain-shell fallback. ANU_ACCOUNT_TEST_NO_PERL forces all of them at once.
vnew; vhome n
mkdir -p "$VA/n/sessions/2026/09/04"; printf '{"r":"n"}\n' > "$VA/n/sessions/2026/09/04/rollout-n.jsonl"
mkdir -p "$VA/n/sessions/2026/09/01"; printf '{"r":"n-canon"}\n' > "$VA/n/sessions/2026/09/01/rollout-canon.jsonl"
printf '{"session_id":"n","ts":5,"text":"five"}\n' > "$VA/n/history.jsonl"
ln -s /nowhere "$VA/n/AGENTS.md"
out="$(ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_TEST_NO_PERL=1 vlaunch n 2>&1)"
assert_eq '{"r":"n"}' "$(cat "$VC/sessions/2026/09/04/rollout-n.jsonl" 2>/dev/null)" "no perl: the rollout is merged in"
assert_eq "1" "$([ "$VA"/n/sessions.pre-link.*/2026/09/04/rollout-n.jsonl -ef "$VC/sessions/2026/09/04/rollout-n.jsonl" ] && echo 1 || echo 0)" \
  "…as a hard link"
assert_eq '{"r":"canon"}' "$(cat "$VC/sessions/2026/09/01/rollout-canon.jsonl")" "…never over a rollout ~/.codex has"
assert_contains "$(cat "$VC/history.jsonl")" '"text":"five"' "no perl: history lines are appended"
assert_eq "$VC/AGENTS.md" "$(readlink "$VA/n/AGENTS.md")" "no perl: a link elsewhere is replaced"
assert_eq "$VC/sessions" "$(readlink "$VA/n/sessions")" "…and every link is in place"
for noperl in "" 1; do
  vnew; vhome x
  mkdir -p "$VA/x/sessions/2026/09/05"; printf '{"r":"x"}\n' > "$VA/x/sessions/2026/09/05/rollout-x.jsonl"
  out="$(ANU_ACCOUNT_TEST=1 ANU_ACCOUNT_TEST_CROSS_DEVICE=1 ANU_ACCOUNT_TEST_NO_PERL="$noperl" vlaunch x 2>&1)"
  assert_eq '{"r":"x"}' "$(cat "$VC/sessions/2026/09/05/rollout-x.jsonl" 2>/dev/null)" \
    "across filesystems${noperl:+, without perl}: the rollout is COPIED in"
  assert_eq "0" "$([ "$VA"/x/sessions.pre-link.*/2026/09/05/rollout-x.jsonl -ef "$VC/sessions/2026/09/05/rollout-x.jsonl" ] && echo 1 || echo 0)" \
    "…a copy, since nothing can be linked there"
  assert_eq '{"r":"x"}' "$(cat "$VA"/x/sessions.pre-link.*/2026/09/05/rollout-x.jsonl 2>/dev/null)" "…and the original is kept"
done

# --- a share lock left by a process that died is reclaimed -------------------
vnew; vhome s
sleep 0 & deadpid=$!; wait "$deadpid" 2>/dev/null
mkdir -p "$VA/.share.lock"; printf '%s\n' "$deadpid" > "$VA/.share.lock/pid"
printf 'x' > "$VA/s/stale.json"; old_mtime "$VA/s/stale.json"
out="$(vlaunch s 2>&1)"
assert_eq "$VC/stale.json" "$(readlink "$VA/s/stale.json")" "a lock whose holder died does not wedge the share"
assert_eq "0" "$([ -e "$VA/.share.lock" ] && echo 1 || echo 0)" "…and the lock is released after"

# --- credentials never travel; a store linked the other way is left alone ---
vnew; vhome c
printf '{"tokens":"half-written"}\n' > "$VA/c/auth.json.tmp"; old_mtime "$VA/c/auth.json.tmp"
out="$(vlaunch c 2>&1)"
assert_eq "0" "$([ -e "$VC/auth.json.tmp" ] && echo 1 || echo 0)" \
  "a file named after auth.json is never moved into ~/.codex, however old"
assert_eq "0" "$([ -L "$VA/c/auth.json.tmp" ] && echo 1 || echo 0)" "…and stays the home's own"
d="$(mktmp)"; VH="$d/home"; VA="$d/acct"; VC="$VH/.codex"
mkdir -p "$VC" "$VA/sessions/2026"; printf 'r\n' > "$VA/sessions/2026/r.jsonl"
ln -s "$VA/sessions" "$VC/sessions"; vhome back; ln -s "$VA/sessions" "$VA/back/sessions"
out="$(vlaunch back 2>&1)"
assert_eq "1" "$([ -d "$VA/sessions" ] && [ ! -L "$VA/sessions" ] && echo 1 || echo 0)" \
  "a ~/.codex/sessions that is itself a link to the old store is left alone — no loop"
assert_eq "r" "$(cat "$VC/sessions/2026/r.jsonl" 2>/dev/null)" "…and ~/.codex/sessions still reads every rollout"

# --- doctor --fix folds a home in, exactly as a launch would -----------------
vnew; vhome f
printf 'db' > "$VA/f/memories_1.sqlite"
out="$(va doctor --provider codex)"; rc=$?
assert_eq "1" "$rc" "doctor names a home with a real database where the link belongs"
assert_contains "$out" "real, to fold into ~/.codex: memories_1.sqlite" "…by entry, and by what it is"
out="$(va doctor --provider codex --fix 2>&1)"; rc=$?
assert_ok $rc "doctor --fix exits 0 once every home is a view"
assert_eq "$VC/memories_1.sqlite" "$(readlink "$VA/f/memories_1.sqlite")" "…having folded the home in"
assert_contains "$(va doctor --provider claude)" "no home to check" "doctor --provider claude has nothing to check"

# ---------------------------------------------------------------------------
t_section "3. launch — the CLI is pointed at a home, never handed a secret"

# The codex stub, in its LAUNCH role: print the argv and the environment that
# actually reached the process, so every assertion below is about the real
# exec, not about what anu meant to do.
stub "$SD" codex '
case "$1 $2" in
  "login --device-auth") printf "codex %s\n" "$*" >> "'"$LOG"'"
                         printf "CODEX_HOME=%s\n" "${CODEX_HOME:-}" >> "'"$LOG"'"
                         [ -n "${CODEX_LOGIN_FAIL:-}" ] && exit 1
                         printf "{\"tokens\":\"x\"}\n" > "$CODEX_HOME/auth.json"; exit 0 ;;
esac
printf "codex %s\n" "$*"
printf "env CODEX_HOME=%s ANU_ACCOUNT=%s NONCE=%s OPENAI_API_KEY=%s CLAUDE_TOKEN=%s CWD=%s PROVIDER=%s\n" \
  "${CODEX_HOME:-}" "${ANU_ACCOUNT:-}" "${ANU_LAUNCH_NONCE:-}" "${OPENAI_API_KEY:-}" \
  "${CLAUDE_CODE_OAUTH_TOKEN:-}" "$PWD" "${ANU_PROVIDER:-}"'
stub "$SD" tmux 'printf "tmux %s\n" "$*" >> "'"$LOG"'"'
"$ACCOUNT" add --provider codex account_gamma >/dev/null 2>&1

: > "$LOG"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --as account_alpha --need any -- --full-auto)"; rc=$?
assert_ok $rc "launch --provider codex exits 0"
assert_contains "$out" "codex --full-auto" "…execs codex with the caller's args"
assert_contains "$out" "CODEX_HOME=$ANU_ACCOUNT_CODEX_DIR/account_alpha" "…with CODEX_HOME pointed at that account's home"
assert_contains "$out" "ANU_ACCOUNT=account_alpha" "…and ANU_ACCOUNT naming it"
assert_not_contains "$out" "OPENAI_API_KEY=sk" "an API key never survives into the child (it would bill the wrong account)"
log="$(cat "$LOG")"
assert_contains "$log" "set-option -p -t %7 @anu_provider codex" "stamps the provider"
assert_contains "$log" "set-option -p -t %7 @anu_account account_alpha" "stamps the account"
assert_contains "$log" "set-option -p -t %7 @anu_launch --full-auto" "stamps the replayable args"
assert_contains "$log" "set-option -p -t %7 @anu_need any" "stamps the need"
assert_match "$log" "set-option -p -t %7 @anu_launch_nonce [0-9a-zA-Z]" "stamps a launch nonce"

# The nonce the pane carries IS the one the child inherits — that pairing is
# what lets a hook tell its own launch from a delayed one.
nonce_stamped="$(printf '%s\n' "$log" | sed -n 's/.*@anu_launch_nonce //p' | tail -1)"
nonce_env="$(printf '%s\n' "$out" | sed -n 's/.*NONCE=\([^ ]*\).*/\1/p' | tail -1)"
assert_eq "$nonce_stamped" "$nonce_env" "the stamped nonce and the exported one are the same value"
assert_ne "" "$nonce_env" "…and it is not empty"

# A launch that actually ASKED the dashboard remembers its pick — keyed by
# provider, so a codex answer can never be replayed for a claude launch.
: > "$LOG"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto)"
assert_contains "$out" "ANU_ACCOUNT=account_alpha" "no --as: the codex pick decides"
assert_contains "$(cat "$LOG")" "@anu_last_pick codex:any:account_alpha:" \
  "…and the remembered pick is namespaced by provider AND need"

t_section "3b. a launch clears the old identity — 'session id unknown' is a state"
assert_contains "$log" "set-option -pu -t %7 @anu_session" "a fresh launch clears @anu_session"
assert_contains "$log" "set-option -pu -t %7 @anu_transcript" "…@anu_transcript"
assert_contains "$log" "set-option -pu -t %7 @anu_rollout_offset" "…@anu_rollout_offset"
assert_contains "$log" "set-option -pu -t %7 @anu_wall_pending" "…and @anu_wall_pending (only a launch clears a pending wall)"

# …except the relaunch a rotate/switch types, which carries --keep-wall. Its
# wall stays pending until that relaunch is confirmed ALIVE — the rotation
# clears it itself then — so a relaunch that dies on a dead login leaves the
# session still reading walled instead of erasing the only evidence it is.
: > "$LOG"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --as account_alpha --need any --keep-wall -- resume sid-9 "go on")"; rc=$?
assert_ok $rc "launch --keep-wall exits 0"
assert_not_contains "$(cat "$LOG")" "@anu_wall_pending" "--keep-wall leaves @anu_wall_pending exactly as it is"
assert_contains "$(cat "$LOG")" "set-option -pu -t %7 @anu_session" "…while the rest of the old identity is still cleared"
assert_contains "$out" "codex resume sid-9 go on" "…and the resume runs as ever"
out="$("$ACCOUNT" launch --keep-wall -- --model x 2>&1)"; rc=$?
assert_fail $rc "--keep-wall is refused on the claude path"
assert_contains "$out" "codex-only" "…and says why"

# …except when the caller said which conversation this is: `codex resume <id>`
# carries the expected identity, and it is stamped up front for the hook to
# confirm later.
: > "$LOG"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --as account_alpha --need any -- resume 0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee "pick it back up")"
assert_contains "$out" "codex resume 0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee pick it back up" \
  "the continuation travels as the resume ARGUMENT, never typed into a composer"
log="$(cat "$LOG")"
assert_contains "$log" "set-option -p -t %7 @anu_session 0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee" \
  "…and the resumed id is stamped as the expected identity"
line="$(grep -F '@anu_launch ' "$LOG" | tail -1)"
assert_eq "tmux set-option -p -t %7 @anu_launch " "$line" \
  "@anu_launch drops the resume id and the continuation positional — rotate supplies its own"

t_section "3c. @anu_launch is a whitelist of codex flags worth replaying"
: > "$LOG"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --as account_alpha -- --model gpt-5 --sandbox workspace-write -a never --yolo -C /tmp --profile p1 --unknown x "a prompt")"
line="$(grep -F '@anu_launch ' "$LOG" | tail -1)"
assert_contains "$line" "--model gpt-5" "--model survives with its value"
assert_contains "$line" "--sandbox workspace-write" "--sandbox too"
assert_contains "$line" "-a never" "-a (ask-for-approval) too"
assert_contains "$line" "--yolo" "--yolo survives"
assert_contains "$line" "-C /tmp" "-C survives"
assert_contains "$line" "--profile p1" "--profile survives"
assert_not_contains "$line" "--unknown" "an unrecognized flag is dropped…"
assert_not_contains "$line" "a prompt" "…and so is the bare positional (the prompt is already in the rollout)"

t_section "3d. --cd, and the distinct failure outcomes"
run_dir="$(mktmp)/work"; mkdir -p "$run_dir"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --as account_alpha --cd "$run_dir" -- --full-auto)"
assert_contains "$out" "CWD=$run_dir" "--cd runs codex in the session's own cwd (a resume elsewhere opens a modal)"

out="$("$ACCOUNT" launch --provider codex --as nosuch -- --full-auto 2>&1)"; rc=$?
assert_eq "1" "$rc" "a named account with no home on this device exits exactly 1"
assert_contains "$out" "nosuch" "…and names it"
assert_contains "$out" "add --provider codex" "…and says how to fix it"

# Pool exhausted is a real, distinguishable fact — exit 3, never a silent
# fall-through to whatever ~/.codex happens to be logged in as.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[],"out":[{"id":"codex-account_alpha","why":"week_all 100"}],"generatedAt":"2026-09-16T00:00:00Z"}
JSON
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex -- --full-auto 2>&1)"; rc=$?
assert_eq "3" "$rc" "nothing has room exits 3"
assert_not_contains "$out" "keychain" "…and there is no keychain fallback for codex — there is no keychain"
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_alpha","name":"account_alpha","email":"alpha@example.invalid","resetsAt":"2026-09-20T02:59:59Z","remaining":71,"shared":false},
 {"id":"codex-account_gamma","name":"account_gamma","email":"t@x.com","resetsAt":"2026-09-21T02:59:59Z","remaining":40,"shared":false}],
 "out":[{"id":"codex-account_beta","why":"week_all 100"}],"generatedAt":"2026-09-16T00:00:00Z"}
JSON

out="$("$ACCOUNT" launch --provider codex --need fable -- --full-auto 2>&1)"; assert_fail $? "--need fable is refused on the codex path"
out="$("$ACCOUNT" launch --provider codex --box -- --full-auto 2>&1)"; assert_fail $? "--box is refused: no hook can run in a box, so a wall there is invisible"
assert_not_contains "$(cat "$LOG")" "anu-secrets" "no launch on the codex path ever reaches for a token"

# ---------------------------------------------------------------------------
t_section "3e. the pick is intersected with the accounts logged in HERE"
# The live bug: the chooser's top pick had no login on this laptop, `launch`
# took it anyway, and `cdxx` refused outright — on a machine with two
# perfectly good codex accounts. The dashboard ranks a POOL; only this device
# knows which of them it can actually authenticate as, which is the same
# intersection rotation has always made.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_epsilon","name":"account_epsilon","email":"g@x.com","resetsAt":"2026-09-24T02:59:59Z","remaining":90,"shared":false},
 {"id":"codex-account_delta","name":"account_delta","email":"q@x.com","resetsAt":"2026-09-25T02:59:59Z","remaining":55,"shared":false}],
 "out":[],"generatedAt":"2026-09-23T00:00:00Z"}
JSON
"$ACCOUNT" add --provider codex account_delta >/dev/null 2>&1
assert_eq "0" "$([ -f "$ANU_ACCOUNT_CODEX_DIR/account_epsilon/auth.json" ] && echo 1 || echo 0)" \
  "account_epsilon — the top pick — has no login on this device"

: > "$LOG"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_ok $rc "launch exits 0 rather than refusing on the top pick"
assert_contains "$out" "ANU_ACCOUNT=account_delta" "…it walks down to the first ranked account logged in here"
assert_contains "$out" "CODEX_HOME=$ANU_ACCOUNT_CODEX_DIR/account_delta" "…and points the CLI at that home"
assert_contains "$out" "skipping account_epsilon" "…saying which ranked account it passed over"
assert_contains "$(cat "$LOG")" "set-option -p -t %7 @anu_account account_delta" "…and stamps the account it actually landed on"

# …and only refuses when NONE of the ranked accounts is usable here.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_epsilon","name":"account_epsilon","email":"g@x.com","resetsAt":"2026-09-24T02:59:59Z","remaining":90,"shared":false},
 {"id":"codex-fixture_owner","name":"fixture_owner","email":"a@x.com","resetsAt":"2026-09-25T02:59:59Z","remaining":55,"shared":false},
 {"id":"codex-account_beta","name":"account_beta","email":"d@x.com","resetsAt":"2026-09-26T02:59:59Z","remaining":20,"shared":false}],
 "out":[],"generatedAt":"2026-09-23T00:00:00Z"}
JSON
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_fail $rc "with no ranked account logged in here, launch refuses"
assert_eq "1" "$rc" "…exit 1 — a local credential problem, not an exhausted pool (that is still 3)"
assert_contains "$out" "account_epsilon, fixture_owner, account_beta" "…the message names the ranked accounts, in rank order"
assert_contains "$out" "none is logged in on this device" "…and says what is actually wrong"
assert_contains "$out" "com8-account add --provider codex" "…with the command that fixes it"
# The hint names an account this device really is logged in as — whichever
# one it is; the invariant is that following the advice would work.
suggested="$(printf '%s' "$out" | sed -n 's/.*--as \([A-Za-z0-9_-]*\).*/\1/p' | tail -1)"
assert_ne "" "$suggested" "…and points at a specific account to use instead"
assert_file "$ANU_ACCOUNT_CODEX_DIR/$suggested/auth.json" \
  "…one that really is logged in here, so following the advice works"

# An exhausted pool is still its own, different answer.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[],"out":[{"id":"codex-account_delta","why":"week_all 100"}],"generatedAt":"2026-09-23T00:00:00Z"}
JSON
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex -- --full-auto 2>&1)"; rc=$?
assert_eq "3" "$rc" "the dashboard ranking nothing at all is still exit 3"
assert_contains "$out" "no codex account has room" "…with its own message"

# An explicit --as is unchanged: the human named it, so it is not second-guessed.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_epsilon","name":"account_epsilon","email":"g@x.com","resetsAt":"2026-09-24T02:59:59Z","remaining":90,"shared":false},
 {"id":"codex-account_delta","name":"account_delta","email":"q@x.com","resetsAt":"2026-09-25T02:59:59Z","remaining":55,"shared":false}],
 "out":[],"generatedAt":"2026-09-23T00:00:00Z"}
JSON
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --as account_delta -- --full-auto 2>&1)"
assert_contains "$out" "ANU_ACCOUNT=account_delta" "--as still launches exactly the named account"
assert_not_contains "$out" "skipping" "…without walking anything"
out="$("$ACCOUNT" launch --provider codex --as account_epsilon -- --full-auto 2>&1)"; rc=$?
assert_eq "1" "$rc" "--as an account with no home here still exits 1"
assert_contains "$out" "no codex home for 'account_epsilon'" "…with today's message, unchanged"

# Put the suite's standard ranking back — the rotate section below reads it.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_alpha","name":"account_alpha","email":"alpha@example.invalid","resetsAt":"2026-09-20T02:59:59Z","remaining":71,"shared":false},
 {"id":"codex-account_gamma","name":"account_gamma","email":"t@x.com","resetsAt":"2026-09-21T02:59:59Z","remaining":40,"shared":false}],
 "out":[{"id":"codex-account_beta","why":"week_all 100"}],"generatedAt":"2026-09-16T00:00:00Z"}
JSON

t_section "3f. an inherited ANU_ACCOUNT belongs to ONE provider"
# Every shell opened from a Claude pane inherits that pane's
# ANU_ACCOUNT=<claude account>. `launch --provider codex` used to read it as
# an explicit codex `--as`, so `cdxx` in a Claude pane died with
# "no codex home for 'account_alpha'" — a CLAUDE account name — and the ranked
# pick and its local intersection never ran at all. An inherited name is only
# an answer for the provider that exported it.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_delta","name":"account_delta","email":"q@x.com","resetsAt":"2026-09-25T02:59:59Z","remaining":80,"shared":false},
 {"id":"codex-account_gamma","name":"account_gamma","email":"t@x.com","resetsAt":"2026-09-26T02:59:59Z","remaining":40,"shared":false}],
 "out":[],"generatedAt":"2026-09-23T00:00:00Z"}
JSON

: > "$LOG"
out="$(ANU_ACCOUNT=account_alpha ANU_PROVIDER=claude TMUX_PANE=%7 \
  "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_ok $rc "a codex launch from a CLAUDE pane's shell does not die on the claude account"
assert_contains "$out" "ANU_ACCOUNT=account_delta" "…it picks and intersects as if nothing were inherited"
assert_not_contains "$out" "no codex home for 'account_alpha'" "…never treating a claude name as a codex --as"

# A pre-port pane exported no ANU_PROVIDER at all. For codex that is still
# not an answer: only a launch that said `codex` can speak for one.
: > "$LOG"
out="$(ANU_ACCOUNT=account_alpha TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"
assert_contains "$out" "ANU_ACCOUNT=account_delta" "an inherited name with NO provider is ignored by the codex path too"

# …but a codex pane's own account is honoured, and costs no dashboard call.
: > "$LOG"
out="$(ANU_ACCOUNT=account_gamma ANU_PROVIDER=codex TMUX_PANE=%7 \
  "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"
assert_contains "$out" "ANU_ACCOUNT=account_gamma" "ANU_PROVIDER=codex: the inherited account IS honoured"
assert_not_contains "$(cat "$LOG")" "usage/pick" "…and the dashboard is not asked at all"

t_section "3g. a launch says which provider its account belongs to"
: > "$LOG"
out="$(TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --as account_delta -- --full-auto 2>&1)"
assert_contains "$out" "PROVIDER=codex" "a codex launch exports ANU_PROVIDER=codex beside ANU_ACCOUNT"

# The last-pick memory follows the same test: a pick this launch actually
# made is worth remembering, an inherited account is not this launch's pick.
: > "$LOG"
out="$(ANU_ACCOUNT=account_gamma ANU_PROVIDER=codex TMUX_PANE=%7 \
  "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"
assert_not_contains "$(cat "$LOG")" "@anu_last_pick" "an honoured inherited account is not recorded as a pick"
: > "$LOG"
out="$(ANU_ACCOUNT=account_alpha ANU_PROVIDER=claude TMUX_PANE=%7 \
  "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"
assert_contains "$(cat "$LOG")" "@anu_last_pick codex:any:account_delta:" \
  "…but an IGNORED one leaves a real pick behind, which is"

# Put the suite's standard ranking back — the rotate section below reads it.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_alpha","name":"account_alpha","email":"alpha@example.invalid","resetsAt":"2026-09-20T02:59:59Z","remaining":71,"shared":false},
 {"id":"codex-account_gamma","name":"account_gamma","email":"t@x.com","resetsAt":"2026-09-21T02:59:59Z","remaining":40,"shared":false}],
 "out":[{"id":"codex-account_beta","why":"week_all 100"}],"generatedAt":"2026-09-16T00:00:00Z"}
JSON

t_section "3h. dashboard unreachable — never strictly worse than plain codex"
# The live case: a laptop back from an OS upgrade with Tailscale stopped, the
# tailnet-only dashboard timing out, and `cdxx` exiting 1 while plain `codex`
# worked. Offline, a launch falls back to a home logged in HERE — in a fixed
# order, each through the rotation preflight — and stays a MANAGED launch.
# Isolated homes and cache, so the order below is about these fixtures only.
SAVE_DIR="$ANU_ACCOUNT_CODEX_DIR" SAVE_CACHE="$ANU_ACCOUNT_CACHE"
export ANU_ACCOUNT_CODEX_DIR="$(mktmp)/codex-offline"
export ANU_ACCOUNT_CACHE="$(mktmp)/offline-state/tokens.json"
OSTATE="$(dirname "$ANU_ACCOUNT_CACHE")"; mkdir -p "$OSTATE"
for n in alpha beta gamma; do "$ACCOUNT" add --provider codex "$n" >/dev/null 2>&1; done
assert_file "$ANU_ACCOUNT_CODEX_DIR/gamma/auth.json" "three homes logged in on this device"
mode_of() { ls -l "$1" | cut -c1-10; }

# (1) the last pick, under 10 minutes old
printf 'beta %s\n' "$(date +%s)" > "$OSTATE/last-pick.codex-any"
: > "$LOG"
out="$(CURL_FAIL=1 TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_ok $rc "dashboard unreachable + a fresh last pick: the launch goes ahead"
assert_contains "$out" "ANU_ACCOUNT=beta" "…as the last pick"
assert_contains "$out" "anu account: dashboard unreachable — launching as beta without a room check" \
  "…saying exactly once that there was no room check"
assert_eq "1" "$(printf '%s\n' "$out" | grep -c 'without a room check')" "…once"
assert_contains "$out" "CODEX_HOME=$ANU_ACCOUNT_CODEX_DIR/beta" "…pointed at that home"
assert_contains "$out" "PROVIDER=codex" "…exporting ANU_PROVIDER"
log="$(cat "$LOG")"
assert_contains "$log" "set-option -p -t %7 @anu_account beta" "…and MANAGED: the account is stamped"
assert_contains "$log" "set-option -p -t %7 @anu_provider codex" "…the provider too"
assert_match "$log" "set-option -p -t %7 @anu_launch_nonce [0-9a-zA-Z]" "…and a launch nonce, so the hooks will stamp"
assert_not_contains "$log" "set-option -p -t %7 @anu_last_pick" "an offline launch is not a pick — the 10-minute clock is not refreshed"
assert_eq "beta" "$(cut -d' ' -f1 "$OSTATE/last-launch.codex")" "the launch leaves a last-launch stamp naming its home"
assert_eq "-rw-------" "$(mode_of "$OSTATE/last-launch.codex")" "…mode 0600"

# The pick call is bounded tighter than a usage read.
: > "$LOG.args"
CURL_FAIL=1 "$ACCOUNT" launch --provider codex --as alpha -- --full-auto >/dev/null 2>&1
CURL_FAIL=1 "$ACCOUNT" pick --provider codex >/dev/null 2>&1
assert_contains "$(grep 'usage/pick' "$LOG.args")" "--connect-timeout 3 -m 6" \
  "a pick waits 3s to connect and 6s in all, so a dead tailnet costs seconds"
CURL_FAIL=1 "$ACCOUNT" ls --provider codex >/dev/null 2>&1
assert_contains "$(grep -v 'usage/pick' "$LOG.args")" "-m 15" "…while ls keeps ANU_USAGE_TIMEOUT's 15"
assert_contains "$(ANU_PICK_TIMEOUT=2 CURL_FAIL=1 "$ACCOUNT" pick --provider codex 2>&1; grep 'usage/pick' "$LOG.args" | tail -1)" \
  "-m 2" "ANU_PICK_TIMEOUT overrides the pick bound"

# (2) the last pick is stale → the home this device last launched
printf 'beta %s\n' "$(( $(date +%s) - 700 ))" > "$OSTATE/last-pick.codex-any"
printf 'gamma %s\n' "$(( $(date +%s) - 86400 ))" > "$OSTATE/last-launch.codex"
out="$(CURL_FAIL=1 TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_ok $rc "stale last pick + a last-launch stamp: the launch goes ahead"
assert_contains "$out" "ANU_ACCOUNT=gamma" "…as the last-launched home (a day old is fine — it answers 'most recent')"
assert_contains "$out" "launching as gamma without a room check" "…with the warning"

# …unless this pane recorded that home walled within 30 minutes.
stub "$SD" tmux '
printf "tmux %s\n" "$*" >> "'"$LOG"'"
case "$*" in *"show-options -pqv -t %7 @anu_tried_codex"*) echo "gamma:$(date +%s)" ;; esac'
printf 'gamma %s\n' "$(date +%s)" > "$OSTATE/last-launch.codex"
out="$(CURL_FAIL=1 TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"
assert_contains "$out" "ANU_ACCOUNT=alpha" "a last-launched home this pane just bounced off is passed over"
stub "$SD" tmux 'printf "tmux %s\n" "$*" >> "'"$LOG"'"'

# (3) neither → the first logged-in home by name that passes the preflight
rm -f "$OSTATE/last-pick.codex-any" "$OSTATE/last-launch.codex"
out="$(CURL_FAIL=1 TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_ok $rc "no last pick, no stamp: the launch still goes ahead"
assert_contains "$out" "ANU_ACCOUNT=alpha" "…as the first home by name"
rm -f "$OSTATE/last-launch.codex"
mv "$ANU_ACCOUNT_CODEX_DIR/alpha/config.toml" "$ANU_ACCOUNT_CODEX_DIR/alpha/config.toml.off"
out="$(CURL_FAIL=1 TMUX_PANE=%7 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"
assert_contains "$out" "skipping alpha" "a home that fails the preflight says why…"
assert_contains "$out" "ANU_ACCOUNT=beta" "…and the next by name is launched instead"
mv "$ANU_ACCOUNT_CODEX_DIR/alpha/config.toml.off" "$ANU_ACCOUNT_CODEX_DIR/alpha/config.toml"

# The stamp is written only by a launch that reaches exec.
printf 'gamma 1\n' > "$OSTATE/last-launch.codex"
CURL_FAIL=1 "$ACCOUNT" launch --provider codex --as nosuch -- --full-auto >/dev/null 2>&1
CURL_FAIL=1 "$ACCOUNT" launch --provider codex --as beta --cd "$(mktmp)/missing" -- --full-auto >/dev/null 2>&1
assert_eq "gamma 1" "$(cat "$OSTATE/last-launch.codex")" "a launch that dies leaves the stamp untouched"

# Homes exist but none survives the preflight: its own exit 1, not plain codex.
for n in alpha beta gamma; do mv "$ANU_ACCOUNT_CODEX_DIR/$n/config.toml" "$ANU_ACCOUNT_CODEX_DIR/$n/config.toml.off"; done
out="$(CURL_FAIL=1 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_eq "1" "$rc" "offline with homes that all fail the preflight exits 1"
assert_contains "$out" "no codex home logged in here passes the preflight" "…naming that, after each skip reason"
for n in alpha beta gamma; do mv "$ANU_ACCOUNT_CODEX_DIR/$n/config.toml.off" "$ANU_ACCOUNT_CODEX_DIR/$n/config.toml"; done

# No codex home on this device at all: today's message, exit 1 — the case
# `cdx` itself turns into plain codex.
export ANU_ACCOUNT_CODEX_DIR="$(mktmp)/codex-none"; mkdir -p "$ANU_ACCOUNT_CODEX_DIR"
out="$(CURL_FAIL=1 "$ACCOUNT" launch --provider codex --need any -- --full-auto 2>&1)"; rc=$?
assert_eq "1" "$rc" "offline with no codex home at all exits 1"
assert_contains "$out" "dashboard unreachable — pass --as <name> to launch a codex pane anyway" "…with today's message"
assert_not_contains "$out" "without a room check" "…and launches nothing"
export ANU_ACCOUNT_CODEX_DIR="$SAVE_DIR" ANU_ACCOUNT_CACHE="$SAVE_CACHE"

t_section "4. the hooks — identity from Codex itself, bound to a launch"

# A tmux that both records and ANSWERS: the nonce gate reads the pane's own
# @anu_launch_nonce back, so the stub has to serve it.
stub "$SD" tmux '
printf "tmux %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  show-options) case "$*" in
    *@anu_launch_nonce*) printf "%s\n" "${OPT_NONCE-n1}" ;;
    *) printf "\n" ;; esac ;;
esac
exit 0'
ROLL="$(mktmp)/rollout.jsonl"
printf '{"type":"session_meta"}\n{"type":"event_msg"}\n' > "$ROLL"
ROLL_SIZE="$(wc -c < "$ROLL" | tr -d ' ')"

: > "$LOG"
printf '{"session_id":"sid-1","transcript_path":"%s","source":"startup","hook_event_name":"SessionStart"}' "$ROLL" \
  | ANU_LAUNCH_NONCE=n1 TMUX_PANE=%3 "$ACCOUNT" hook codex session-start
assert_ok $? "session-start always exits 0"
log="$(cat "$LOG")"
assert_contains "$log" "set-option -p -t %3 @anu_session sid-1" "stamps the session id Codex minted"
assert_contains "$log" "set-option -p -t %3 @anu_transcript $ROLL" "…and the rollout it writes to"
assert_contains "$log" "set-option -p -t %3 @anu_rollout_offset $ROLL_SIZE" \
  "…with the cursor at the file's CURRENT size"

# A resume replays the whole conversation, old wall record included. That is
# history, not fresh evidence — so the cursor starts at the end there too.
: > "$LOG"
printf '{"session_id":"sid-1","transcript_path":"%s","source":"resume","hook_event_name":"SessionStart"}' "$ROLL" \
  | ANU_LAUNCH_NONCE=n1 TMUX_PANE=%3 "$ACCOUNT" hook codex session-start
assert_contains "$(cat "$LOG")" "@anu_rollout_offset $ROLL_SIZE" \
  "a RESUME sets the cursor to the end too — a replayed wall is history, never a fresh wall"

t_section "4b. the nonce gate — a delayed hook never re-stamps"
: > "$LOG"
printf '{"session_id":"sid-OLD","transcript_path":"%s","source":"startup"}' "$ROLL" \
  | ANU_LAUNCH_NONCE=n0 TMUX_PANE=%3 "$ACCOUNT" hook codex session-start
assert_ok $? "a hook from a previous launch still exits 0"
assert_eq "" "$(grep -F '@anu_session' "$LOG" || true)" "…and stamps nothing at all"
: > "$LOG"
printf '{"session_id":"sid-x","transcript_path":"%s","source":"startup"}' "$ROLL" \
  | TMUX_PANE=%3 "$ACCOUNT" hook codex session-start
assert_eq "" "$(grep -F '@anu_session' "$LOG" || true)" "a codex anu never launched (no nonce at all) is never stamped"

t_section "4c. user-prompt — which turn is in flight"
: > "$LOG"
printf '{"session_id":"sid-1","turn_id":"turn-7","hook_event_name":"UserPromptSubmit"}' \
  | ANU_LAUNCH_NONCE=n1 TMUX_PANE=%3 "$ACCOUNT" hook codex user-prompt
assert_ok $? "user-prompt exits 0"
log="$(cat "$LOG")"
assert_contains "$log" "set-option -p -t %3 @anu_turn turn-7" "stamps the turn id"
assert_contains "$log" "set-option -p -t %3 @anu_prompted 1" "…and that this session has been prompted at all"
: > "$LOG"
printf '{"turn_id":"turn-8"}' | ANU_LAUNCH_NONCE=nope TMUX_PANE=%3 "$ACCOUNT" hook codex user-prompt
assert_eq "" "$(grep -F '@anu_turn' "$LOG" || true)" "the nonce gate covers user-prompt too"

# A hook must never block or break the CLI it runs inside.
: > "$LOG"
printf 'not json at all' | ANU_LAUNCH_NONCE=n1 TMUX_PANE=%3 "$ACCOUNT" hook codex session-start
assert_ok $? "unparseable stdin is a silent no-op, not an error"
: > "$LOG"
printf '{"session_id":"sid-1"}' | ANU_LAUNCH_NONCE=n1 "$ACCOUNT" hook codex session-start
assert_ok $? "no pane to stamp is a silent no-op too"
assert_eq "" "$(cat "$LOG")" "…and really touches nothing (TMUX_PANE is unset for the whole suite)"
out="$(printf '{}' | ANU_LAUNCH_NONCE=n1 TMUX_PANE=%3 "$ACCOUNT" hook codex nonsense 2>&1)"
assert_ok $? "an unknown codex hook sub-verb never fails the CLI"

# ---------------------------------------------------------------------------
t_section "6. rotate — leave the old process, resume on the next account"

ROTCWD="$(mktmp)/repo"; mkdir -p "$ROTCWD"
# `pane state` resolves the target and reports the wall, exactly as it does on
# the claude path. A codex wall is always `session`: the rollout record says a
# limit was hit and never which window.
stub "$SD" pane '
printf "pane %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  state)
    shift; json=0; t=""
    for a in "$@"; do case "$a" in --json) json=1 ;; *) [ -z "$t" ] && t="$a" ;; esac; done
    case "$t" in %*) p="$t" ;; *) p="%4" ;; esac
    st="${PANE_STATE:-limited}"
    if [ "$json" = 1 ]; then echo "{\"pane\":\"$p\",\"state\":\"$st\",\"cli\":\"codex\",\"wall\":\"session\"}"
    else echo "$st"; fi ;;
  send) exit 0 ;;
esac'
stub "$SD" tmux '
printf "tmux %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  show-options) case "$*" in
    *@anu_provider*)     echo "${OPT_PROVIDER-codex}" ;;
    *@anu_account*)      echo "${OPT_ACCOUNT-account_beta}" ;;
    *@anu_session*)      echo "${OPT_SESSION-sid-1}" ;;
    *@anu_need*)         echo "any" ;;
    *@anu_launch_nonce*) echo "n1" ;;
    *@anu_launch*)       echo "--full-auto" ;;
    *@anu_box*)          echo "0" ;;
    *@anu_rotating*)     echo "${OPT_ROTATING-}" ;;
    *@anu_tried_codex*)  echo "${OPT_TRIED-}" ;;
    *@anu_tried*)        echo "" ;;
    *@anu_generation*)   echo "${OPT_GENERATION:-1000}" ;;
    *) echo ;; esac ;;
  display-message) case "$*" in
    *socket_path*)       echo "/tmp/tmux-test/default" ;;
    *pane_tty*)          echo "/dev/ttyTEST" ;;
    *pane_current_path*) echo "'"$ROTCWD"'" ;;
    *pane_current_command*)
      # What the pane is RUNNING: codex until it is asked to leave, then
      # whatever is actually left. With no CMD_LEFT override that is the
      # honest answer — codex while the old process is still alive, bash once
      # it is gone — so the Ctrl-C-ignored case really does have to signal it.
      if grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'"; then echo "${CMD_AFTER-codex}"
      elif grep -q "send-keys -t .* C-c" "'"$LOG"'"; then
        if [ -n "${CMD_LEFT:-}" ]; then echo "$CMD_LEFT"
        elif [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then echo "codex"
        else echo "bash"; fi
      else echo "codex"; fi ;;
    *) echo ;; esac ;;
  send-keys)
    case "$*" in
      *" C-c"*) [ "${CC_KILLS:-1}" = 1 ] && [ -n "${OLD_PID:-}" ] && kill "$OLD_PID" 2>/dev/null ;;
    esac ;;
  capture-pane) echo "" ;;
esac
exit 0'
# `ps` is how the rotation learns WHICH process it is leaving: the pid on the
# pane's tty whose command is the pane's own foreground command.
stub "$SD" ps '
if grep -q "@ACCOUNT_LAUNCH@ launch" "'"$LOG"'"; then printf "  %s codex\n" "${NEW_PID:-1}"
else printf "  %s codex\n" "${OLD_PID:-1}"; fi'
stub "$SD" sleep 'exit 0'

# A stand-in for the codex process the rotation is leaving. stdout/stderr
# are closed on purpose: a background job that inherits the command
# substitution's pipe keeps it open, and `OLD_PID="$(spawn_fake)"` would
# then block for the whole sleep.
spawn_fake() { /bin/sleep 30 >/dev/null 2>&1 </dev/null & printf '%s' "$!"; }

: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$("$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "rotate exits 0 on the codex path"
log="$(cat "$LOG")"
assert_contains "$log" "pane state %4 --json" "…having confirmed the wall from the pane"
assert_contains "$log" "provider=openai" "…and asked the dashboard for a CODEX account"
assert_contains "$log" "exclude=account_beta" "…excluding the one it is leaving"
assert_match "$log" "send-keys -t %4 C-c.*send-keys -t %4 C-c" "leaves with Ctrl-C twice"
assert_not_contains "$log" "/exit" "never /exit — the tested build ignored it"
assert_not_contains "$log" "send-keys -t %4 -l" "and never types text into the pane"
assert_contains "$log" "send-keys -t %4 printf '\033[2J\033[3J\033[H' && @ACCOUNT_LAUNCH@ launch --provider codex --as account_alpha --need any --cd $ROTCWD --keep-wall -- resume sid-1 " \
  "the relaunch line resumes the session under the pick, in the session's own cwd, keeping the wall until it is up"
assert_contains "$log" "resume sid-1 You\\ were\\ resumed" \
  "…with the continuation as the resume ARGUMENT, never typed into a composer"
assert_not_contains "$log" "pane send %4" "nothing is sent to the pane afterwards — the argument already carried it"
assert_contains "$log" "@anu_tried_codex account_beta:" "the tried-list is namespaced by provider"
assert_contains "$out" "account_beta → account_alpha" "…and the move is reported"

t_section "6b. the old process is verified gone, or signalled"
# Ctrl-C that the TUI ignores: the VERIFIED old pid is terminated, never the
# pane, and never a pid the rotation did not itself read off the tty.
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(CC_KILLS=0 "$ACCOUNT" rotate %4 2>&1)"; rc=$?
/bin/sleep 0.4
assert_eq "0" "$(kill -0 "$OLD_PID" 2>/dev/null && echo 1 || echo 0)" \
  "a codex that ignored Ctrl-C is terminated by pid"
assert_ok $rc "…and the rotation still completes"

t_section "6c. a pane that will not come back to a shell is PARKED"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(CC_KILLS=1 CMD_LEFT=vim "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "2" "$rc" "a pane that is not back at a shell parks (exit 2)"
assert_not_contains "$(cat "$LOG")" "@ACCOUNT_LAUNCH@ launch" "…and nothing is relaunched on top of it"
assert_contains "$out" "%4" "…the message names the pane"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

t_section "6d. the destination is preflighted BEFORE the pane is touched"
# account_alpha's hook block, broken: a candidate whose hooks Codex would refuse to
# run is not a place to land — a pane there would wall again with no evidence.
cp "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml" "$FIX/account_alpha.toml.bak"
sed -i.bak 's/trusted_hash = "sha256:[0-9a-f]*"/trusted_hash = "sha256:deadbeef"/' \
  "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$("$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "the next candidate is taken instead"
assert_contains "$out" "→ account_gamma" "…skipping the account whose hooks are not trusted"
cp "$FIX/account_alpha.toml.bak" "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

# Directory trust is per home: the first launch in a directory a home has not
# seen opens a modal no unattended rotation can answer. So the preflight WRITES
# the row — but never over a value a human set.
assert_contains "$(cat "$ANU_ACCOUNT_CODEX_DIR/account_gamma/config.toml")" "[projects.\"$ROTCWD\"]" \
  "the preflight pre-writes the destination's directory trust"
assert_contains "$(cat "$ANU_ACCOUNT_CODEX_DIR/account_gamma/config.toml")" 'trust_level = "trusted"' "…as trusted"

# An earlier rotation already trusted this directory in account_alpha's home (the
# preflight writes the row), so "the human said otherwise" is that SAME row
# edited — not a second table, which would be invalid TOML and cost Codex the
# whole file.
assert_contains "$(cat "$FIX/account_alpha.toml.bak")" "[projects.\"$ROTCWD\"]" \
  "the earlier rotation's preflight had already written the row"
awk -v want="[projects.\"$ROTCWD\"]" '
  { if (seen && $0 ~ /^trust_level/) { print "trust_level = \"untrusted\""; seen = 0; next }
    if ($0 == want) seen = 1
    print }' "$FIX/account_alpha.toml.bak" > "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$("$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_contains "$out" "→ account_gamma" "an account where the human marked this directory untrusted is skipped, not overruled"
assert_contains "$out" "is marked untrusted in" "…and _codex_trust's own reason names the file, not just \"nothing has room\""
assert_contains "$(cat "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml")" 'trust_level = "untrusted"' \
  "…and their answer is left exactly as they wrote it"
cp "$FIX/account_alpha.toml.bak" "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

t_section "6g. the hook-trust check reads the FILE, not the checker's PATH"
# `add` resolves the hook command once, on the device: `anu account …` when
# the dispatcher is on PATH, an absolute bin path otherwise. A watchd worker
# with a minimal environment resolves it differently — and a check that
# recomputed the expected command would then call every healthy home broken
# and park every automatic rotation on a perfectly good pool.
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(ANU_ACCOUNT_HOOK_CMD="$SD/some-other-bin" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "a checker that would WRITE a different hook command still lands"
assert_contains "$out" "→ account_alpha" "…on the account whose file is perfectly good"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

# The same thing the way it actually bites: `anu` off the checker's PATH. The
# suite's own `anu` stub is moved aside for this case, so the probe PATH is
# genuinely without one.
mv "$SD/anu" "$SD/anu.hidden" 2>/dev/null || true
BAREPATH="$SD:$(dirname "$(command -v jq)"):/usr/bin:/bin"
assert_eq "" "$(PATH="$BAREPATH" command -v anu || true)" "the probe PATH really has no anu on it"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(PATH="$BAREPATH" ANU_ACCOUNT_HOOK_CMD= "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "rotate with anu off PATH still lands (this is the watchd worker's environment)"
assert_contains "$out" "→ account_alpha" "…and picks the same account"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true
mv "$SD/anu.hidden" "$SD/anu" 2>/dev/null || true

# …but the hash must still be the hash OF the installed string. A command
# edited without re-trusting it is a hook Codex will refuse to run.
cp "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml" "$FIX/k.ok"
sed "s|hook codex session-start\"|hook codex session-start --extra\"|" \
  "$FIX/k.ok" > "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$("$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_contains "$out" "→ account_gamma" "a hook command edited without re-trusting it is skipped"
assert_contains "$out" "trust hashes do not match" "…and the reason is named, not folded into \"nothing has room\""
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

# An ABSOLUTE program that is no longer there IS anu's business — that is a
# fact no PATH can change.
sed "s|command = \"$HOOKBIN|command = \"$FIX/gone-anu-account|g" "$FIX/k.ok" \
  > "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$("$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_contains "$out" "→ account_gamma" "a hook naming an absolute program that is gone is skipped"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true
cp "$FIX/k.ok" "$ANU_ACCOUNT_CODEX_DIR/account_alpha/config.toml"

t_section "6e. switch — the voluntary move"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(PANE_STATE=idle "$ACCOUNT" switch %4 --as account_gamma 2>&1)"; rc=$?
assert_ok $rc "switch --as exits 0 for an idle codex pane"
log="$(cat "$LOG")"
assert_contains "$log" "--as account_gamma --need any --cd $ROTCWD --keep-wall -- resume sid-1" "…resuming the same conversation on the named account"
assert_not_contains "$log" "You\\ were\\ resumed" "…with NO continuation: an idle pane is reopened, never nudged into work"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(PANE_STATE=idle "$ACCOUNT" switch %4 --as account_gamma --continue 2>&1)"; rc=$?
assert_contains "$(cat "$LOG")" "resume sid-1 You\\ were\\ resumed" "--continue does carry one, as the resume argument"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

: > "$LOG"
out="$(PANE_STATE=idle "$ACCOUNT" switch %4 --as nosuch 2>&1)"; rc=$?
assert_fail $rc "switch --as an account with no home on this device refuses"
assert_not_contains "$(cat "$LOG")" "C-c" "…with nothing typed"

t_section "6f. session id unknown is a state, not a guess"
: > "$LOG"
out="$(OPT_SESSION= "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_fail $rc "a codex pane with no @anu_session cannot be resumed"
assert_contains "$out" "@anu_session" "…and says so, rather than guessing the newest rollout in a shared store"

# ---------------------------------------------------------------------------
t_section "6h. rotate/switch read the PANE, never the caller's environment"
# A rotation is dispatched from a watchd worker, or typed by a human in some
# other pane — either way the caller's own ANU_ACCOUNT/ANU_PROVIDER describe
# the CALLER, not the pane being moved. The target's account comes off its own
# tmux options and from nowhere else.
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(ANU_ACCOUNT=someone-else ANU_PROVIDER=claude "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "rotate works from a shell carrying a different account"
assert_contains "$out" "account_beta → " "…and reports the move from the PANE's account (@anu_account account_beta)"
assert_not_contains "$out" "someone-else" "…never the caller's own"
assert_contains "$(cat "$LOG")" "exclude=account_beta" "…and excludes the pane's account from the pick, not the caller's"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(ANU_ACCOUNT=someone-else ANU_PROVIDER=claude PANE_STATE=idle \
  "$ACCOUNT" switch %4 --as account_gamma 2>&1)"; rc=$?
assert_ok $rc "switch likewise"
assert_contains "$out" "account_beta → account_gamma" "…moving the pane's account, not the caller's"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

t_section "6i. a relaunch that does not stay up is THAT candidate failing"
# The 2026-09-23 drill: the preflight passed account_delta (an auth.json is all it can
# see, and it never asks a server), chatgpt.com then refused its login at
# bootstrap, codex exited in ~2.6 s — and the rotation died there, after the
# walled codex was already gone, with the failed launch having cleared the
# wall stamp. Nothing retried; the session sat at a shell.
#
# These stubs model the pane per relaunch: whichever account the LAST typed
# launch line names decides what the pane runs. An account in $DEAD_ACCTS
# shows a codex for exactly one read — long enough to be seen — whose pid is
# already gone, and is a bare shell from then on; any other account runs
# $NEW_PID. The wall stamp reads $OPT_WALL.
export CX_LOG="$LOG" CX_CWD="$ROTCWD"
cat > "$SD/tmux" <<'STUB'
#!/usr/bin/env bash
printf 'tmux %s\n' "$*" >> "$CX_LOG"
last_as() { awk '/@ACCOUNT_LAUNCH@ launch/ { for (i = 1; i < NF; i++) if ($i == "--as") a = $(i + 1) } END { print a }' "$CX_LOG"; }
reads_since() { awk '/@ACCOUNT_LAUNCH@ launch/ { n = 0; next } /pane_current_command/ { n++ } END { print n + 0 }' "$CX_LOG"; }
case "$1" in
  show-options) case "$*" in
    *@anu_provider*)     echo codex ;;
    *@anu_account*)      echo "${OPT_ACCOUNT-account_beta}" ;;
    *@anu_session*)      echo sid-1 ;;
    *@anu_need*)         echo any ;;
    *@anu_launch_nonce*) echo n1 ;;
    *@anu_launch*)       echo "--sandbox read-only " ;;
    *@anu_box*)          echo 0 ;;
    *@anu_rotating*)     echo "" ;;
    *@anu_tried_codex*)  echo "${OPT_TRIED-}" ;;
    *@anu_tried*)        echo "" ;;
    *@anu_generation*)   echo 1000 ;;
    *@anu_wall_pending*) echo "${OPT_WALL-1790185383:turn-7}" ;;
    *) echo ;; esac ;;
  display-message) case "$*" in
    *socket_path*)       echo /tmp/tmux-test/default ;;
    *pane_tty*)          echo "${PANE_TTY:-/dev/ttyTEST}" ;;
    *pane_current_path*) echo "$CX_CWD" ;;
    *pane_current_command*)
      if grep -q '@ACCOUNT_LAUNCH@ launch' "$CX_LOG"; then
        case " ${DEAD_ACCTS:-} " in
          *" $(last_as) "*) if [ "$(reads_since)" -gt 1 ]; then echo bash; else echo codex; fi ;;
          *) echo codex ;;
        esac
      elif grep -q 'send-keys -t .* C-c' "$CX_LOG"; then
        if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then echo codex; else echo bash; fi
      else echo codex; fi ;;
    *) echo ;; esac ;;
  send-keys) case "$*" in *" C-c"*) [ -n "${OLD_PID:-}" ] && kill "$OLD_PID" 2>/dev/null ;; esac ;;
  capture-pane) echo "Error: account/read failed during TUI bootstrap: workspace routing discovery unauthorized (401)" ;;
esac
exit 0
STUB
cat > "$SD/ps" <<'STUB'
#!/usr/bin/env bash
a="$(awk '/@ACCOUNT_LAUNCH@ launch/ { for (i = 1; i < NF; i++) if ($i == "--as") a = $(i + 1) } END { print a }' "$CX_LOG")"
if [ -z "$a" ]; then printf '  %s codex\n' "${OLD_PID:-1}"; exit 0; fi
case " ${DEAD_ACCTS:-} " in
  *" $a "*) for e in ${DEAD_PIDS:-}; do [ "${e%%:*}" = "$a" ] && { printf '  %s codex\n' "${e#*:}"; exit 0; }; done ;;
esac
printf '  %s codex\n' "${NEW_PID:-1}"
STUB
chmod +x "$SD/tmux" "$SD/ps"
# A pid that WAS a codex a moment ago: spawned, reaped, gone.
spawn_dead() { /bin/sleep 0 >/dev/null 2>&1 </dev/null & local p=$!; wait "$p" 2>/dev/null; printf '%s' "$p"; }
export DEAD_PIDS="account_alpha:$(spawn_dead) account_gamma:$(spawn_dead)"
launch_lines() { grep -n '@ACCOUNT_LAUNCH@ launch' "$LOG"; }

: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(DEAD_ACCTS=account_alpha "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "the first candidate dies, the next one stays up: rotate still exits 0"
assert_contains "$out" "account_beta → account_gamma" "…and reports the account that actually came up"
assert_contains "$out" "account_alpha did not stay up after relaunch" "…having said which one did not, and why"
assert_contains "$out" "unauthorized (401)" "…with that relaunch's own screen kept for the log"
assert_eq "2" "$(launch_lines | wc -l | tr -d ' ')" "exactly two relaunches: the dead candidate, then the next"
assert_contains "$(launch_lines | sed -n 1p)" "--as account_alpha" "…account_alpha first (it ranks first)"
assert_contains "$(launch_lines | sed -n 2p)" "--as account_gamma" "…then account_gamma, from the shell account_alpha left behind"
assert_contains "$(cat "$LOG")" "@anu_tried_codex account_alpha:" "the dead candidate goes on the tried list"
clear_at="$(grep -n 'set-option -pu -t %4 @anu_wall_pending' "$LOG" | cut -d: -f1)"
assert_eq "1" "$(printf '%s\n' "$clear_at" | grep -c .)" "the wall stamp is cleared exactly once"
account_gamma_at="$(launch_lines | sed -n 2p | cut -d: -f1)"
assert_eq "1" "$([ -n "$clear_at" ] && [ "${clear_at:-0}" -gt "${account_gamma_at:-0}" ] && echo 1 || echo 0)" \
  "…and only after the relaunch that stayed up — never when one died"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

# A wall stamped AGAIN by the time the relaunch is confirmed — the new
# process's first turn walled, and the scan saw it — is new evidence, not the
# wall this rotation was carrying. It is not cleared.
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
cat > "$SD/tmux-wall" <<'STUB'
#!/usr/bin/env bash
if [ "$1" = show-options ] && [[ "$*" == *@anu_wall_pending* ]] && grep -q '@ACCOUNT_LAUNCH@ launch' "$CX_LOG"; then
  printf 'tmux %s\n' "$*" >> "$CX_LOG"; echo "1790185999:turn-8"; exit 0
fi
exec "$(dirname "$0")/tmux.real" "$@"
STUB
mv "$SD/tmux" "$SD/tmux.real"; mv "$SD/tmux-wall" "$SD/tmux"; chmod +x "$SD/tmux"
out="$("$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "a relaunch whose first turn walls again still counts as up"
assert_not_contains "$(cat "$LOG")" "set-option -pu -t %4 @anu_wall_pending" "…and the NEW wall's stamp is left for the next rotation"
mv "$SD/tmux.real" "$SD/tmux"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

# Every candidate dies: the pane is at a shell, the session runs nowhere.
TTYF="$FIX/pane-tty"; : > "$TTYF"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(DEAD_ACCTS="account_alpha account_gamma" PANE_TTY="$TTYF" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
parked="anu account: rotation failed — no codex account with room is logged in on this device (account_alpha, account_gamma did not stay up); resume with: @ACCOUNT_LAUNCH@ launch --provider codex --as account_beta -- resume sid-1 --sandbox read-only"
assert_eq "1" "$rc" "every candidate dead: rotate exits 1"
assert_eq "$parked" "$(printf '%s\n' "$out" | tail -n 1)" \
  "…its LAST line says so, and how to resume (the watchd reap raises that line as the notice)"
assert_contains "$(cat "$TTYF")" "$parked" "…and the same one line is left in the pane itself"
assert_eq "2" "$(launch_lines | wc -l | tr -d ' ')" "…after exactly one relaunch per candidate"
assert_not_contains "$(cat "$LOG")" "set-option -pu -t %4 @anu_wall_pending" "…and the wall stamp is never cleared: the session is still walled"
assert_contains "$(cat "$LOG")" "set-option -p -t %4 @anu_account account_beta" "…and the pane goes back on the account it walled on"
assert_contains "$(cat "$LOG")" "@anu_tried_codex account_gamma:" "…both dead candidates on the tried list"
assert_match "$(cat "$LOG")" "send-keys -t %4 C-m$" "…and one Enter, so the shell draws a fresh prompt under the line"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

# …and when the walled account's own home is the only login left, it says so.
# A device of its own for this: the three homes and nothing else.
ONLY="$(mktmp)/codex"
ANU_ACCOUNT_CODEX_DIR="$ONLY" "$ACCOUNT" add --provider codex account_alpha >/dev/null 2>&1
ANU_ACCOUNT_CODEX_DIR="$ONLY" "$ACCOUNT" add --provider codex account_gamma >/dev/null 2>&1
mkdir -p "$ONLY/account_beta"; : > "$ONLY/account_beta/auth.json"
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(ANU_ACCOUNT_CODEX_DIR="$ONLY" DEAD_ACCTS="account_alpha account_gamma" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_eq "2" "$(launch_lines | wc -l | tr -d ' ')" "a device with only the walled login left still tried both candidates"
assert_contains "$(printf '%s\n' "$out" | tail -n 1)" "account_beta, the account that walled, is the only codex login left on this device" \
  "…and names the walled account's home as the only login left"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

# A switch names (or picks) ONE destination and does not walk on — but a
# limited pane's wall survives the dead relaunch there too.
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(DEAD_ACCTS=account_gamma "$ACCOUNT" switch %4 --as account_gamma 2>&1)"; rc=$?
assert_fail $rc "switch onto a candidate that dies fails"
assert_contains "$out" "did not stay up after relaunch" "…saying so, as it always has"
assert_eq "1" "$(launch_lines | wc -l | tr -d ' ')" "…with no second relaunch"
assert_not_contains "$(cat "$LOG")" "set-option -pu -t %4 @anu_wall_pending" "…and the wall kept"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

t_section "6j. the account being left is never a destination; the tried list is a set"
# The dashboard is asked to exclude the current account and the real route
# does — but a ranking that lists it anyway (this stub ignores `exclude`, as a
# static one did in the drill: account_gamma → account_gamma) must not send the pane back onto
# the wall it is leaving.
: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(OPT_ACCOUNT=account_alpha "$ACCOUNT" rotate %4 2>&1)"; rc=$?
assert_ok $rc "a ranking that still lists the walled account rotates"
assert_contains "$out" "account_alpha → account_gamma" "…onto the next account, never back onto the one it is leaving"
assert_not_contains "$(cat "$LOG")" "--as account_alpha" "…nothing is ever typed for the walled account"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

: > "$LOG"
OLD_PID="$(spawn_fake)"; NEW_PID="$(spawn_fake)"; export OLD_PID NEW_PID
out="$(OPT_TRIED="account_beta:1000 account_gamma:2000" "$ACCOUNT" rotate %4 2>&1)"; rc=$?
tried="$(grep '@anu_tried_codex ' "$LOG" | tail -n 1 | sed 's/.*@anu_tried_codex //')"
assert_eq "1" "$(printf '%s\n' "$tried" | tr ' ' '\n' | grep -c '^account_beta:')" "an account left twice is on the tried list once"
assert_contains "$tried" "account_gamma:2000" "…the other entries kept"
assert_not_contains "$tried" "account_beta:1000" "…and its entry carries the LATER epoch"
assert_match "$tried" "account_beta:[0-9]+$" "…moved to the end, where the newest entry goes"
kill "$OLD_PID" "$NEW_PID" 2>/dev/null || true

t_section "6k. rebalance — a codex pane moves on the CODEX ranking"
# The rule does not care which provider it judges; everything it reads is the
# pane's own provider's: the openai ranking, need any whatever the pane
# carries, the codex preflight (a home logged in HERE, hooks Codex will run,
# this directory trusted) — and the next ranked pick when one fails it. The
# move is `switch --as`, which section 6e already drives.
RBSTORE="$(mktmp)"
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[
 {"id":"codex-account_epsilon","name":"account_epsilon","email":"g@x.com","resetsAt":"2026-09-20T03:33:44.000Z","remaining":90,"shared":false},
 {"id":"codex-account_gamma","name":"account_gamma","email":"t@x.com","resetsAt":"2026-09-20T04:33:44.000Z","remaining":80,"shared":false},
 {"id":"codex-account_beta","name":"account_beta","email":"d@x.com","resetsAt":null,"remaining":100,"shared":false}],
 "out":[],"generatedAt":"2026-09-16T00:00:00Z"}
JSON
stub "$SD" tmux '
printf "tmux %s\n" "$*" >> "'"$LOG"'"
case "$1" in
  show-options)
    if [ "$2" = -gqv ]; then [ -f "'"$RBSTORE"'/$3" ] && cat "'"$RBSTORE"'/$3"; echo; exit 0; fi
    echo ;;
  set-option) [ "$2" = -g ] && printf "%s" "$4" > "'"$RBSTORE"'/$3" ;;
  display-message) case "$*" in
    *"#{@anu_account}|"*) printf "account_beta|codex|fable|sid-1|0|||codex|%s\n" "'"$ROTCWD"'" ;;
    *) echo ;; esac ;;
esac
exit 0'
: > "$LOG"
out="$(PANE_STATE=idle "$ACCOUNT" rebalance %4 --dry-run 2>&1)"; rc=$?
assert_ok $rc "rebalance --dry-run exits 0 for a codex pane"
assert_contains "$(cat "$LOG")" "pick?need=any&fresh=0&provider=openai" "…asking for the OPENAI ranking, need any whatever the pane carries"
assert_contains "$out" "skipping account_epsilon — no auth.json" "the best, with no login on this device, is passed over — saying why"
assert_match "$(printf '%s\n' "$out" | awk '$1 == "%4"')" "^%4 +codex +account_beta +account_gamma +sooner-reset +would-move$" \
  "…for the next ranked pick that still beats the current one (whose null reset is +infinity)"
assert_file "$RBSTORE/@anu_rebalance_rank_codex" "the ranking is cached in @anu_rebalance_rank_codex"
assert_not_contains "$(cat "$LOG")" "anu-secrets" "…and no token is ever looked up for a codex account"
# The suite's standard ranking back, for anything after this.
cat > "$FIX/pick-codex.json" <<'JSON'
{"need":"any","picks":[{"id":"codex-account_alpha","name":"account_alpha","email":"alpha@example.invalid","resetsAt":"2026-09-20T02:59:59Z","remaining":71,"shared":false},
 {"id":"codex-account_gamma","name":"account_gamma","email":"t@x.com","resetsAt":"2026-09-21T02:59:59Z","remaining":40,"shared":false}],
 "out":[{"id":"codex-account_beta","why":"week_all 100"}],"generatedAt":"2026-09-16T00:00:00Z"}
JSON

t_done
