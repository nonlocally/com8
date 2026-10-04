#!/usr/bin/env bash
# pane is the agent-to-agent control plane, so these tests run the real executable
# against a small tmux stub instead of sourcing internals. That keeps the top-level
# no-tmux guard, reply-file path, target resolution, and command dispatch covered.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$here/../lib/harness.sh"
source "$here/../lib/helpers.bash"

t_suite "pane"

PANE="$ANU_ROOT/profiles/runtime/accounts/pane"
assert_file "$PANE" "pane executable present"

write_screen() {
  printf '%s\n' "$@" > "$SCREEN_FILE"
}

install_pane_tmux_stub() {
  local dir="$1" log="$2"
  : > "$log"
  stub "$dir" tmux '
log="'"$log"'"
printf "%s\n" "$*" >> "$log"
case "$1" in
  list-panes)
    if [[ "$*" == *"-F"* ]]; then
      printf "%%1\t${PANE_CLI:-codex}\t1:test\t0\t${PANE_ROLE:-reviewer}\t${PANE_TITLE:-Review task}\n"
    else
      printf "%%1\n"
    fi
    ;;
  capture-pane)
    cat "${SCREEN_FILE:?}"
    ;;
  send-keys)
    case "$*" in
      *" Escape"*|*" Enter"*|*" %1 "[0-9]*|*" C-c"*)
        printf "done\n› \n" > "${SCREEN_FILE:?}"
        ;;
    esac
    ;;
  show-option|show-options)
    case "$*" in
      *"@anu_replyid"*) printf "%s\n" "${REPLY_ID:-}" ;;
      *"@anu_role"*) printf "%s\n" "${PANE_ROLE:-reviewer}" ;;
      *"@swarm_panes"*) printf "%s\n" "${STUB_OPT:-}" ;;
      *"@anu_box"*) printf "%s\n" "${PANE_BOX:-}" ;;
      *) printf "\n" ;;
    esac
    ;;
  display-message)
    case "$*" in
      *pane_dead*)            printf "%s\t%s\t%s\n" "${PANE_DEAD:-0}" "${PANE_INMODE:-0}" "${PANE_CLI:-codex}" ;;
      *pane_title*)           printf "%s\n" "${PANE_TITLE:-task}" ;;
      *pane_in_mode*)         printf "%s\n" "${PANE_INMODE:-0}" ;;
      *pane_current_command*) printf "%s\n" "${PANE_CLI:-codex}" ;;
      *)                      printf "\n" ;;
    esac
    ;;
esac
exit 0
'
}

t_section "state classifier"
tmp="$(mktmp)"
sd="$(new_stubdir)"
log="$tmp/tmux.log"
SCREEN_FILE="$tmp/screen"; export SCREEN_FILE
install_pane_tmux_stub "$sd" "$log"
old_path="$PATH"; PATH="$sd:$PATH"; export TMUX=fake PANE_CLI=codex PANE_ROLE=reviewer

write_screen "Working appears in an old answer, not the status area" "" "› "
assert_eq "idle" "$("$PANE" state %1)" "ordinary answer text does not look busy"

write_screen "some answer" "" "esc to interrupt"
assert_eq "busy" "$("$PANE" state %1)" "busy state is detected from the bottom status"

write_screen "Apply patch?" "Do you want to proceed?" "1. Yes, proceed" "2. No" "Esc to cancel"
state_json="$("$PANE" state %1 --json)"
assert_contains "$state_json" '"state":"approval"' "approval state is exposed as json"
assert_contains "$state_json" '"key":"1"' "approval choices include option 1"
assert_contains "$state_json" '"key":"2"' "approval choices include option 2"


t_section "limited: the provider's wall text"
PANE_CLI=claude write_screen "" "You've reached your Fable limit." "  Continue on Opus? (uses usage credits)" "  ❯ 1. Yes" "    2. No" "esc to cancel"
assert_eq "limited" "$(PANE_CLI=claude "$PANE" state %1)" "fable wall beats the approval pattern"
assert_contains "$(PANE_CLI=claude "$PANE" state %1 --json)" '"wall":"fable"' "…and names the wall"
write_screen "" "You've hit your limit · resets 3pm" "new messages wait for your usage limit to reset" "› "
assert_eq "limited" "$(PANE_CLI=claude "$PANE" state %1)" "session wall"
assert_contains "$(PANE_CLI=claude "$PANE" state %1 --json)" '"wall":"session"' "…named"
write_screen "" "you have reached your weekly usage limit" "› "
assert_contains "$(PANE_CLI=claude "$PANE" state %1 --json)" '"wall":"weekly"' "weekly wall named"
write_screen "done" "› "
assert_eq "idle" "$(PANE_CLI=claude "$PANE" state %1)" "a clean prompt is idle"
lines=(); for i in $(seq 1 20); do lines+=("line $i"); done
write_screen "usage limit reached" "${lines[@]}" "› "
assert_eq "idle" "$(PANE_CLI=claude "$PANE" state %1)" "wall text scrolled out of the last 16 lines does not count"
write_screen "" "You've reached your Fable limit." "  Continue on Opus? (uses usage credits)" "  ❯ 1. Yes" "    2. No" "esc to cancel"
assert_ne "limited" "$(PANE_CLI=claude PANE_INMODE=1 "$PANE" state %1)" "copy-mode's stale scroll view is never read as limited"

# Wall text QUOTED in the conversation is not a wall: the TUI prints the wall at
# the start of a line (after the ⎿ glyph), never inside prose or a user echo.
write_screen "❯ wait, its wall text is \"You've hit your usage limit for …\", right?" "  Yes — Codex prints \"You've hit your usage limit for …\"; and Claude prints" "  \"You've reached your Fable limit.\" — and usage limit reached is the 5h one." "" "──────────" "❯ " "──────────" "  [Fable @gmail]"
assert_eq "idle" "$(PANE_CLI=2.1.272 "$PANE" state %1)" "wall phrases quoted inside a turn's prose or the user echo are not a wall"
# The fullscreen TUI: transcript top-anchored, composer at the bottom of a tall
# pane, dozens of blank rows between — the wall line must still be found (B7).
blanks=(); for i in $(seq 1 44); do blanks+=(""); done
write_screen "▝▜██████▀  Fable 5.1 with xhigh effort · Claude API" "" "❯ Reply with the single word ok." "  ⎿  You've reached your Fable limit. Run /usage-credits to continue or switch models with /model." "✻ Brewed for 0s · done 3:22 AM" "${blanks[@]}" "──────────" "❯ " "──────────" "  [Fable 5.1 xhigh @account_beta] | b7:master*" "  ⏵⏵ bypass permissions on"
assert_eq "limited" "$(PANE_CLI=2.1.272 "$PANE" state %1)" "fullscreen layout: a wall far above the composer is still limited"
assert_contains "$(PANE_CLI=2.1.272 "$PANE" state %1 --json)" '"wall":"fable"' "…and named fable"
write_screen "❯ do the thing" "  ⎿  You've reached your weekly usage limit" "${blanks[@]}" "──────────" "❯ " "──────────" "  [Fable 5.1 @account_beta]"
assert_contains "$(PANE_CLI=2.1.272 "$PANE" state %1 --json)" '"wall":"weekly"' "fullscreen layout: weekly wall named"
# An earlier wall that a newer turn has already moved past is not the pane's state.
write_screen "❯ first question" "  ⎿  You've reached your Fable limit. Run /usage-credits to continue or switch models with /model." "❯ /model opus" "  ⎿  Set model to Opus" "❯ second question" "ok" "" "──────────" "❯ " "──────────" "  [Opus @account_beta]"
assert_eq "idle" "$(PANE_CLI=2.1.272 "$PANE" state %1)" "a resolved wall above a newer turn is not limited"
# Text typed into the composer must not hide a wall in the last turn.
write_screen "❯ ask" "  ⎿  You've hit your limit · resets 3pm" "${blanks[@]}" "──────────" "❯ half-typed follow-up" "──────────" "  [Fable @account_beta]"
assert_eq "limited" "$(PANE_CLI=2.1.272 "$PANE" state %1)" "a composer with typed text is still the composer, the wall above it counts"

# a `box claude` pane's pane_current_command is the container runtime, not
# claude — the wall gate reads it as claude only when @anu_box=1 (stamped by
# `com8-account launch --box`), so an unrelated container pane can't
# false-positive on stray wall text.
write_screen "" "You've hit your limit · resets 3pm" "new messages wait for your usage limit to reset" "› "
assert_eq "limited" "$(PANE_CLI=container PANE_BOX=1 "$PANE" state %1)" "a boxed claude pane (container) reads the wall as limited when @anu_box=1"
assert_ne "limited" "$(PANE_CLI=container "$PANE" state %1)" "…but not without @anu_box=1"

# Same gate for the approval detector: a permission prompt inside a box would
# otherwise read `idle`, and respond/gather/watchd would treat a blocked agent
# as a finished one.
write_screen "Apply patch?" "Do you want to proceed?" "1. Yes, proceed" "2. No" "Esc to cancel"
assert_eq "approval" "$(PANE_CLI=container PANE_BOX=1 "$PANE" state %1)" "a permission prompt in a boxed pane (container) reads approval when @anu_box=1"
assert_contains "$(PANE_CLI=container PANE_BOX=1 "$PANE" state %1 --json)" '"key":"1"' "…with its choices, same as an unboxed one"
assert_ne "approval" "$(PANE_CLI=container "$PANE" state %1)" "…but not without @anu_box=1"


t_section "non-blocking status (working/idle detection)"
export PANE_TITLE PANE_DEAD                           # the stub runs as a child -> must export
# THE fix: a codex pane mid-stream shows NO "esc to interrupt" (the streamed answer
# overwrote it) but DOES carry a title spinner glyph -> must read working, not idle.
PANE_TITLE="⠋ anu"                                   # leading braille spinner glyph
write_screen "  37. 157 is prime." "  38. 163 is prime." "› "
assert_eq "working" "$("$PANE" status %1)" "codex title spinner reads working without 'esc to interrupt' (streaming)"
assert_contains "$("$PANE" status %1 --json)" '"state":"working"' "status --json reports the state"
# codex clears its title to the plain cwd at idle -> idle.
PANE_TITLE="anu"; write_screen "  done" "› "
assert_eq "idle" "$("$PANE" status %1)" "codex with a plain title (glyph cleared) reads idle"
# back-compat: an explicit bottom 'esc to interrupt' still means working.
PANE_TITLE="anu"; write_screen "some output" "esc to interrupt"
assert_eq "working" "$("$PANE" status %1)" "bottom 'esc to interrupt' still reads working"
# claude FREEZES its glyph at idle, so a present-but-static glyph + a prompt is idle.
PANE_CLI=claude PANE_TITLE="✳ Review the diff"; write_screen "✻ Worked for 12s" "❯ "
assert_eq "idle" "$("$PANE" status %1)" "claude frozen (non-animating) glyph reads idle, not working"
# booting: an agent process with no composer prompt on screen yet.
PANE_CLI=codex PANE_TITLE="anu"; write_screen "Loading model" "  please wait"
assert_eq "booting" "$("$PANE" status %1)" "agent with no composer prompt reads booting"
# a dead pane is neither working nor idle.
PANE_CLI=codex PANE_DEAD=1 PANE_TITLE="anu"; write_screen "[exited]"
assert_eq "dead" "$("$PANE" status %1)" "a dead pane reads dead"
PANE_DEAD=0
# no target -> the team table (unchanged behavior).
PANE_TITLE="anu"; write_screen "› "
assert_contains "$("$PANE" status 2>&1)" "STATE" "status with no target prints the team table"
PANE_CLI=codex; unset PANE_TITLE PANE_DEAD


t_section "wait predicates"
write_screen "header" "READY" "› "
out="$("$PANE" wait %1 2 --for READY 2>&1)"
assert_ok "$?" "wait --for succeeds when the bottom screen matches"
assert_contains "$out" "READY" "wait prints the settled reply text"
"$PANE" wait %1 --for "[" >/dev/null 2>&1
assert_fail "$?" "wait rejects invalid regexes"


t_section "focus (click target)"
ftmp="$(mktmp)"; fsd="$(new_stubdir)"; flog="$ftmp/tmux.log"; folog="$ftmp/osa.log"
stub "$fsd" tmux '
log="'"$flog"'"; printf "%s\n" "$*" >> "$log"
case "$1" in
  list-panes)   printf "%%1\tcodex\t1:w\t0\trev\tTitle\n" ;;
  list-clients) echo "/dev/ttys016" ;;
  display-message)
    case "$*" in
      *session_name*)    echo "anu-test-sess" ;;
      *window_index*)    echo "1" ;;
      *client_termname*) echo "xterm-ghostty" ;;
      *) echo "" ;;
    esac ;;
