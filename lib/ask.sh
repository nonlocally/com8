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

  # The reply listener: ephemeral socket + planted numeric sidecar.
  local rsock; rsock="$(comm_socket_dir)/ask-$$-$RANDOM.sock"
  local out; out="$(mktemp)"
  python3 "$CC_PEER_PY" recv --socket "$rsock" --timeout "$timeout" --unwrap \
    --plant-name "$asker" --sessions-dir "$(comm_sessions_dir)" >"$out" 2>/dev/null &
  local rpid=$!
  local i=0; while [ ! -S "$rsock" ] && [ $i -lt 30 ]; do sleep 0.1; i=$((i+1)); done
  [ -S "$rsock" ] || { kill "$rpid" 2>/dev/null; rm -f "$out"; die "reply listener failed to start"; }

  local coached="$question

$(comm_coach_text "$asker" "$rsock")"

  case "$lane" in
    claude)
      log "ask -> $name [claude] (reply listener: $asker)"
      python3 "$CC_PEER_PY" send --to "$sock" --text "$coached" --from "$rsock" --name "$asker" \
        || { kill "$rpid" 2>/dev/null; rm -f "$out"; die "delivery failed"; };;
    codex)
      log "ask -> $name [codex session $thread] (reply listener: $asker)"
      codex_queue local "$thread" -- "$coached" >/dev/null \
        || { kill "$rpid" 2>/dev/null; rm -f "$out"; die "codex queue failed"; };;
  esac

  local rc=0; wait "$rpid" || rc=$?
  if [ "$rc" = 0 ] && [ -s "$out" ]; then
    cat "$out"; echo
    rm -f "$out"; return 0
  fi
  rm -f "$out"
  log "no reply within ${timeout}s — the question WAS delivered; the answer may arrive later (codex: check that session's UI/history)"
  return 2
}
