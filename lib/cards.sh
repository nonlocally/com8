# shellcheck shell=bash
# communicate :: cards — self-described capability, the layer above titles.
# A card says what an agent IS and what to ASK IT FOR; other agents read cards
# to choose whom to message. Stored in communicate's own state dir, keyed by
# sessionId so a card survives resumes and hex-name churn. Standalone (no com8
# daemon); the eventual unification meets com8's card axis halfway.

_cards_dir() { printf '%s/cards\n' "$COMM_STATE"; }

# Resolve <name|self> -> "<key>\t<display-name>"; key = sessionId else name:<name>
_card_key() {
  local target="$1"
  python3 - "$(comm_sessions_dir)" "${CLAUDE_CODE_MESSAGING_SOCKET:-}" "$target" <<'PY'
import glob, json, os, sys
sessions_dir, mysock, target = sys.argv[1:4]
best = None
for f in glob.glob(os.path.join(sessions_dir, "*.json")):
    try: r = json.load(open(f))
    except Exception: continue
    if target == "self":
        if mysock and r.get("messagingSocketPath") == mysock: best = r; break
    elif r.get("name") == target:
        if best is None or r.get("startedAt", 0) > best.get("startedAt", 0): best = r
if best is None:
    if target == "self":
        sys.stderr.write("cannot resolve 'self' — no $CLAUDE_CODE_MESSAGING_SOCKET session found\n")
        raise SystemExit(3)
    sys.stderr.write("no agent named '%s' (see: communicate agents)\n" % target)
    raise SystemExit(3)
sid = best.get("sessionId") or ""
key = sid if sid else "name:" + best.get("name", target)
sys.stdout.write("%s\t%s" % (key.replace("/", "_"), best.get("name", target)))
PY
}

cards_cmd() {
  comm_need_python
  local sub="${1:-}"; shift || true
  case "$sub" in
    set)
      local target="${1:-}"; shift || true
      [ -n "$target" ] || die "usage: communicate card set <name|self> [--what TEXT] [--ask-me-for TEXT]"
      local what="" ask="" have=0
      while [ $# -gt 0 ]; do
        case "$1" in
          --what) what="$2"; have=1; shift 2;;
          --ask-me-for) ask="$2"; have=1; shift 2;;
          *) die "unknown flag: $1";;
        esac
      done
      [ "$have" = 1 ] || die "nothing to set (--what and/or --ask-me-for)"
      local kd; kd="$(_card_key "$target")" || exit 3
      local key="${kd%%$'\t'*}" disp="${kd#*$'\t'}"
      mkdir -p "$(_cards_dir)"
      python3 - "$(_cards_dir)/$key.json" "$what" "$ask" "$disp" <<'PY'
import json, os, sys, time
path, what, ask, disp = sys.argv[1:5]
d = {}
if os.path.exists(path):
    try: d = json.load(open(path))
    except Exception: d = {}
if what: d["what"] = what
if ask: d["ask_me_for"] = ask
d["updated"] = int(time.time()); d["name_at_set"] = disp
json.dump(d, open(path, "w"), indent=2)
PY
      ok "card set for '$disp' (key: $key)";;
    show)
      local target="${1:-self}"
      local kd; kd="$(_card_key "$target")" || exit 3
      local key="${kd%%$'\t'*}"
      if [ -f "$(_cards_dir)/$key.json" ]; then cat "$(_cards_dir)/$key.json"
      else log "no card for '$target'"; return 1; fi;;
    clear)
      local target="${1:-}"
      [ -n "$target" ] || die "usage: communicate card clear <name|self>"
      local kd; kd="$(_card_key "$target")" || exit 3
      local key="${kd%%$'\t'*}"
      rm -f "$(_cards_dir)/$key.json" && ok "card cleared";;
    *) die "usage: communicate card {set|show|clear} <name|self> [--what TEXT] [--ask-me-for TEXT]";;
  esac
}