esac
exit 0'
stub "$fsd" osascript 'printf "%s\n" "$*" >> "'"$folog"'"; exit 0'
op="$PATH"; PATH="$fsd:$PATH"; export TMUX=fake
"$PANE" focus %1 --client /dev/ttys016 >/dev/null 2>&1
ftl="$(cat "$flog")"
assert_contains "$ftl" "select-window -t =anu-test-sess:1" "focus selects the pane's window"
assert_contains "$ftl" "select-pane -t %1" "focus selects the target pane"
assert_contains "$ftl" "switch-client -c /dev/ttys016 -t =anu-test-sess" "focus drives the snapshotted client"
assert_contains "$(cat "$folog")" "com.mitchellh.ghostty" "focus activates Ghostty by bundle id"
# focus clears the target pane's needs-ledger record (human arrived -> self-dismiss)
export ANU_NOTIFY_DIR="$ftmp/n"; mkdir -p "$ANU_NOTIFY_DIR"; printf 'call 100 hi\n' > "$ANU_NOTIFY_DIR/need_fake_1"
"$PANE" focus %1 --client /dev/ttys016 >/dev/null 2>&1
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/need_fake_1" ] && echo 1 || echo 0)" "focus clears the target pane's needs record"
unset ANU_NOTIFY_DIR
# gone pane -> quiet no-op (list-panes returns nothing, so %1 won't resolve)
: > "$folog"
stub "$fsd" tmux 'case "$1" in list-panes) exit 0 ;; esac; exit 0'
"$PANE" focus %1 >/dev/null 2>&1; assert_ok "$?" "focus on a missing pane exits 0"
assert_eq "" "$(cat "$folog")" "no terminal activation when the pane is gone"
PATH="$op"; unset TMUX

# ----------------------------------------------------------------- notify -----

t_section "notify (emit chokepoint)"
ntmp="$(mktmp)"; nsd="$(new_stubdir)"; ntn="$ntmp/tn.log"
export ANU_NOTIFY_DIR="$ntmp/n"
stub "$nsd" tmux '
case "$1" in
  list-panes)   printf "%%1\tcodex\t1:w\t1\trev\tT\n" ;;
  list-clients) echo "/dev/ttys016" ;;
  display-message) case "$*" in *session_name*) echo s ;; *window_name*) echo win ;; *client_termname*) echo xterm-ghostty ;; *) echo "" ;; esac ;;
  show-options) echo "" ;;
esac
exit 0'
stub "$nsd" terminal-notifier 'printf "%s\n" "$*" >> "'"$ntn"'"; exit 0'
op="$PATH"; PATH="$nsd:$PATH"; export TMUX=fake ANU_ACTIVE_PANE_OVERRIDE=%9
"$PANE" notify "need you now" --kind call --target %1 >/dev/null 2>&1
nout="$(cat "$ntn")"
assert_contains "$nout" "-group anu:%1:call" "notify groups per pane+kind"
assert_contains "$nout" "focus %1" "execute calls pane focus with the pane id"
assert_contains "$nout" "/profiles/runtime/accounts/pane" "execute uses an absolute pane path"
assert_contains "$nout" "need you now" "notify passes the message through"
assert_not_contains "$nout" "-sender" "notify never uses -sender"
# suppress-if-focused
: > "$ntn"; export ANU_ACTIVE_PANE_OVERRIDE=%1
"$PANE" notify "x" --kind info --target %1 >/dev/null 2>&1
assert_eq "" "$(cat "$ntn")" "notify suppressed when the target pane is focused"
# --force escapes suppression for a call
: > "$ntn"
"$PANE" notify "x" --kind call --target %1 --force >/dev/null 2>&1
assert_contains "$(cat "$ntn")" "anu:%1:call" "--force emits even when focused"
# cooldown: a rapid second info is dropped
: > "$ntn"; export ANU_ACTIVE_PANE_OVERRIDE=%9 ANU_NOTIFY_DIR="$ntmp/n2"
"$PANE" notify "a" --kind info --target %1 >/dev/null 2>&1
"$PANE" notify "b" --kind info --target %1 >/dev/null 2>&1
assert_eq "1" "$(grep -c "anu:%1:info" "$ntn")" "info cooldown suppresses a rapid second notify"
# bad kind is rejected
"$PANE" notify "x" --kind bogus --target %1 >/dev/null 2>&1; assert_fail "$?" "notify rejects an unknown --kind"
# quoting: message with quotes/$/backticks survives intact as one -message arg
: > "$ntn"; export ANU_NOTIFY_DIR="$ntmp/n3"
"$PANE" notify 'weird "q" $x `cmd` end' --kind info --target %1 >/dev/null 2>&1
assert_contains "$(cat "$ntn")" 'weird "q" $x `cmd` end' 'message survives quotes/$/backticks'
# title maps a Claude version-string cli (e.g. 2.1.195) to "claude", not the raw number
: > "$ntn"; export ANU_NOTIFY_DIR="$ntmp/n5"
stub "$nsd" tmux 'case "$1" in
  list-panes) printf "%%1\t2.1.195\t1:feat\t1\t\tT\n" ;;
  list-clients) echo "/dev/ttys016" ;;
  display-message) case "$*" in *session_name*) echo s ;; *window_name*) echo feat ;; *client_termname*) echo xterm-ghostty ;; *) echo "" ;; esac ;;
  show-options) echo "" ;;
esac
exit 0'
"$PANE" notify "hi" --kind input --target %1 >/dev/null 2>&1
assert_contains "$(cat "$ntn")" "claude" "title maps a Claude version-string cli to 'claude'"
assert_not_contains "$(cat "$ntn")" "2.1.195" "title does not show the raw version string"
# osascript fallback when terminal-notifier is absent (hermetic PATH: no homebrew)
rm -f "$nsd/terminal-notifier"
nol="$ntmp/osa.log"; stub "$nsd" osascript 'printf "ARGS:%s\n" "$*" >> "'"$nol"'"; cat >/dev/null 2>&1; exit 0'
export ANU_NOTIFY_DIR="$ntmp/n4"
PATH="$nsd:/usr/bin:/bin" "$PANE" notify "fallback" --kind done --target %1 >/dev/null 2>&1
assert_file "$nol" "notify falls back to osascript without terminal-notifier"
PATH="$op"; unset TMUX ANU_ACTIVE_PANE_OVERRIDE ANU_NOTIFY_DIR

# ------------------------------------------------------------------- call -----

t_section "call (agent->human summon)"
ctmp="$(mktmp)"; csd="$(new_stubdir)"; ctn="$ctmp/tn.log"; ctlog="$ctmp/tmux.log"
export ANU_NOTIFY_DIR="$ctmp/c"
stub "$csd" tmux '
printf "%s\n" "$*" >> "'"$ctlog"'"
case "$1" in
  list-panes)   printf "%%1\tcodex\t1:w\t1\trev\tT\n" ;;
  list-clients) echo "/dev/ttys016" ;;
  display-message) case "$*" in *session_name*) echo s ;; *window_name*) echo win ;; *client_termname*) echo xterm-ghostty ;; *) echo "" ;; esac ;;
  show-options) echo "" ;;
esac
exit 0'
stub "$csd" terminal-notifier 'printf "%s\n" "$*" >> "'"$ctn"'"; exit 0'
op="$PATH"; PATH="$csd:$PATH"; export TMUX=fake TMUX_PANE=%1 ANU_ACTIVE_PANE_OVERRIDE=%9
"$PANE" call "need a decision on X" >/dev/null 2>&1
assert_contains "$(cat "$ctn")" "anu:%1:call" "call emits a call notification for its own pane"
assert_contains "$(cat "$ctn")" "need a decision on X" "call passes the reason as the message"
assert_contains "$(cat "$ctlog")" "set-option -p -t %1 @anu_last_call_at" "call stamps @anu_last_call_at on the pane"
# call also writes a durable needs-ledger record (with the reason), surfaced by the feed
assert_contains "$(cat "$ANU_NOTIFY_DIR/need_fake_1")" "call" "call writes a needs-ledger record (kind=call)"
assert_contains "$(cat "$ANU_NOTIFY_DIR/need_fake_1")" "need a decision on X" "call persists the reason in the needs ledger"
nfj="$("$PANE" needs --json 2>/dev/null)"
assert_contains "$nfj" '"pane":"%1"' "pane needs --json lists the calling pane"
assert_contains "$nfj" '"kind":"call"' "pane needs --json reports kind=call"
assert_contains "$nfj" '"reason":"need a decision on X"' "pane needs --json carries the call reason"
PATH="$op"; unset TMUX TMUX_PANE ANU_ACTIVE_PANE_OVERRIDE ANU_NOTIFY_DIR

# ------------------------------------------------------------------ needs -----

t_section "needs ledger (who-needs-you feed)"
qtmp="$(mktmp)"; qsd="$(new_stubdir)"
export ANU_NOTIFY_DIR="$qtmp/q"; mkdir -p "$ANU_NOTIFY_DIR"
stub "$qsd" tmux '
case "$1" in
  list-clients) echo "/dev/ttys016" ;;
  display-message) case "$*" in
      *%999*) printf "\n" ;;
      *session_name*) echo s ;;
      *window_name*) echo 1:w ;;
      *) printf "\n" ;;
    esac ;;
esac
exit 0'
op="$PATH"; PATH="$qsd:$PATH"; export TMUX=fake ANU_ACTIVE_PANE_OVERRIDE=%9
# empty ledger -> []
assert_eq "[]" "$("$PANE" needs --json)" "empty needs ledger emits []"
# precedence: call outranks done regardless of since
printf 'done 100 \n' > "$ANU_NOTIFY_DIR/need_fake_1"
printf 'call 200 pick a branch\n' > "$ANU_NOTIFY_DIR/need_fake_2"
nj="$("$PANE" needs --json)"
assert_contains "$nj" '"pane":"%1"' "feed includes the done pane"
assert_contains "$nj" '"pane":"%2"' "feed includes the called pane"
firstkind="$(printf '%s' "$nj" | grep -o '"kind":"[a-z]*"' | head -1)"
assert_eq '"kind":"call"' "$firstkind" "highest-precedence need (call) sorts first"
assert_contains "$nj" '"window":"1:w"' "feed carries the pane's window"
assert_contains "$nj" '"since":200' "feed carries the signal timestamp"
# a focused pane is skipped (human already there)
export ANU_ACTIVE_PANE_OVERRIDE=%2
nj2="$("$PANE" needs --json)"
assert_not_contains "$nj2" '"pane":"%2"' "feed skips a pane the human is currently on"
assert_contains "$nj2" '"pane":"%1"' "feed still lists other panes when one is focused"
export ANU_ACTIVE_PANE_OVERRIDE=%9
# a vanished pane is dropped and its stale record cleaned up
printf 'done 300 \n' > "$ANU_NOTIFY_DIR/need_fake_999"
nj3="$("$PANE" needs --json)"
assert_not_contains "$nj3" '"pane":"%999"' "feed drops a vanished pane"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/need_fake_999" ] && echo 1 || echo 0)" "feed cleans up the vanished pane's stale record"
# the reason is JSON-escaped
printf 'call 400 say "hi" now\n' > "$ANU_NOTIFY_DIR/need_fake_3"
assert_contains "$("$PANE" needs --json)" 'say \"hi\" now' "feed JSON-escapes quotes in the reason"
PATH="$op"; unset TMUX ANU_NOTIFY_DIR ANU_ACTIVE_PANE_OVERRIDE

# ---------------------------------------------------- watchd state machine ----

t_section "watchd (done/input detection)"
wtmp="$(mktmp)"; wsd="$(new_stubdir)"; wtn="$wtmp/tn.log"; SCREEN_FILE="$wtmp/scr"; export SCREEN_FILE
export ANU_NOTIFY_DIR="$wtmp/w" ANU_ACTIVE_PANE_OVERRIDE=%9
stub "$wsd" tmux '
case "$1" in
  list-panes)   printf "%%1\tcodex\t1:w\t0\t\tT\n" ;;
  capture-pane) cat "'"$SCREEN_FILE"'" ;;
  list-clients) echo "/dev/ttys016" ;;
  display-message) case "$*" in *pane_dead*) printf "%s\t%s\t%s\n" "${PANE_DEAD:-0}" "${PANE_INMODE:-0}" "${PANE_CLI:-codex}" ;; *pane_current_command*) printf "%s\n" "${PANE_CLI:-codex}" ;; *session_name*) echo s ;; *window_name*) echo win ;; *client_termname*) echo xterm-ghostty ;; *) echo "" ;; esac ;;
  show-options) case "$*" in *anu_notify_done*) echo "${WD_OPTOUT:-}" ;; *anu_last_call_at*) echo "${WD_LASTCALL:-}" ;; *) echo "" ;; esac ;;
