# shellcheck shell=bash
# communicate :: ask — synchronous, correlated ask of ANY agent by name.
#
# Stands up an ephemeral reply listener (socket + a real numeric sidecar, so
# native SendMessage replies work), appends an in-band [reply-to ...] coaching
# block teaching the receiver every way to answer, delivers via the right lane
# (router socket for Claude/peers; codex queue for a LOCAL Codex session found
# by native thread name), and blocks until the first reply lands. Per-ask
# sockets make correlation structural: whatever arrives on this socket IS the
# answer (com8's natural-reply forgiveness, without tokens).

_ask_codex_thread() { # <name> -> thread id of newest local codex session with that name
  python3 - "$1" "${COMM_CODEX_INDEX:-$HOME/.codex/session_index.jsonl}" <<'PY'
import json, sys
name, idx = sys.argv[1], sys.argv[2]
best = None
try:
    for ln in open(idx):
        try: d = json.loads(ln)
        except Exception: continue
        if d.get("thread_name") == name and d.get("id"):
            best = d["id"]
except OSError:
    pass
sys.stdout.write(best or "")
PY
}

# Stop a reply listener: with a grace period it runs inside a subshell, so its
# python child is stopped by parent pid as well.
_ask_stop() { pkill -P "$1" 2>/dev/null; kill "$1" 2>/dev/null; }

ask_cmd() {
  comm_need_python
  local name="${1:-}"; shift || true
  [ -n "$name" ] || die "usage: communicate ask <name> [--timeout SEC] [--as NAME] <question>"
  local timeout=90 as=""
  local -a msg=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --timeout) timeout="$2"; shift 2;;
      --as) as="$2"; shift 2;;
      --) shift; msg+=("$@"); break;;
      *) msg+=("$1"); shift;;
    esac
  done
  [ "${#msg[@]}" -gt 0 ] || die "empty question"
  case "$timeout" in ''|.|*[!0-9.]*|*.*.*) die "--timeout takes seconds (whole or decimal), got '$timeout'";; esac
  local asker="${as:-ask-$$}"
  local question; question="$(printf '%s ' "${msg[@]}")"; question="${question% }"

  # Resolve the lane: router name first, then a local Codex session by thread name.
  local lane="" info="" sock="" thread=""
  if info="$(router_whereis "$name" 2>/dev/null)"; then
    lane=claude; sock="${info##*$'\t'}"
  else
    thread="$(_ask_codex_thread "$name")"
    [ -n "$thread" ] && lane=codex
  fi
  [ -n "$lane" ] || die "no agent or local codex session named '$name' (see: communicate agents; codex sessions come from ~/.codex/session_index.jsonl)"

  # The reply listener: ephemeral socket + planted numeric sidecar. It outlives
  # the ask by COMM_ASK_GRACE seconds (0 = no grace), so an answer that misses
  # the timeout is saved to a file instead of hitting a removed socket.
  local grace="${COMM_ASK_GRACE:-600}"
  case "$grace" in ''|*[!0-9]*) die "COMM_ASK_GRACE must be whole seconds, got '$grace'";; esac
  local rsock; rsock="$(comm_socket_dir)/ask-$$-$RANDOM.sock"
  local out
  if [ "$grace" -gt 0 ]; then
    local late_dir="$COMM_STATE/ask-late" tag="${asker//[^A-Za-z0-9._-]/_}" total
    mkdir -p "$late_dir" && chmod 700 "$late_dir" 2>/dev/null
    out="$(mktemp "$late_dir/$tag-XXXXXX")" || die "cannot create a late-reply file in $late_dir"
    total="$(awk -v t="$timeout" -v g="$grace" 'BEGIN { print t + g }')"
    # Wrapped so a listener that never gets an answer leaves no empty file. Its
    # stdio is detached: a caller capturing this ask's output must not have to
    # wait out the grace period too.
    ( python3 "$CC_PEER_PY" recv --socket "$rsock" --timeout "$total" --unwrap \
        --plant-name "$asker" --sessions-dir "$(comm_sessions_dir)" >"$out" 2>/dev/null
      [ -s "$out" ] || rm -f "$out" ) </dev/null >/dev/null 2>&1 &
  else
    out="$(mktemp)"
    python3 "$CC_PEER_PY" recv --socket "$rsock" --timeout "$timeout" --unwrap \
      --plant-name "$asker" --sessions-dir "$(comm_sessions_dir)" >"$out" 2>/dev/null &
  fi
  local rpid=$!
  local i=0; while [ ! -S "$rsock" ] && [ $i -lt 30 ]; do sleep 0.1; i=$((i+1)); done
  [ -S "$rsock" ] || { _ask_stop "$rpid"; rm -f "$out"; die "reply listener failed to start"; }

  local coached="$question

$(comm_coach_text "$asker" "$rsock")"

  case "$lane" in
    claude)
      log "ask -> $name [claude] (reply listener: $asker)"
      python3 "$CC_PEER_PY" send --to "$sock" --text "$coached" --from "$rsock" --name "$asker" \
        || { _ask_stop "$rpid"; rm -f "$out"; die "delivery failed"; };;
    codex)
      log "ask -> $name [codex session $thread] (reply listener: $asker)"
      codex_queue local "$thread" -- "$coached" >/dev/null \
        || { _ask_stop "$rpid"; rm -f "$out"; die "codex queue failed"; };;
  esac

  if [ "$grace" -gt 0 ]; then
    # Wait up to the timeout for the first reply; the listener may run on past it.
    local ticks; ticks="$(awk -v t="$timeout" 'BEGIN { n = int(t * 5); if (n < t * 5) n++; print n }')"
    i=0
    while [ "$i" -lt "$ticks" ] && [ ! -s "$out" ] && kill -0 "$rpid" 2>/dev/null; do
      sleep 0.2; i=$((i+1))
    done
    if [ ! -s "$out" ] && kill -0 "$rpid" 2>/dev/null; then
      # Timed out with the listener still up: leave it to save a late reply.
      disown "$rpid" 2>/dev/null || true
      log "no reply within ${timeout}s — the question WAS delivered; a reply in the next ${grace}s will be saved to $out"
      return 2
    fi
  fi
  # Without a grace period the listener exits by itself at the timeout; with one
  # it has answered or died. Reap it, then read whatever it left: a reply that
  # lands between the checks above is still the answer, not litter.
  wait "$rpid" 2>/dev/null || true
  if [ -s "$out" ]; then
    cat "$out"; echo
    rm -f "$out"; return 0
  fi
  rm -f "$out"
  log "no reply within ${timeout}s — the question WAS delivered; the answer may arrive later (codex: check that session's UI/history)"
  return 2
}