esac
exit 0'
stub "$wsd" terminal-notifier 'printf "%s\n" "$*" >> "'"$wtn"'"; exit 0'
op="$PATH"; PATH="$wsd:$PATH"; export TMUX=fake PANE_CLI=codex
: > "$wtn"
# busy -> idle -> idle (debounced) => exactly one done
write_screen "working" "esc to interrupt"; ANU_NOW_OVERRIDE=100 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "" "$(cat "$wtn")" "no done on the initial busy observation"
write_screen "the answer" "› "; ANU_NOW_OVERRIDE=104 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "" "$(cat "$wtn")" "no done on the first idle tick (debounce)"
write_screen "the answer" "› "; ANU_NOW_OVERRIDE=106 "$PANE" watchd --tick >/dev/null 2>&1
assert_contains "$(cat "$wtn")" "anu:%1:done" "busy->idle (debounced) fires one done"
assert_contains "$(cat "$ANU_NOTIFY_DIR/need_fake_1" 2>/dev/null)" "done" "watchd records a done need on busy->idle"
# a new busy epoch clears the stale need (the agent resumed -> no longer waiting on you)
write_screen "working again" "esc to interrupt"; ANU_NOW_OVERRIDE=110 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/need_fake_1" ] && echo 1 || echo 0)" "a new busy epoch clears the stale need"
# a pane that was never busy never fires done
: > "$wtn"; export ANU_NOTIFY_DIR="$wtmp/w2"
write_screen "just sitting" "› "
ANU_NOW_OVERRIDE=200 "$PANE" watchd --tick >/dev/null 2>&1
ANU_NOW_OVERRIDE=204 "$PANE" watchd --tick >/dev/null 2>&1
ANU_NOW_OVERRIDE=206 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "" "$(cat "$wtn")" "a never-busy pane never fires done"
# approval -> input (once), never done
: > "$wtn"; export ANU_NOTIFY_DIR="$wtmp/w3"
write_screen "Apply patch?" "Do you want to proceed?" "1. Yes, proceed" "2. No" "Esc to cancel"
ANU_NOW_OVERRIDE=300 "$PANE" watchd --tick >/dev/null 2>&1
assert_contains "$(cat "$wtn")" "anu:%1:input" "entering approval fires input"
assert_contains "$(cat "$ANU_NOTIFY_DIR/need_fake_1" 2>/dev/null)" "input" "watchd records an input need on entering approval"
assert_not_contains "$(cat "$wtn")" "anu:%1:done" "approval never fires done"
ni="$(grep -c "anu:%1:input" "$wtn")"
ANU_NOW_OVERRIDE=302 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "$ni" "$(grep -c "anu:%1:input" "$wtn")" "input not re-fired while still in approval"
# opt-out: @anu_notify_done=off suppresses done
: > "$wtn"; export ANU_NOTIFY_DIR="$wtmp/w4" WD_OPTOUT=off
write_screen "working" "esc to interrupt"; ANU_NOW_OVERRIDE=400 "$PANE" watchd --tick >/dev/null 2>&1
write_screen "done now" "› "; ANU_NOW_OVERRIDE=404 "$PANE" watchd --tick >/dev/null 2>&1
ANU_NOW_OVERRIDE=406 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "" "$(cat "$wtn")" "@anu_notify_done=off opts the pane out of done"
unset WD_OPTOUT
# a call during the busy epoch suppresses the subsequent done
: > "$wtn"; export ANU_NOTIFY_DIR="$wtmp/w5" WD_LASTCALL=401
write_screen "working" "esc to interrupt"; ANU_NOW_OVERRIDE=400 "$PANE" watchd --tick >/dev/null 2>&1
write_screen "done now" "› "; ANU_NOW_OVERRIDE=404 "$PANE" watchd --tick >/dev/null 2>&1
ANU_NOW_OVERRIDE=406 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "" "$(cat "$wtn")" "a call during the busy epoch suppresses done"
unset WD_LASTCALL
PATH="$op"; unset TMUX PANE_CLI SCREEN_FILE ANU_NOTIFY_DIR ANU_ACTIVE_PANE_OVERRIDE ANU_NOW_OVERRIDE

# ------------------------------------------------- watchd: the limited arm ----
# The unattended rotation arm: a pane sitting at a provider usage wall is
# rotated in place by a detached `com8-account rotate <pane>` worker. Everything
# here runs the REAL pane executable against a tmux stub plus a fake
# anu-account (ANU_ACCOUNT_BIN) that records its argv + environment and exits
# with $ANU_ROTATE_RC — so the whole state machine (gate, confirmation,
# in-flight guard, rc handling, backoff, gc) is exercised without ever touching
# a real account or a real tmux server.

t_section "watchd (limited arm)"
rtmp="$(mktmp)"; rsd="$(new_stubdir)"; rtn="$rtmp/tn.log"
RSCREEN="$rtmp/scr"; SCREEN_FILE="$RSCREEN"; export SCREEN_FILE
RACCT=""; RENV=""; ROPT="$rtmp/tmux.log"
export ANU_ACTIVE_PANE_OVERRIDE=%9
stub "$rsd" tmux '
printf "%s\n" "$*" >> "'"$ROPT"'"
case "$1" in
  list-panes)
    # RP_VANISH simulates a pane that disappears MID-TICK: %1 is enumerated by
    # the tick'"'"'s first list-panes call (so watchd picks it up) and is gone from
    # every call after it — which is what `_resolve` consults when cmd_notify
    # tries to reach it. %2 is present throughout, so the tick has a pane left
    # to process after the one that vanished.
    if [ -n "${RP_VANISH:-}" ]; then
      case "$*" in
        *-F*)   # only the enumerating calls are counted
          n=0; [ -r "${RP_COUNT:-}" ] && n="$(cat "$RP_COUNT")"; n="${n:-0}"
          printf "%s" "$((n+1))" > "$RP_COUNT"
          [ "$n" = 0 ] && printf "%%1\t${RP_CLI:-claude}\t1:w\t0\t\tT\t${RP_BOX:-}\n" ;;
      esac
      printf "%%2\t${RP_CLI:-claude}\t1:w\t0\t\tT\t${RP_BOX:-}\n"
    else
      printf "%%1\t${RP_CLI:-claude}\t1:w\t0\t\tT\t${RP_BOX:-}\n"
    fi ;;
  capture-pane) cat "'"$RSCREEN"'" ;;
  list-clients) echo "/dev/ttys016" ;;
  display-message)
    case "$*" in
      *pane_dead*)            printf "%s\t%s\t%s\n" "0" "0" "${RP_CLI:-claude}" ;;
      *pane_current_command*) printf "%s\n" "${RP_CLI:-claude}" ;;
      *session_name*)         echo s ;;
      *window_name*)          echo win ;;
      *client_termname*)      echo xterm-ghostty ;;
      *)                      echo "" ;;
    esac ;;
  show-options)
    case "$*" in
      *@anu_autorotate*) printf "%s\n" "${RP_GATE:-}" ;;
      *@anu_box*)        printf "%s\n" "${RP_BOX:-}" ;;
      *@anu_notify_done*|*@anu_last_call_at*) printf "\n" ;;
      # the whole per-pane option table, as `show-options -pq -t %1` prints it
      *-pq*)
        [ -n "${RP_SESSION-unset}" ] && [ -n "${RP_SESSION:-}" ] && printf "@anu_session %s\n" "$RP_SESSION"
        [ -n "${RP_ACCOUNT:-}" ]  && printf "@anu_account %s\n" "$RP_ACCOUNT"
        [ -n "${RP_ROTATING:-}" ] && printf "@anu_rotating %s\n" "$RP_ROTATING"
        [ -n "${RP_WALLEV:-}" ]   && printf "@anu_wall_event %s\n" "$RP_WALLEV"
        [ -n "${RP_WALLSEEN:-}" ] && printf "@anu_wall_seen %s\n" "$RP_WALLSEEN"
        [ -n "${RP_BOX:-}" ]      && printf "@anu_box %s\n" "$RP_BOX"
        printf "@anu_role \n" ;;
      *) printf "\n" ;;
    esac ;;
esac
exit 0'
stub "$rsd" terminal-notifier 'printf "%s\n" "$*" >> "'"$rtn"'"; exit 0'
# The fake `anu account`: records argv and the socket it was handed, then exits
# with the rc the case under test wants (optionally after a sleep, so a test can
# observe a worker that is still in flight on the next tick).
stub "$rsd" fake-account '
printf "%s\n" "$*" >> "$ANU_NOTIFY_DIR/acct.log"
printf "TMUX=%s\n" "${TMUX-<unset>}" >> "$ANU_NOTIFY_DIR/acct.env"
[ -n "${ANU_ROTATE_SLEEP:-}" ] && sleep "$ANU_ROTATE_SLEEP"
[ "${ANU_ROTATE_RC:-0}" = 0 ] && printf "%%1: account_beta → account_alpha (session wall, session abcd, ready via hook)\n"
exit "${ANU_ROTATE_RC:-0}"'
export ANU_ACCOUNT_BIN="$rsd/fake-account"
rop="$PATH"; PATH="$rsd:$PATH"; export TMUX=fake RP_CLI=claude
RP_COUNT="$rtmp/lpcount"; export RP_COUNT; : > "$RP_COUNT"
rwall() { printf '%s\n' "Claude usage limit reached" "You have reached your weekly usage limit" > "$RSCREEN"; }
ridle() { printf '%s\n' "all done" "› " > "$RSCREEN"; }
# The worker is detached on purpose, so poll for its record rather than racing it.
# rwaitf takes the file to wait FOR: the stub writes acct.log first, acct.env
# after it, and the .rc file only once the rotation has actually finished — an
# assertion on the wrong one passes vacuously.
rwaitf() { local i=0; while [ "$i" -lt "${2:-60}" ]; do [ -s "$1" ] && return 0; sleep 0.1; i=$((i+1)); done; return 1; }
rwait()  { rwaitf "$RACCT"; }
# A fresh case: its own notify dir (so its rotation state AND the fake account's
# logs are isolated from every other case, including a worker left sleeping).
rdir() { export ANU_NOTIFY_DIR="$rtmp/$1"; mkdir -p "$ANU_NOTIFY_DIR"
         RACCT="$ANU_NOTIFY_DIR/acct.log"; RENV="$ANU_NOTIFY_DIR/acct.env"
         : > "$RACCT"; : > "$RENV"; : > "$rtn"; : > "$ROPT"; }
# Every case runs with a StopFailure event 120 s old unless it says otherwise
# (RP_WALLEV=<epoch>, or RP_WALLEV= for "no event"): inside the 10-minute window
# a host pane needs, outside the 60 s one-tick shortcut, so the two-tick
# confirmation is what the ordinary cases exercise.
rtick() { RP_WALLEV="${RP_WALLEV-$(( $1 - 120 ))}" ANU_NOW_OVERRIDE="$1" "$PANE" watchd --tick ${2:+--socket "$2"} >/dev/null 2>&1; }

# --- the gate: nothing happens until @anu_autorotate is 1 --------------------
export RP_SESSION=abcd RP_ACCOUNT=account_beta
rdir g1; rwall
RP_GATE= rtick 100; RP_GATE= rtick 102; RP_GATE= rtick 104
assert_eq "" "$(cat "$RACCT")" "gate off (@anu_autorotate unset): a walled pane is never rotated"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/r_default_1" ] && echo 1 || echo 0)" "…and no rotation state file is even created"

# --- gate on, but the pane is not one anu launched ---------------------------
rdir g2; rwall
RP_GATE=1 RP_SESSION= rtick 200; RP_GATE=1 RP_SESSION= rtick 202
assert_eq "" "$(cat "$RACCT")" "no @anu_session: an unmanaged pane is never rotated"
rdir g3
RP_GATE=1 RP_ACCOUNT= rtick 210; RP_GATE=1 RP_ACCOUNT= rtick 212
assert_eq "" "$(cat "$RACCT")" "a session id but no @anu_account (unmanaged) is never rotated either"

# --- confirmation: two consecutive limited ticks -----------------------------
rdir c1; rwall
RP_GATE=1 rtick 300
assert_eq "" "$(cat "$RACCT")" "one limited tick is not enough — a single misread screen never costs a session"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "confirm=1" "…but the confirmation is counted"
RP_GATE=1 rtick 302; rwait
assert_contains "$(cat "$RACCT")" "rotate %1" "limited on two consecutive ticks dispatches exactly one rotate"
assert_eq "1" "$(grep -c "rotate %1" "$RACCT")" "…exactly once"
assert_contains "$(cat "$ROPT")" "set-option -p -t %1 @anu_wall_seen 302" "…and stamps @anu_wall_seen at dispatch"

# --- the in-flight guard: one rotation per pane ------------------------------
rdir c2; rwall
RP_GATE=1 ANU_ROTATE_SLEEP=2 rtick 400
RP_GATE=1 ANU_ROTATE_SLEEP=2 rtick 402; rwait
RP_GATE=1 ANU_ROTATE_SLEEP=2 rtick 404
RP_GATE=1 ANU_ROTATE_SLEEP=2 rtick 406
assert_eq "1" "$(grep -c "rotate %1" "$RACCT")" "a worker still running is never joined by a second dispatch"

# --- the StopFailure corroboration: rotate on the FIRST tick -----------------
rdir c3; rwall
RP_GATE=1 RP_WALLEV=430 rtick 500
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "confirm=1" "the 70-second-old event still needs a second confirming tick"
assert_eq "" "$(cat "$RACCT")" "a stale @anu_wall_event (>60s) does not shortcut the confirmation"
rdir c4
RP_GATE=1 RP_WALLEV=490 rtick 500; rwait
assert_contains "$(cat "$RACCT")" "rotate %1" "a fresh @anu_wall_event (the StopFailure hook) rotates on the first tick"
# …but ONE hook event corroborates ONE dispatch. @anu_wall_seen is stamped at
# dispatch; an event no newer than it has already been spent, so the next wall
# inside the same 60s window goes back to needing two confirming ticks —
# otherwise a single screen misread in that window is enough to relaunch.
rdir c4b; rwall
RP_GATE=1 RP_WALLEV=490 RP_WALLSEEN=495 rtick 500
assert_eq "" "$(cat "$RACCT")" "a wall event already acted on (not newer than @anu_wall_seen) does not shortcut the confirmation"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "confirm=1" "…the tick is only counted"
RP_GATE=1 RP_WALLEV=490 RP_WALLSEEN=495 rtick 502
assert_eq "" "$(cat "$RACCT")" "…and, on a host pane, a spent event never rotates at all — the next wall needs its own"

# --- a host pane never rotates on screen text alone ---------------------------
rdir h1; rwall
RP_GATE=1 RP_WALLEV= rtick 700; RP_GATE=1 RP_WALLEV= rtick 702; RP_GATE=1 RP_WALLEV= rtick 704
assert_eq "" "$(cat "$RACCT")" "no @anu_wall_event: a host pane reading limited is never rotated"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "no fresh @anu_wall_event" "…and the log says why"
assert_eq "1" "$(grep -c "no fresh @anu_wall_event" "$ANU_NOTIFY_DIR/r_default_1.log")" "…once per wall, not every tick"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "nohook=1" "…tracked by its own flag"
rdir h2; rwall
RP_GATE=1 RP_WALLEV=90 rtick 700; RP_GATE=1 RP_WALLEV=90 rtick 702
assert_eq "" "$(cat "$RACCT")" "an event older than 10 min does not count as a report"
rdir h3; rwall
RP_GATE=1 RP_CLI=container RP_BOX=1 RP_WALLEV= rtick 800; RP_GATE=1 RP_CLI=container RP_BOX=1 RP_WALLEV= rtick 802; rwait
assert_contains "$(cat "$RACCT")" "rotate %1" "a BOXED pane (no hook can run there) still rotates on two screen reads"

# --- @anu_rotating: something already moved this pane ------------------------
rdir c5; rwall
RP_GATE=1 RP_ROTATING=590 rtick 600; RP_GATE=1 RP_ROTATING=590 rtick 602
assert_eq "" "$(cat "$RACCT")" "a pane rotated in the last 120s is left alone"
rdir c6
RP_GATE=1 RP_ROTATING=400 rtick 600; RP_GATE=1 RP_ROTATING=400 rtick 602; rwait
assert_contains "$(cat "$RACCT")" "rotate %1" "…and dispatched again once that cooldown has passed"

# --- rc 0: the pane LEAVES the wall, which is exactly why the reap cannot live
# --- in the limited arm — a success would otherwise never be delivered -------
rdir r0; rwall
RP_GATE=1 rtick 700; RP_GATE=1 rtick 702; rwait
ridle                                          # relaunched, resumed, back at a prompt
RP_GATE=1 rtick 706
assert_contains "$(cat "$rtn")" "rotated to account_alpha" "a pane that left the wall is still reaped — rc 0 notifies, naming the account"
assert_eq "1" "$(grep -c "anu:%1:info" "$rtn")" "…exactly once"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "rc=0" "…and records the exit code"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "worker=0" "…and clears the in-flight worker"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/r_default_1.rc" ] && echo 1 || echo 0)" "…and consumes the worker's rc file"
# and the pane's NEXT wall must not replay the old result
: > "$rtn"; rwall
RP_GATE=1 rtick 1100
assert_eq "" "$(cat "$rtn")" "the pane's next wall emits no stale 'rotated to' notification"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "confirm=1" "…and counts that wall from zero"

# --- rc 2: nothing has room -> one notice, then a 600s backoff ---------------
rdir r2; rwall
export ANU_ROTATE_RC=2
RP_GATE=1 rtick 800; RP_GATE=1 rtick 802; rwait
RP_GATE=1 rtick 806            # reap the worker: rc 2
assert_contains "$(cat "$rtn")" "waiting for capacity" "rc 2 (nothing has room) notifies once"
assert_eq "1" "$(grep -c "waiting for capacity" "$rtn")" "…exactly once per wall"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "backoff_until=1406" "…and backs off for 600s"
: > "$RACCT"
RP_GATE=1 rtick 900; RP_GATE=1 rtick 1000; RP_GATE=1 rtick 1400
assert_eq "" "$(cat "$RACCT")" "…dispatching nothing while backed off"
assert_eq "1" "$(grep -c "waiting for capacity" "$rtn")" "…and never re-notifying while backed off"
RP_GATE=1 rtick 1410; RP_GATE=1 rtick 1412; rwait
assert_contains "$(cat "$RACCT")" "rotate %1" "…then trying again once the backoff expires"
unset ANU_ROTATE_RC

# --- any other rc: the failure is logged, backed off 120s, notified once -----
rdir r1; rwall
export ANU_ROTATE_RC=1
RP_GATE=1 rtick 1500; RP_GATE=1 rtick 1502; rwait
RP_GATE=1 rtick 1506
assert_contains "$(cat "$rtn")" "rotation failed" "a failing rotate notifies once, naming its log"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "backoff_until=1626" "…and backs off 120s, not 600"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "rotate ended rc=1" "…recording the exit code in the worker log"
unset ANU_ROTATE_RC

# --- the socket: the worker must target the daemon's own tmux server ---------
# anu-account (and the `pane state` it shells out to) use a BARE tmux, which
# finds its server through $TMUX and otherwise talks to the DEFAULT socket —
# while watchd runs per socket with $TMUX unset.
rdir sk1; rwall
RP_GATE=1 rtick 1700 /tmp/x; RP_GATE=1 rtick 1702 /tmp/x; rwaitf "$RENV"
assert_eq "TMUX=/tmp/x,0,0" "$(cat "$RENV")" "--socket <s>: the rotate worker is pointed at that server via \$TMUX"
rdir sk2
RP_GATE=1 rtick 1800; RP_GATE=1 rtick 1802; rwaitf "$RENV"
assert_eq "TMUX=fake" "$(cat "$RENV")" "no --socket: \$TMUX is left exactly as inherited (this suite exports TMUX=fake)"

# --- the worker's own watchdog: a rotation that outruns the cap is killed -----
# ANU_ROTATE_WORKER_CAP exists so this is a 1-second test rather than a 5-minute
# one; the stub sleeps well past it, so the watchdog TERMs its process group and
# the worker reports the signal as an ordinary failure.
rdir wd; rwall
RP_GATE=1 ANU_ROTATE_WORKER_CAP=1 ANU_ROTATE_SLEEP=8 rtick 3000
RP_GATE=1 ANU_ROTATE_WORKER_CAP=1 ANU_ROTATE_SLEEP=8 rtick 3002
rwaitf "$ANU_NOTIFY_DIR/r_default_1.rc" 80
assert_ne "rc=0" "$(cat "$ANU_NOTIFY_DIR/r_default_1.rc" 2>/dev/null)" "a worker past its cap is killed — it reports a signal, not a clean exit, well before the stub's own sleep ends"
RP_GATE=1 ANU_ROTATE_WORKER_CAP=1 rtick 3004
assert_contains "$(cat "$rtn")" "rotation failed" "…and the killed rotation is reported as a failure"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "backoff_until=3124" "…with the 120s failure backoff"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "worker=0" "…and the in-flight worker cleared"

# --- the reap's own bound: a worker still alive long past the cap is abandoned
# --- (kill -0 alone would let an orphan — or a RECYCLED pid — own a pane for
# --- ever). Time is pinned, so this costs nothing in wall clock. -------------
rdir hg; rwall
RP_GATE=1 ANU_ROTATE_SLEEP=8 rtick 4000
RP_GATE=1 ANU_ROTATE_SLEEP=8 rtick 4002; rwait
RP_GATE=1 rtick 4100                          # 98s in: still inside cap+60, left alone
assert_eq "" "$(cat "$rtn")" "a worker inside the cap is left alone"
assert_ne "0" "$(sed -n 's/^worker=//p' "$ANU_NOTIFY_DIR/r_default_1")" "…still recorded as in flight"
hg_worker="$(sed -n 's/^worker=//p' "$ANU_NOTIFY_DIR/r_default_1")"
RP_GATE=1 rtick 4400                          # 398s in: past 300+60, abandoned
assert_contains "$(cat "$rtn")" "rotation failed" "a worker still alive past cap+60 is abandoned and reported"
# Abandoned, NOT signalled: past the bound the recorded pid is exactly what we
# have stopped trusting (an orphan, or a recycled pid now owned by something
# unrelated), so killing it would be shooting a stranger.
kill -0 "$hg_worker" 2>/dev/null; assert_ok $? "…and the abandoned pid is left alone, never signalled"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "not signalled" "…and the log says so"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "worker=0" "…the in-flight guard is released"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "rc=hung" "…recorded as hung, not as a real exit code"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "backoff_until=4520" "…with the 120s failure backoff"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "abandoned" "…and the log says why"

# --- the boxed cap: a boxed rotation gets a bigger budget than a host one ----
# `com8-account rotate` gives a boxed relaunch 60s hook + 90s running + 180s
# idle + ~15s to leave = 345s of its own, so the host's 300s cap would shoot a
# boxed rotation that was still on track and report it as a failure.
rdir bcap; rwall
RP_GATE=1 RP_CLI=container RP_BOX=1 ANU_ROTATE_SLEEP=8 rtick 7000
RP_GATE=1 RP_CLI=container RP_BOX=1 ANU_ROTATE_SLEEP=8 rtick 7002; rwait
RP_GATE=1 RP_CLI=container RP_BOX=1 rtick 7400        # 400s in: past 300+60, inside 420+60
assert_eq "" "$(cat "$rtn")" "a boxed worker 400s in is still inside its own cap — the host's 300s would already have abandoned it"
assert_ne "0" "$(sed -n 's/^worker=//p' "$ANU_NOTIFY_DIR/r_default_1")" "…still recorded as in flight"
RP_GATE=1 RP_CLI=container RP_BOX=1 rtick 7500        # 500s in: past 420+60
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "cap 420s" "…and past 420+60 it is abandoned, naming the boxed cap"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "abandoned" "…as abandoned"
rdir bcapo; rwall
RP_GATE=1 RP_CLI=container RP_BOX=1 ANU_ROTATE_WORKER_CAP=5 ANU_ROTATE_SLEEP=8 rtick 7600
RP_GATE=1 RP_CLI=container RP_BOX=1 ANU_ROTATE_WORKER_CAP=5 ANU_ROTATE_SLEEP=8 rtick 7602; rwait
RP_GATE=1 RP_CLI=container RP_BOX=1 ANU_ROTATE_WORKER_CAP=5 rtick 7700
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "cap 5s" "ANU_ROTATE_WORKER_CAP overrides the boxed cap too, not just the host one"

# --- the attempt cap: three failures per wall, then stop ---------------------
# A dashboard that keeps refusing, or a relaunch that keeps dying, would
# otherwise be retried every 120s for as long as the pane stays walled —
# each try costing the human's session another relaunch.
rdir at; rwall
export ANU_ROTATE_RC=1
# one cycle = two confirming ticks, the worker finishing, then the reap tick.
rcycle() { RP_GATE=1 rtick "$1"; RP_GATE=1 rtick "$2"
           rwaitf "$ANU_NOTIFY_DIR/r_default_1.rc" 80; RP_GATE=1 rtick "$3"; }
rcycle 5000 5002 5006          # attempt 1 -> fails, backoff to 5126
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "attempts=1" "a dispatched rotation counts as an attempt"
rcycle 5130 5132 5136          # attempt 2 -> fails, backoff to 5256
rcycle 5260 5262 5266          # attempt 3 -> fails, gives up
assert_eq "3" "$(grep -c "rotate %1" "$RACCT")" "three failing rotations is the cap for one wall"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "attempts=3" "…recorded in the state file"
assert_contains "$(cat "$rtn")" "gave up after 3 attempts" "…and the wall gets its own 'gave up' notification"
assert_eq "1" "$(grep -c "gave up" "$rtn")" "…exactly once"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "gave up on this wall" "…with the reason in the worker log"
RP_GATE=1 rtick 5390; RP_GATE=1 rtick 5392; RP_GATE=1 rtick 5394
assert_eq "3" "$(grep -c "rotate %1" "$RACCT")" "…and nothing more is dispatched for that wall, backoff or no backoff"
assert_eq "1" "$(grep -c "gave up" "$rtn")" "…and it is not re-notified every tick"
# leaving `limited` is the way back
ridle; RP_GATE=1 rtick 5400
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "attempts=0" "leaving the wall clears the attempt count"
rwall; RP_GATE=1 rtick 5500; RP_GATE=1 rtick 5502; rwaitf "$ANU_NOTIFY_DIR/r_default_1.rc" 80
assert_eq "4" "$(grep -c "rotate %1" "$RACCT")" "…and the pane's next wall may rotate again"
unset ANU_ROTATE_RC

# --- a notify target that vanished mid-tick must not end the tick ------------
# cmd_notify runs as a FUNCTION inside watchd (from _rot_reap), so an `exit`
# there would take the whole daemon down over one pane that disappeared
# between being enumerated and being notified.
rdir vn; rwall
export ANU_ROTATE_RC=1
RP_GATE=1 rtick 8000; RP_GATE=1 rtick 8002
rwaitf "$ANU_NOTIFY_DIR/r_default_1.rc" 80
: > "$RP_COUNT"
RP_VANISH=1 RP_GATE= rtick 8006
assert_contains "$(cat "$ANU_NOTIFY_DIR/w_default_1")" " 8006" "a notify target that vanished mid-tick does not end the tick"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "worker=0" "…the reap still completed, releasing the in-flight guard"
assert_file "$ANU_NOTIFY_DIR/w_default_2" "…and the panes after it were still processed"
unset ANU_ROTATE_RC

# --- the binary: an unusable override is reported once, and never spends the
# --- wall's real outcome notification ---------------------------------------
# $HOME is redirected so the installed-copy fallback cannot resolve either (and
# so this can never dispatch a REAL rotation from a test).
rdir nb; rwall
SAVED_PANE="$PANE"; cp "$PANE" "$rtmp/observer-only"; PANE="$rtmp/observer-only"
: > "$rtmp/not-exec"; chmod -x "$rtmp/not-exec"
for t in 6000 6002 6004; do
  RP_GATE=1 ANU_ACCOUNT_BIN="$rtmp/not-exec" HOME="$rtmp/nohome" rtick "$t"
done
assert_eq "" "$(cat "$RACCT")" "an unresolvable anu-account dispatches nothing"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "is not executable" "a non-executable ANU_ACCOUNT_BIN is named in the log…"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "no com8-account helper available" "…and so is the fall-through finding nothing"
assert_eq "1" "$(grep -c "is not executable" "$ANU_NOTIFY_DIR/r_default_1.log")" "…logged once per wall, not every tick"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "nobin=1" "…tracked by its own flag"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "notified=0" "…so the wall's outcome notification is still unspent"

PANE="$SAVED_PANE"

# --- a boxed container pane is an agent pane; a bare one is not --------------
rdir bx1; rwall
RP_GATE=1 RP_CLI=container RP_BOX=1 rtick 1900
RP_GATE=1 RP_CLI=container RP_BOX=1 rtick 1902; rwait
assert_contains "$(cat "$RACCT")" "rotate %1" "a container pane with @anu_box=1 is enumerated and rotated"
rdir bx2
RP_GATE=1 RP_CLI=container RP_BOX= rtick 1900; RP_GATE=1 RP_CLI=container RP_BOX= rtick 1902
assert_eq "" "$(cat "$RACCT")" "a container pane without @anu_box is not an agent pane at all"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/w_default_1" ] && echo 1 || echo 0)" "…and gets no watchd state file either"

# --- a walled pane never fires "Done" ----------------------------------------
rdir d1
printf '%s\n' "working" "esc to interrupt" > "$RSCREEN"
RP_GATE= rtick 2000
rwall; RP_GATE= rtick 2004; RP_GATE= rtick 2006; RP_GATE= rtick 2008
assert_eq "" "$(cat "$rtn")" "a busy pane that walls never fires Done — the turn died, it did not finish"
assert_contains "$(cat "$ANU_NOTIFY_DIR/w_default_1")" "limited " "…and the watchd state file reads limited"
assert_eq "7" "$(wc -w < "$ANU_NOTIFY_DIR/w_default_1" | tr -d ' ')" "…still exactly 7 space-separated fields (the format other code parses)"

# --- leaving the wall resets the per-wall counters ---------------------------
rdir d2; rwall
RP_GATE=1 rtick 2100                          # one tick: counted, not yet dispatched
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "confirm=1" "a walled pane carries its confirmation count"
ridle; RP_GATE=1 rtick 2102
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "confirm=0" "leaving the wall resets the confirmation count"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "notified=0" "…and the one-notification-per-wall flag"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "backoff_until=0" "…and the backoff"

# --- gc drops the rotation files for a vanished pane -------------------------
rdir gc1
: > "$ANU_NOTIFY_DIR/r_default_1"; : > "$ANU_NOTIFY_DIR/r_default_1.log"
: > "$ANU_NOTIFY_DIR/r_default_77"; : > "$ANU_NOTIFY_DIR/r_default_77.log"; : > "$ANU_NOTIFY_DIR/r_default_77.rc"
"$PANE" watchd --gc >/dev/null 2>&1
assert_file "$ANU_NOTIFY_DIR/r_default_1" "gc keeps rotation state for a live pane"
assert_file "$ANU_NOTIFY_DIR/r_default_1.log" "…and its log"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/r_default_77" ] && echo 1 || echo 0)" "gc prunes rotation state for a vanished pane"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/r_default_77.log" ] && echo 1 || echo 0)" "…its log"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/r_default_77.rc" ] && echo 1 || echo 0)" "…and its rc file"

PATH="$rop"; unset TMUX RP_CLI RP_GATE RP_SESSION RP_ACCOUNT RP_ROTATING RP_WALLEV RP_BOX
unset SCREEN_FILE ANU_NOTIFY_DIR ANU_ACTIVE_PANE_OVERRIDE ANU_NOW_OVERRIDE ANU_ACCOUNT_BIN
unset RP_VANISH RP_COUNT RP_WALLSEEN

# ------------------------------------------------ watchd (the Codex wall) ----
# The Codex wall is NOT a screen read. Codex fires no hook for a usage-limit
# turn at all; it writes a `task_complete` record with
# `codex_error_info: usage_limit_exceeded` into the session's rollout, and
# that record is the only first-party evidence there is. So the tick tails the
# rollout, and `_classify` reads `limited` off the pending stamp the tail left.

t_section "watchd (codex: the rollout record is the wall)"
ctmp="$(mktmp)"; csd="$(new_stubdir)"; ctn="$ctmp/tn.log"
CSCREEN="$ctmp/scr"; printf 'all quiet\n\u203a \n' > "$CSCREEN"
COPT="$ctmp/tmux.log"
CST="$ctmp/opts"; mkdir -p "$CST"     # the pane options the stub actually STORES
export ANU_ACTIVE_PANE_OVERRIDE=%9
# A tmux stub that both records and REMEMBERS: `set-option -p -t %1 @x v`
# writes $CST/@x, and `show-options` reads it back. That is what makes this an
# end-to-end test — the pending stamp the scan writes is the same one
# `_classify` reads a moment later, in the same tick.
stub "$csd" tmux '
printf "%s\n" "$*" >> "'"$COPT"'"
st="'"$CST"'"
case "$1" in
  set-option)
    name=""; val=""; seen=0
    for a in "$@"; do
      case "$a" in
        @*) name="$a"; seen=1; continue ;;
      esac
      [ "$seen" = 1 ] && { val="${val:+$val }$a"; }
    done
    [ -n "$name" ] || exit 0
    case "$*" in *" -pu "*|*" -pu"*) rm -f "$st/$name"; exit 0 ;; esac
    printf "%s" "$val" > "$st/$name"; exit 0 ;;
  list-panes) printf "%%1\t${CP_CLI:-codex}\t1:w\t0\t\tT\t\n" ;;
  capture-pane) cat "'"$CSCREEN"'" ;;
  list-clients) echo "/dev/ttys016" ;;
  display-message)
    case "$*" in
      *pane_dead*)            printf "%s\t%s\t%s\n" "0" "0" "${CP_CLI:-codex}" ;;
      *pane_current_command*) printf "%s\n" "${CP_CLI:-codex}" ;;
      *session_name*)         echo s ;;
      *window_name*)          echo win ;;
      *client_termname*)      echo xterm-ghostty ;;
      *)                      echo "" ;;
    esac ;;
  show-options)
    case "$*" in
      *@anu_autorotate*)       printf "%s\n" "${CP_GATE:-}" ;;
      *@anu_codex_autorotate*) printf "%s\n" "${CP_CODEX_GATE:-}" ;;
      *-pq\ *|*-pq)
        for f in "$st"/@*; do [ -e "$f" ] || continue; printf "%s %s\n" "${f##*/}" "$(cat "$f")"; done
        printf "@anu_role \n" ;;
      *)
        for a in "$@"; do case "$a" in @*) [ -e "$st/$a" ] && cat "$st/$a"; printf "\n"; exit 0 ;; esac; done
        printf "\n" ;;
    esac ;;
esac
exit 0'
stub "$csd" terminal-notifier 'printf "%s\n" "$*" >> "'"$ctn"'"; exit 0'
stub "$csd" fake-account '
printf "%s\n" "$*" >> "$ANU_NOTIFY_DIR/acct.log"
exit "${ANU_ROTATE_RC:-0}"'
export ANU_ACCOUNT_BIN="$csd/fake-account"
cop="$PATH"; PATH="$csd:$PATH"; export TMUX=fake
setopt() { printf '%s' "$2" > "$CST/$1"; }
clropt() { rm -f "$CST/$1"; }
cdir() { export ANU_NOTIFY_DIR="$ctmp/$1"; mkdir -p "$ANU_NOTIFY_DIR"
         CACCT="$ANU_NOTIFY_DIR/acct.log"; : > "$CACCT"; : > "$ctn"; : > "$COPT"
         rm -f "$CST"/@*
         setopt @anu_provider codex; setopt @anu_account account_alpha
         setopt @anu_session sid-1;   setopt @anu_turn turn-7
         setopt @anu_launch_nonce n1; setopt @anu_transcript "$ROLLOUT"; }
ctick() { ANU_NOW_OVERRIDE="$1" "$PANE" watchd --tick >/dev/null 2>&1; }
cwait() { local i=0; while [ "$i" -lt 60 ]; do [ -s "$CACCT" ] && return 0; sleep 0.1; i=$((i+1)); done; return 1; }

ROLLOUT="$ctmp/rollout.jsonl"
roll_quiet() { printf '%s\n' \
  '{"timestamp":"t","type":"session_meta","payload":{}}' \
  '{"timestamp":"t","type":"event_msg","payload":{"type":"agent_message","turn_id":"turn-7"}}' > "$ROLLOUT"; }
roll_append_wall() { printf '%s\n' \
  '{"timestamp":"t","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-7","last_agent_message":null,"error":{"message":"You have hit your usage limit.","codex_error_info":"usage_limit_exceeded"}}}' >> "$ROLLOUT"; }
roll_append_ok() { printf '%s\n' \
  '{"timestamp":"t","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-7","last_agent_message":"done","error":null}}' >> "$ROLLOUT"; }
roll_append_old_wall() { printf '%s\n' \
  '{"timestamp":"t","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-OLD","error":{"codex_error_info":"usage_limit_exceeded"}}}' >> "$ROLLOUT"; }

# --- a quiet rollout is not a wall -------------------------------------------
cdir k1; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
ctick 100
assert_eq "0" "$([ -e "$CST/@anu_wall_pending" ] && echo 1 || echo 0)" "a rollout with nothing new is not a wall"
assert_eq "idle" "$("$PANE" state %1)" "…and the pane reads idle"

# --- the wall record ----------------------------------------------------------
cdir k2; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
ctick 200
assert_contains "$(cat "$CST/@anu_wall_pending" 2>/dev/null)" "200:turn-7" \
  "a task_complete carrying codex_error_info usage_limit_exceeded stamps the pending wall"
assert_eq "codex:200" "$(cat "$CST/@anu_wall_event" 2>/dev/null)" \
  "…and the wall event, namespaced by provider"
assert_eq "$(wc -c < "$ROLLOUT" | tr -d ' ')" "$(cat "$CST/@anu_rollout_offset")" \
  "…and only THEN does the cursor advance past those bytes"
assert_eq "limited" "$("$PANE" state %1)" "a codex pane with a pending wall reads limited"
assert_contains "$("$PANE" state %1 --json)" '"wall":"session"' \
  "…and names the wall session (the record says a limit, never which window)"

# --- the record must be THIS turn's -------------------------------------------
cdir k3; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_old_wall
ctick 300
assert_eq "0" "$([ -e "$CST/@anu_wall_pending" ] && echo 1 || echo 0)" \
  "a wall record for an OLDER turn is skipped — a resumed rollout is full of them"
assert_eq "$(wc -c < "$ROLLOUT" | tr -d ' ')" "$(cat "$CST/@anu_rollout_offset")" "…and the cursor still advances past it"

# --- a turn that simply finished ----------------------------------------------
cdir k4; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_ok
ctick 400
assert_eq "0" "$([ -e "$CST/@anu_wall_pending" ] && echo 1 || echo 0)" "a task_complete with no error is not a wall"

# --- a half-written record is left for the next tick --------------------------
cdir k5; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
before="$(wc -c < "$ROLLOUT" | tr -d ' ')"
printf '%s' '{"type":"event_msg","payload":{"type":"task_comple' >> "$ROLLOUT"
ctick 500
assert_eq "$before" "$(cat "$CST/@anu_rollout_offset")" \
  "the cursor advances only to the last COMPLETE newline — Codex is still writing that line"
printf '%s\n' 'te","turn_id":"turn-7","error":{"codex_error_info":"usage_limit_exceeded"}}}' >> "$ROLLOUT"
ctick 502
assert_contains "$(cat "$CST/@anu_wall_pending" 2>/dev/null)" "502:turn-7" \
  "…and the record is read whole once the line is finished"

# --- a rollout that was replaced ----------------------------------------------
cdir k6; roll_quiet; setopt @anu_rollout_offset 999999; setopt @anu_rollout_inode 1
roll_append_wall
ctick 600
assert_contains "$(cat "$CST/@anu_wall_pending" 2>/dev/null)" "600:" \
  "a file that shrank below the cursor is re-read from 0, not from a meaningless offset"

# --- the pending wall is not a screen read, and survives one ------------------
cdir k7; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
ctick 700
printf 'a brand new answer\n\u203a \n' > "$CSCREEN"
assert_eq "limited" "$("$PANE" state %1)" "the pending wall outlives any screen; only a launch clears it"
clropt @anu_wall_pending
assert_eq "idle" "$("$PANE" state %1)" "…and with it cleared the same screen reads idle again"
printf 'all quiet\n\u203a \n' > "$CSCREEN"

# --- a pane anu did not launch as codex is never scanned ----------------------
cdir k8; roll_quiet; clropt @anu_provider; setopt @anu_rollout_offset 0
roll_append_wall
ctick 800
assert_eq "0" "$([ -e "$CST/@anu_wall_pending" ] && echo 1 || echo 0)" \
  "no @anu_provider codex: the rollout is not this tool's business"

# --- the rollout lives in ~/.codex; the pane's path runs through two links ----
# An account home's `sessions` is a link (to the old store, itself a link to
# ~/.codex/sessions, on a machine migrated in place), and the transcript_path
# Codex reports is spelled through it. The scan must read the real file.
vroot="$ctmp/view"; vcanon="$vroot/home/.codex"; vacct="$vroot/acct"
mkdir -p "$vcanon/sessions/2026/09/23" "$vacct/k"
ln -s "$vcanon/sessions" "$vacct/sessions"; ln -s "$vacct/sessions" "$vacct/k/sessions"
cdir k9; ROLLOUT="$vcanon/sessions/2026/09/23/rollout-k9.jsonl"; roll_quiet
setopt @anu_transcript "$vacct/k/sessions/2026/09/23/rollout-k9.jsonl"
setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
ctick 810
assert_contains "$(cat "$CST/@anu_wall_pending" 2>/dev/null)" "810:turn-7" \
  "a transcript spelled through the home's sessions link (home → old store → ~/.codex) is read"

# --- a pane stamped BEFORE the migration keeps being read AFTER it -----------
# The old store was a real directory when this pane's hook stamped the
# transcript; the share then merged it into ~/.codex/sessions (hard links) and
# turned the store into a link. The same path now resolves into ~/.codex — to
# the SAME inode — so the cursor stays valid and nothing keys on where the
# store used to be.
vroot="$ctmp/view2"; vcanon="$vroot/home/.codex"; vacct="$vroot/acct"
mkdir -p "$vcanon/sessions" "$vacct/sessions/2026/09/23" "$vacct/k"
ln -s "$vacct/sessions" "$vacct/k/sessions"; printf '{"tokens":"x"}\n' > "$vacct/k/auth.json"
cdir k10; ROLLOUT="$vacct/sessions/2026/09/23/rollout-k10.jsonl"; roll_quiet
setopt @anu_transcript "$vacct/k/sessions/2026/09/23/rollout-k10.jsonl"
setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
setopt @anu_rollout_inode "$(ls -i "$ROLLOUT" | awk '{print $1}')"
k10_inode="$(cat "$CST/@anu_rollout_inode")"
HOME="$vroot/home" ANU_ACCOUNT_CODEX_DIR="$vacct" "$ANU_ROOT/profiles/runtime/accounts/account" doctor --provider codex --fix >/dev/null 2>&1
assert_eq "$vcanon/sessions" "$(readlink "$vacct/sessions")" "(the share turned the old store into a link to ~/.codex/sessions)"
# The live Codex still holds the file it opened before the migration: append
# through the name that inode kept (the store's backup), exactly as its fd
# would. Only a hard-link merge lets the pane's path see those bytes.
ROLLOUT="$(ls -d "$vacct"/sessions.pre-link.* | head -1)/2026/09/23/rollout-k10.jsonl"
roll_append_wall
ctick 820
assert_contains "$(cat "$CST/@anu_wall_pending" 2>/dev/null)" "820:turn-7" \
  "a wall written after the migration is still seen through the transcript stamped before it"
assert_eq "$k10_inode" "$(ls -i "$vacct/k/sessions/2026/09/23/rollout-k10.jsonl" | awk '{print $1}')" \
  "…because the stamped path now resolves to the SAME inode, so the cursor never reset"
ROLLOUT="$ctmp/rollout.jsonl"


t_section "watchd (codex: automatic rotation is opt-in)"
# --- @anu_codex_autorotate is off by default ---------------------------------
cdir g1; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
CP_GATE=1 ctick 900; CP_GATE=1 ctick 902; CP_GATE=1 ctick 904
assert_eq "" "$(cat "$CACCT")" "@anu_autorotate on but @anu_codex_autorotate off: no codex rotation is dispatched"
assert_contains "$(cat "$CST/@anu_wall_pending" 2>/dev/null)" "900:turn-7" "…the wall is still detected and stamped"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "@anu_codex_autorotate" "…and the log says why it did not act"
assert_eq "1" "$(grep -c "@anu_codex_autorotate" "$ANU_NOTIFY_DIR/r_default_1.log")" "…once per wall, not every tick"

# --- with the codex gate on, the ordinary arm runs ---------------------------
cdir g2; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
CP_GATE=1 CP_CODEX_GATE=1 ctick 1000; cwait
assert_contains "$(cat "$CACCT")" "rotate %1" \
  "the rollout record is the provider's OWN report, so it rotates on the first tick"

# --- a failed Codex handoff is retried, under the caps Claude already uses ---
# The pending wall is not "spent" by a dispatch: a rotation that failed leaves
# the pane exactly as walled as it was, and the record that proved it is still
# the truth. Before this, the wall event was stamped once per LAUNCH and the
# spend rule then blocked every later dispatch — a dashboard hiccup left the
# pane sitting `limited` for ever, having been told "waiting for capacity".
cwaitf() { local i=0; while [ "$i" -lt "${2:-80}" ]; do [ -s "$1" ] && return 0; sleep 0.1; i=$((i+1)); done; return 1; }
# one cycle = two confirming ticks, the worker finishing, then the reap tick.
ccycle() { CP_GATE=1 CP_CODEX_GATE=1 ctick "$1"; CP_GATE=1 CP_CODEX_GATE=1 ctick "$2"
           cwaitf "$ANU_NOTIFY_DIR/r_default_1.rc" 80; CP_GATE=1 CP_CODEX_GATE=1 ctick "$3"; }

cdir rt1; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
export ANU_ROTATE_RC=1
ccycle 2000 2002 2006          # attempt 1 -> rc 1, backoff to 2126
assert_eq "1" "$(grep -c "rotate %1" "$CACCT")" "the wall dispatches once"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "attempts=1" "…counted as an attempt"
CP_GATE=1 CP_CODEX_GATE=1 ctick 2010; CP_GATE=1 CP_CODEX_GATE=1 ctick 2012
assert_eq "1" "$(grep -c "rotate %1" "$CACCT")" "…and nothing again inside the 120s backoff"
ccycle 2130 2132 2136          # attempt 2
assert_eq "2" "$(grep -c "rotate %1" "$CACCT")" "once the backoff has elapsed the SAME pending wall dispatches again"
ccycle 2260 2262 2266          # attempt 3 -> gives up
assert_eq "3" "$(grep -c "rotate %1" "$CACCT")" "three failing rotations is the cap for one Codex wall too"
assert_contains "$(cat "$ctn")" "gave up after 3 attempts" "…and the wall gets its own 'gave up' notification"
assert_eq "1" "$(grep -c "gave up" "$ctn")" "…exactly once"
CP_GATE=1 CP_CODEX_GATE=1 ctick 2400; CP_GATE=1 CP_CODEX_GATE=1 ctick 2402
assert_eq "3" "$(grep -c "rotate %1" "$CACCT")" "…and nothing more, backoff or no backoff"
unset ANU_ROTATE_RC

# rc 2 is not a failed attempt — nothing had room, and the 600s backoff is the
# right answer to that. It must still come back.
cdir rt2; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
export ANU_ROTATE_RC=2
ccycle 3000 3002 3006
assert_eq "1" "$(grep -c "rotate %1" "$CACCT")" "nothing has room: one dispatch"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "attempts=0" "…which is not counted against the attempt cap"
CP_GATE=1 CP_CODEX_GATE=1 ctick 3100; CP_GATE=1 CP_CODEX_GATE=1 ctick 3102
assert_eq "1" "$(grep -c "rotate %1" "$CACCT")" "…nothing inside the 600s backoff"
ccycle 3610 3612 3616
assert_eq "2" "$(grep -c "rotate %1" "$CACCT")" "…and the pending wall tries again once it elapses"
unset ANU_ROTATE_RC

# A wall on a LATER turn is read even while an older one is still pending —
# the scan no longer stops at the pending stamp, and the cursor is what keeps
# it from re-reading the record it already acted on.
cdir rt3; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
ctick 4000
assert_contains "$(cat "$CST/@anu_wall_pending")" "4000:turn-7" "turn 7 walls"
ctick 4002
assert_contains "$(cat "$CST/@anu_wall_pending")" "4000:turn-7" "…and is not re-read from behind the cursor"
setopt @anu_turn turn-8
printf '%s\n' '{"timestamp":"t","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-8","error":{"codex_error_info":"usage_limit_exceeded"}}}' >> "$ROLLOUT"
ctick 4010
assert_contains "$(cat "$CST/@anu_wall_pending")" "4010:turn-8" "a wall on a later turn is read even with an older one pending"
assert_eq "codex:4010" "$(cat "$CST/@anu_wall_event")" "…and stamps a fresh wall event"

# --- @anu_autorotate 0 still pauses both providers ---------------------------
cdir g3; roll_quiet; setopt @anu_rollout_offset "$(wc -c < "$ROLLOUT" | tr -d ' ')"
roll_append_wall
CP_GATE= CP_CODEX_GATE=1 ctick 1100; CP_GATE= CP_CODEX_GATE=1 ctick 1102
assert_eq "" "$(cat "$CACCT")" "@anu_autorotate off pauses the codex arm too"

PATH="$cop"; unset TMUX CP_CLI CP_GATE CP_CODEX_GATE ANU_NOTIFY_DIR ANU_NOW_OVERRIDE
unset ANU_ACCOUNT_BIN ANU_ACTIVE_PANE_OVERRIDE

# ------------------------------------------ one server's files, one server ---
# need_/cd_/.lk_ files are keyed by the server's socket basename AND the pane,
# and every verb that runs outside a pane (watchd, a notification click)
# selects its server with --socket rather than inheriting whatever $TMUX says.

t_section "state files and clicks carry the socket"
stmp="$(mktmp)"; ssd="$(new_stubdir)"; stn="$stmp/tn.log"; stl="$stmp/tmux.log"
stub "$ssd" tmux '
printf "TMUX=%s PANE=%s :: %s\n" "${TMUX-<unset>}" "${TMUX_PANE-<unset>}" "$*" >> "'"$stl"'"
case "$1" in
  list-panes)   printf "%%1\tcodex\t1:w\t1\trev\tT\t\n" ;;
  list-clients) echo "/dev/ttys016" ;;
  capture-pane) printf "idle\n› \n" ;;
  display-message) case "$*" in
      *socket_path*) echo "${TMUX%%,*}" ;;
      *pane_dead*) printf "0\t0\tcodex\n" ;;
      *pane_current_command*) echo codex ;;
      *session_name*) echo s ;; *window_name*) echo win ;; *window_index*) echo 1 ;;
      *client_termname*) echo xterm-ghostty ;; *) echo "" ;; esac ;;
  show-options) echo "" ;;
esac
exit 0'
stub "$ssd" terminal-notifier 'printf "%s\n" "$*" >> "'"$stn"'"; exit 0'
stub "$ssd" osascript 'exit 0'
op="$PATH"; PATH="$ssd:$PATH"; export ANU_NOTIFY_DIR="$stmp/n" ANU_ACTIVE_PANE_OVERRIDE=%9
# two servers, the same %1
TMUX=/tmp/tmux-test/work,11,0 TMUX_PANE=%1 "$PANE" call "from work" >/dev/null 2>&1
TMUX=/tmp/tmux-test/default,22,0 TMUX_PANE=%1 "$PANE" call "from default" >/dev/null 2>&1
assert_contains "$(cat "$ANU_NOTIFY_DIR/need_work_1" 2>/dev/null)" "from work" "a need record is keyed by socket basename + pane"
assert_contains "$(cat "$ANU_NOTIFY_DIR/need_default_1" 2>/dev/null)" "from default" "…so another server's %1 has its own, not the same file"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/need_1" ] && echo 1 || echo 0)" "…and nothing is keyed by the pane number alone"
nw="$(TMUX=/tmp/tmux-test/work,11,0 "$PANE" needs --json)"
assert_contains "$nw" "from work" "pane needs lists its own server's records"
assert_not_contains "$nw" "from default" "…never another server's"
assert_contains "$(cat "$stn")" "--socket /tmp/tmux-test/work" "the click command names the pane's own server"
: > "$stn"
TMUX=/tmp/tmux-test/work,11,0 "$PANE" notify "hi" --kind info --target %1 >/dev/null 2>&1
assert_file "$ANU_NOTIFY_DIR/cd_work_1_info" "the cooldown stamp is keyed by socket too"
assert_eq "0" "$(ls -a "$ANU_NOTIFY_DIR" | grep -c '^\.lk_1_')" "…and so is the emit lock (none left keyed by pane alone)"
# a click: no $TMUX at all, --socket selects the server, the right record goes
: > "$stl"
env -u TMUX -u TMUX_PANE "$PANE" focus %1 --socket /tmp/tmux-test/work >/dev/null 2>&1
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/need_work_1" ] && echo 1 || echo 0)" "focus --socket clears that server's record"
assert_file "$ANU_NOTIFY_DIR/need_default_1" "…and leaves the other server's %1 alone"
assert_eq "0" "$(grep -vc '^TMUX=/tmp/tmux-test/work,0,0 PANE=<unset> ::' "$stl")" "…every tmux call it makes goes to that server"

# watchd --socket X run by hand from inside ANOTHER server
: > "$stl"
TMUX=/tmp/tmux-test/other,33,0 TMUX_PANE=%1 "$PANE" watchd --tick --socket /tmp/tmux-test/work >/dev/null 2>&1
assert_ne "0" "$(grep -c . "$stl")" "watchd --tick --socket runs its tick"
assert_eq "0" "$(grep -vc '^TMUX=/tmp/tmux-test/work,0,0 PANE=<unset> ::' "$stl")" "watchd --socket X points every tmux call at X, not at the caller's server"
assert_file "$ANU_NOTIFY_DIR/w__tmp_tmux_test_work_1" "…and the pane it watched is the one TMUX_PANE named on the other server (never excluded as 'self')"
: > "$stl"
TMUX=/tmp/tmux-test/work,44,2 "$PANE" watchd --tick --socket /tmp/tmux-test/work >/dev/null 2>&1
assert_eq "0" "$(grep -vc '^TMUX=/tmp/tmux-test/work,44,2 ' "$stl")" "a \$TMUX that already names that server is kept as run-shell handed it"
PATH="$op"; unset ANU_NOTIFY_DIR ANU_ACTIVE_PANE_OVERRIDE

# ------------------------------- watchd: a failed rotation parked at a shell ---

t_section "watchd (a rotation that left its pane at a shell is still reaped)"
atmp="$(mktmp)"; asd="$(new_stubdir)"; atn="$atmp/tn.log"
stub "$asd" tmux '
case "$1" in
  list-panes)   printf "%%1\t${AP_CLI:-bash}\t1:w\t0\t\tT\t\n%%2\tbash\t1:w\t0\t\tT\t\n" ;;
  list-clients) echo "/dev/ttys016" ;;
  capture-pane) printf "data main ?\n" ;;
  display-message) case "$*" in
      *pane_dead*) printf "0\t0\t%s\n" "${AP_CLI:-bash}" ;;
      *pane_current_command*) echo "${AP_CLI:-bash}" ;;
      *session_name*) echo s ;; *window_name*) echo win ;;
      *client_termname*) echo xterm-ghostty ;; *) echo "" ;; esac ;;
  show-options) echo "" ;;
esac
exit 0'
stub "$asd" terminal-notifier 'printf "%s\n" "$*" >> "'"$atn"'"; exit 0'
op="$PATH"; PATH="$asd:$PATH"; export TMUX=fake ANU_ACTIVE_PANE_OVERRIDE=%9 ANU_NOTIFY_DIR="$atmp/n"
mkdir -p "$ANU_NOTIFY_DIR"
parked="anu account: rotation failed — no codex account with room is logged in on this device (account_delta did not stay up; account_gamma, the account that walled, is the only codex login left on this device); resume with: com8-account launch --provider codex --as account_gamma -- resume sid-1"
/bin/sleep 0 & gone=$!; wait "$gone"
printf 'confirm=0\nattempts=1\nworker=%s\nstarted=990\nrc=\nbackoff_until=0\nnotified=0\nnobin=0\nnohook=0\nnogate=0\nerr=\n' "$gone" > "$ANU_NOTIFY_DIR/r_default_1"
printf 'Error: … unauthorized (401)\n%s\n' "$parked" > "$ANU_NOTIFY_DIR/r_default_1.log"
printf 'rc=1\n' > "$ANU_NOTIFY_DIR/r_default_1.rc"
: > "$ANU_NOTIFY_DIR/r_default_2"      # a shell pane with no rotation at all
ANU_NOW_OVERRIDE=1000 "$PANE" watchd --tick >/dev/null 2>&1
assert_contains "$(cat "$atn")" "rotation failed — no codex account with room" "a pane parked at a shell after a failed relaunch is reaped on the next tick"
assert_contains "$(cat "$atn")" "resume with: com8-account launch" "…and its notice IS the worker's one line"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "worker=0" "…the in-flight guard released"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1")" "rc=1" "…the exit code recorded"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "rotate ended rc=1" "…and logged"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/w_default_1" ] && echo 1 || echo 0)" "…without classifying the shell pane as an agent"
assert_eq "1" "$(grep -c . "$atn")" "…one notice, and none for a shell pane with nothing in flight"
ANU_NOW_OVERRIDE=1002 "$PANE" watchd --tick >/dev/null 2>&1
assert_eq "1" "$(grep -c . "$atn")" "…and never twice"
"$PANE" watchd --gc >/dev/null 2>&1
assert_file "$ANU_NOTIFY_DIR/r_default_1" "gc keeps a shell pane's rotation record — the pane still exists"
: > "$ANU_NOTIFY_DIR/r_default_77"
"$PANE" watchd --gc >/dev/null 2>&1
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/r_default_77" ] && echo 1 || echo 0)" "…and drops a vanished pane's"

PATH="$op"; unset TMUX ANU_ACTIVE_PANE_OVERRIDE ANU_NOTIFY_DIR ANU_ACCOUNT_BIN AP_CLI

# ---------------------------------------------------- watchd: the rebalance arm ---
# An IDLE pane is handed to `com8-account rebalance <pane> --tick` — which holds
# the rule — only when the cheap facts say it could move. Run against the REAL
# pane executable, a tmux stub that serves the enumerator, the per-pane stamps
# (one list-panes), the cached ranking and the clients, and a fake anu-account
# that records its argv and exits with the verb's contract (0 moved, 2 stayed,
# 3 dashboard unreachable, 1 failed).

t_section "watchd (rebalance arm)"
btmp="$(mktmp)"; bsd="$(new_stubdir)"; btn="$btmp/tn.log"; BOPT="$btmp/tmux.log"
BSCREEN="$btmp/scr"; export SCREEN_FILE="$BSCREEN"
stub "$bsd" tmux '
printf "%s\n" "$*" >> "'"$BOPT"'"
case "$1" in
  list-panes)
    case "$*" in
      *@anu_rebalance_seen*) printf "%s\n" "${BP_ROWS-%1|account_beta|abcd|||0|||}" ;;
      *) printf "%s\n" "${BP_LIST-%1	claude	1:w	0		T	}" ;;
    esac ;;
  capture-pane) cat "'"$BSCREEN"'" ;;
  list-clients) case "$*" in *client_activity*) printf "%s\n" "${BP_CLIENTS-}" ;; *) echo "/dev/ttys016" ;; esac ;;
  display-message)
    case "$*" in
      *pane_dead*)            printf "0\t0\tclaude\n" ;;
      *pane_current_command*) echo claude ;;
      *session_name*) echo s ;; *window_name*) echo win ;; *client_termname*) echo xterm-ghostty ;;
      *) echo "" ;;
    esac ;;
  show-options)
    case "$*" in
      *@anu_rebalance_rank_*) k="${3#@anu_rebalance_rank_}"
                              eval "printf \"%s\\n\" \"\${BP_RANK_$k-}\"" ;;
      *@anu_rebalance*)       printf "%s\n" "${BP_GATE-}" ;;
      *) printf "\n" ;;
    esac ;;
esac
exit 0'
stub "$bsd" terminal-notifier 'printf "%s\n" "$*" >> "'"$btn"'"; exit 0'
stub "$bsd" fake-account '
printf "%s\n" "$*" >> "$ANU_NOTIFY_DIR/acct.log"
printf "TMUX=%s\n" "${TMUX-<unset>}" >> "$ANU_NOTIFY_DIR/acct.env"
[ -n "${BP_SLEEP:-}" ] && sleep "$BP_SLEEP"
case "${BP_RC:-2}" in
  0) printf "[12:00:00] rebalance %s (sooner-reset): %s: account_beta → account_alpha (switch, session abcd, ready via hook)\n" "$2" "$2" ;;
  3) printf "[12:00:00] rebalance %s: stays on account_beta — dashboard-unreachable\n" "$2" ;;
esac
exit "${BP_RC:-2}"'
export ANU_ACCOUNT_BIN="$bsd/fake-account"
bop="$PATH"; PATH="$bsd:$PATH"; export TMUX=fake ANU_ACTIVE_PANE_OVERRIDE=%9
bidle() { printf '%s\n' "all done" "› " > "$BSCREEN"; }
bbusy() { printf '%s\n' "working" "esc to interrupt" > "$BSCREEN"; }
# A fresh case: its own notify dir, so its state and the fake's logs are its own.
bdir() { export ANU_NOTIFY_DIR="$btmp/$1"; mkdir -p "$ANU_NOTIFY_DIR"
         BACCT="$ANU_NOTIFY_DIR/acct.log"; : > "$BACCT"; : > "$ANU_NOTIFY_DIR/acct.env"
         : > "$btn"; : > "$BOPT"; bidle; }
btick() { ANU_NOW_OVERRIDE="$1" "$PANE" watchd --tick ${2:+--socket "$2"} >/dev/null 2>&1; }
# Two ticks: the arm only ever looks at a pane read idle on this tick AND the last.
bpair() { btick "$1" "${3:-}"; btick "$2" "${3:-}"; }
bwaitf() { local i=0; while [ "$i" -lt "${2:-60}" ]; do [ -s "$1" ] && return 0; sleep 0.1; i=$((i+1)); done; return 1; }
bwait() { bwaitf "$ANU_NOTIFY_DIR/b_default.rc"; }
dispatched() { grep -c "^rebalance %${1:-1} --tick$" "$BACCT" | tr -d ' '; }
# Whether the tick dispatched at all, read from the arm's own state — written
# before the tick returns, so a "nothing" is never just a worker not started yet.
bpane() { sed -n 's/^pane=//p' "$ANU_NOTIFY_DIR/b_default" 2>/dev/null; }
looks() { grep -c "@anu_rebalance_seen" "$BOPT" | tr -d ' '; }

# --- two idle ticks, then one worker -------------------------------------------
bdir b1
btick 100
assert_eq "" "$(bpane)" "one idle tick is not enough — a pane between two turns can read idle for a moment"
btick 102; bwait
assert_eq "1" "$(dispatched)" "idle on two ticks, no cached ranking: one \`rebalance %1 --tick\` worker"
assert_contains "$(cat "$ANU_NOTIFY_DIR/b_default")" "pane=%1" "…recorded, per server, in b_<sock>"
bdir b1b; bbusy; btick 100; bidle; btick 102
assert_eq "" "$(bpane)" "a pane that was working a tick ago is not a candidate yet"
btick 104; bwait
assert_eq "1" "$(dispatched)" "…and is one the tick after"

# --- the gate --------------------------------------------------------------------
bdir g1
BP_GATE=0 bpair 100 102
assert_eq "" "$(bpane)" "@anu_rebalance 0: nothing is dispatched"
assert_eq "0" "$(looks)" "…and not a pane's stamps are read"
bdir g2
BP_GATE=1 bpair 100 102; bwait
assert_eq "1" "$(dispatched)" "@anu_rebalance 1 is on"
bdir g3
BP_GATE= bpair 100 102; bwait
assert_eq "1" "$(dispatched)" "…and unset is on too — the gate defaults ON, unlike @anu_autorotate"

# --- an unmanaged pane is skipped, silently --------------------------------------
bdir u1
BP_ROWS="%1||abcd|||0|||" bpair 100 102
assert_eq "" "$(bpane)" "no @anu_account: never handed to the verb"
assert_eq "0" "$(ls "$ANU_NOTIFY_DIR" | grep -c '^r_')" "…and no log line — not even a log file"
bdir u2
BP_ROWS="%1|account_beta||||0|||" bpair 100 102
assert_eq "" "$(bpane)" "no @anu_session: nothing to resume, nothing dispatched"

# --- the cached ranking answers the steady state on the spot ---------------------
bdir c1
BP_RANK_claude="90 1000 account_beta {\"picks\":[]}" bpair 100 102
assert_eq "" "$(bpane)" "a fresh ranking whose best IS the pane's account: nothing to ask"
bdir c2
BP_RANK_claude="90 1000 account_alpha {\"picks\":[]}" BP_ROWS="%1|account_beta|abcd|||0|||90 account_beta" bpair 100 102
assert_eq "" "$(bpane)" "a pane already judged against this very ranking (@anu_rebalance_seen) is not asked again"
bdir c3
BP_RANK_claude="90 1000 account_alpha {\"picks\":[]}" BP_ROWS="%1|account_beta|abcd|||0|||80 account_beta" bpair 100 102; bwait
assert_eq "1" "$(dispatched)" "…but a judgement against an OLDER ranking is: that ranking is gone"
bdir c4
BP_RANK_claude="90 1000 account_alpha {\"picks\":[]}" BP_ROWS="%1|account_beta|abcd|||0|||90 account_gamma" bpair 100 102; bwait
assert_eq "1" "$(dispatched)" "…and so is one made while the pane was on another account"
bdir c5
BP_RANK_claude="90 1000 account_alpha {\"picks\":[]}" bpair 100 102; bwait
assert_eq "1" "$(dispatched)" "a fresh ranking whose best is another account: the worker decides"
bdir c6
BP_RANK_claude="90 101 account_beta {\"picks\":[]}" bpair 100 102; bwait
assert_eq "1" "$(dispatched)" "a ranking past its expiry is not trusted, even when it names the pane's own account: the worker refreshes it"
bdir c7
BP_RANK_claude_fable="90 1000 account_beta {\"picks\":[]}" BP_RANK_claude="90 1000 account_alpha {\"picks\":[]}" \
  BP_ROWS="%1|account_beta|abcd||fable|0|||" bpair 100 102
assert_eq "" "$(bpane)" "a fable pane reads the fable ranking, @anu_rebalance_rank_claude_fable"
bdir c8
BP_RANK_codex="90 1000 account_beta {\"picks\":[]}" BP_RANK_claude="90 1000 account_alpha {\"picks\":[]}" \
  BP_ROWS="%1|account_beta|abcd|codex|fable|0|||" bpair 100 102
assert_eq "" "$(bpane)" "…and a codex pane the codex one, whatever need it carries"

# --- the guards the tick can see for itself --------------------------------------
bdir k1
BP_ROWS="%1|account_beta|abcd|||1|||" bpair 100 102
assert_eq "" "$(bpane)" "a boxed pane is never handed over"
bdir k2
BP_ROWS="%1|account_beta|abcd|||0|$((30002 - 21599))||" bpair 30000 30002
assert_eq "" "$(bpane)" "a pane rebalanced inside the last 6h is not"
bdir k3
BP_ROWS="%1|account_beta|abcd|||0|$((30002 - 21600))||" bpair 30000 30002; bwait
assert_eq "1" "$(dispatched)" "…and is again at 6h"
bdir k4
BP_ROWS="%1|account_beta|abcd|||0||$((30002 - 1799))|" bpair 30000 30002
assert_eq "" "$(bpane)" "a pane a rotation or switch relaunched inside 30m is not"
bdir k5
BP_ROWS="%1|account_beta|abcd|||0||$((30002 - 1800))|" bpair 30000 30002; bwait
assert_eq "1" "$(dispatched)" "…and is again at 30m"
bdir k6; bbusy
bpair 100 102
assert_eq "" "$(bpane)" "a working pane is never handed over"
bdir k7
BP_CLIENTS="92|%1" bpair 100 102
assert_eq "" "$(bpane)" "the pane a human is typing in (a client's current pane, a key 10s ago) is not"
bdir k8
BP_CLIENTS="$((102 - 301))|%1" bpair 100 102; bwait
assert_eq "1" "$(dispatched)" "…a client quiet for 5 min is not typing"
bdir k9
sleep 30 & inflight=$!
printf 'confirm=0\nattempts=1\nworker=%s\nstarted=99\nrc=\nbackoff_until=0\nnotified=0\nnobin=0\nnohook=0\nnogate=0\nerr=\n' "$inflight" > "$ANU_NOTIFY_DIR/r_default_1"
bpair 100 102
assert_eq "" "$(bpane)" "a pane whose ROTATION is still in flight belongs to it"
kill "$inflight" 2>/dev/null; wait "$inflight" 2>/dev/null

# --- one worker per server, then the next pane -----------------------------------
bdir o1
export BP_LIST="$(printf '%%1\tclaude\t1:w\t0\t\tT\t\n%%2\tclaude\t1:w\t0\t\tT\t')"
export BP_ROWS="$(printf '%s\n' '%1|account_beta|abcd|||0|||' '%2|account_beta|efgh|||0|||' '%1|account_beta|abcd|||0|||')"
bstarted() { sed -n 's/^started=//p' "$ANU_NOTIFY_DIR/b_default" 2>/dev/null; }
BP_SLEEP=3 bpair 100 102; bwaitf "$BACCT"
assert_eq "1" "$(dispatched 1)" "two eligible panes: ONE worker per tick per server — the first"
assert_eq "%1" "$(bpane)" "…not the second"
BP_SLEEP=3 btick 104
assert_eq "%1 102" "$(bpane) $(bstarted)" "…nor anything while that worker still runs"
bwait; btick 106
assert_eq "%2 106" "$(bpane) $(bstarted)" "once it is reaped, the next pane — in the same tick"
bwait
assert_eq "1" "$(dispatched 2)" "…exactly once"
btick 108
assert_eq "%2 106" "$(bpane) $(bstarted)" "each pane just decided is left alone for its interval, whatever its answer (and a linked session's repeat is one pane)"
btick 138; bwait
assert_eq "%1 138" "$(bpane) $(bstarted)" "…and asked again after it"
assert_eq "2" "$(dispatched 1)" "…twice in all"
unset BP_LIST BP_ROWS

# --- a look with nothing to do waits an interval before the next ------------------
bdir i1
BP_RANK_claude="90 10000 account_beta {\"picks\":[]}" bpair 100 102
assert_eq "1" "$(looks)" "a look reads every pane's stamps in ONE list-panes"
BP_RANK_claude="90 10000 account_beta {\"picks\":[]}" btick 104
BP_RANK_claude="90 10000 account_beta {\"picks\":[]}" btick 131
assert_eq "1" "$(looks)" "…and none again inside the interval (30s)"
BP_RANK_claude="90 10000 account_beta {\"picks\":[]}" btick 132
assert_eq "2" "$(looks)" "…one when it has passed"

# --- the reap: what the worker said ------------------------------------------------
bdir r0
BP_RC=0 bpair 100 102; bwait; bbusy; btick 104
assert_contains "$(cat "$btn")" "rebalanced to account_alpha" "rc 0: one notice, naming the account the pane moved to — reaped whatever the pane is doing now"
assert_contains "$(cat "$ANU_NOTIFY_DIR/b_default")" "worker=0" "…the worker is released"
assert_contains "$(cat "$ANU_NOTIFY_DIR/b_default")" "rc=0" "…its exit code kept"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "rebalance %1 (sooner-reset): %1: account_beta → account_alpha" "…and its one line is in the pane's rotation log"
bdir r2
: > "$ANU_NOTIFY_DIR/r_default_1.log"; printf 'an earlier wall\n' > "$ANU_NOTIFY_DIR/r_default_1.log"
BP_RC=2 bpair 100 102; bwait; btick 104
assert_eq "" "$(cat "$btn")" "rc 2: the pane stays, and nobody is told"
assert_contains "$(cat "$ANU_NOTIFY_DIR/r_default_1.log")" "an earlier wall" "…and a worker appends to the rotation log, never truncates it"
bdir r3
BP_RC=3 bpair 100 102; bwait; btick 104
assert_contains "$(cat "$ANU_NOTIFY_DIR/b_default")" "next=224" "rc 3 (dashboard unreachable): the arm backs off 120s"
BP_RC=3 btick 200; btick 202
assert_eq "102" "$(sed -n 's/^started=//p' "$ANU_NOTIFY_DIR/b_default")" "…dispatching nothing until then"

# --- the socket: the worker targets the daemon's own server ----------------------
bdir s1
bpair 100 102 /tmp/x; bwaitf "$ANU_NOTIFY_DIR/acct.env"
assert_eq "TMUX=/tmp/x,0,0" "$(cat "$ANU_NOTIFY_DIR/acct.env")" "--socket <s>: the worker is pointed at that server"
assert_file "$ANU_NOTIFY_DIR/b__tmp_x" "…and its state is keyed by that socket"

# --- no anu-account: said once, nothing dispatched --------------------------------
bdir n1
SAVED_PANE="$PANE"; cp "$PANE" "$btmp/observer-only"; PANE="$btmp/observer-only"
: > "$btmp/not-exec"; chmod -x "$btmp/not-exec"
for t in 100 102 140 180; do ANU_ACCOUNT_BIN="$btmp/not-exec" HOME="$btmp/nohome" btick "$t"; done
assert_eq "" "$(bpane)" "an unresolvable anu-account dispatches nothing"
assert_eq "1" "$(grep -c "no com8-account helper available" "$ANU_NOTIFY_DIR/r_default_1.log")" "…and says so once"

PANE="$SAVED_PANE"
PATH="$bop"; unset TMUX SCREEN_FILE ANU_NOTIFY_DIR ANU_ACTIVE_PANE_OVERRIDE ANU_ACCOUNT_BIN

# ------------------------------------------------------ watchd lifecycle ------

t_section "watchd lifecycle (ensure/gc)"
ltmp="$(mktmp)"; lsd="$(new_stubdir)"
export ANU_NOTIFY_DIR="$ltmp/wl"
stub "$lsd" tmux 'exit 0'
op="$PATH"; PATH="$lsd:$PATH"; export TMUX=fake
mkdir -p "$ANU_NOTIFY_DIR"; echo "$$" > "$ANU_NOTIFY_DIR/watchd-default.pid"
lout="$("$PANE" watchd --ensure 2>&1)"; assert_ok "$?" "watchd --ensure exits 0 with a live daemon"
assert_contains "$lout" "already" "ensure reports an existing live daemon"
echo "999999" > "$ANU_NOTIFY_DIR/watchd-default.pid"
"$PANE" watchd --ensure --no-spawn >/dev/null 2>&1; assert_ok "$?" "ensure handles a stale pidfile without spawning"
# gc prunes state files for vanished panes, keeps live ones
export ANU_NOTIFY_DIR="$ltmp/wg"; mkdir -p "$ANU_NOTIFY_DIR"
: > "$ANU_NOTIFY_DIR/w_default_1"; : > "$ANU_NOTIFY_DIR/w_default_77"
stub "$lsd" tmux 'case "$1" in list-panes) printf "%%1\tcodex\t1:w\t0\t\tT\n" ;; esac; exit 0'
"$PANE" watchd --gc >/dev/null 2>&1
assert_file "$ANU_NOTIFY_DIR/w_default_1" "gc keeps state for a live pane"
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/w_default_77" ] && echo 1 || echo 0)" "gc prunes state for a vanished pane"
# singleton: a freshly-started loop must NOT take over when a live sibling already owns the
# pidfile (prevents a second persistent watchd if a start races the check). `--once` runs the
# guard + at most one tick then exits, so this is bounded + deterministic (no background loop).
export ANU_NOTIFY_DIR="$ltmp/sg"; mkdir -p "$ANU_NOTIFY_DIR"
stub "$lsd" tmux 'case "$1" in list-panes) printf "%%1\tcodex\t1:w\t0\t\tT\n" ;; capture-pane) printf "idle\n> \n" ;; esac; exit 0'
# live sibling owns the pidfile -> the loop yields (returns before writing) -> pidfile untouched
printf '%s' "$$" > "$ANU_NOTIFY_DIR/watchd-default.pid"   # the test process is a live "sibling"
"$PANE" watchd --once >/dev/null 2>&1
assert_eq "$$" "$(cat "$ANU_NOTIFY_DIR/watchd-default.pid" 2>/dev/null)" "watchd yields to a live sibling (no double-spawn)"
# stale (dead) owner -> the loop takes over (writes its pid, runs, then cleans its own pidfile on exit)
echo "999999" > "$ANU_NOTIFY_DIR/watchd-default.pid"
"$PANE" watchd --once >/dev/null 2>&1
assert_eq "0" "$([ -e "$ANU_NOTIFY_DIR/watchd-default.pid" ] && echo 1 || echo 0)" "watchd takes over a stale (dead-owner) pidfile"
PATH="$op"; unset TMUX ANU_NOTIFY_DIR

PATH="$old_path"
unset TMUX PANE_CLI PANE_ROLE REPLY_ID SCREEN_FILE

t_done
