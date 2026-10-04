# shellcheck shell=bash
# communicate com8 — the com8: durable identities, mailboxes, links.
# Thin verb layer over lib/com8.py (daemon + control-socket client).

COM8_PY="$COMM_HOME/lib/com8.py"

com8_state_dir() { printf '%s/com8' "$COMM_STATE"; }

com8_start() {
  comm_need_python
  if python3 "$COM8_PY" call status >/dev/null 2>&1; then
    die "com8 already running (communicate com8 status)"
  fi
  mkdir -p "$(com8_state_dir)"
  nohup python3 "$COM8_PY" daemon >> "$(com8_state_dir)/daemon.log" 2>&1 &
  disown 2>/dev/null || true
  local i
  for i in $(seq 1 50); do
    python3 "$COM8_PY" call status >/dev/null 2>&1 && { ok "com8 up"; return 0; }
    sleep 0.1
  done
  die "com8 failed to start — see $(com8_state_dir)/daemon.log"
}

com8_cmd() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    start)     com8_start "$@";;
    __daemon)  exec python3 "$COM8_PY" daemon;;
    stop|status|agents|wait|claim|release|describe|send|ask|reply|group|notify|seat|spawn|restart|fan|consult|model|link|unlink|grant|ungrant|grants|card|user|init|pair|connect|board|federate|statepath|premove|depart|arrive|move) python3 "$COM8_PY" call "$sub" "$@";;
    inbox)     com8_inbox "$@";;
    install)   com8_install "$@";;
    uninstall) com8_uninstall "$@";;
    adopt)     com8_adopt "$@";;
    retitle)   python3 "$COM8_PY" retitle "$@";;
    *) die "usage: communicate com8 {start|stop|status|agents|init|user|claim [--boxed]|release|describe|send|ask|reply|wait|group|notify|inbox|seat|spawn|restart|fan|consult|move|link|unlink|grant|ungrant|grants|card|pair|connect|board|federate|install|uninstall|adopt|retitle} ...";;
  esac
}

# Live pane adoption and remote-device adoption share the kernel CLI.
# /rename uses the existing seat driver and verifies the selected session.
com8_adopt() {
  python3 "$COM8_PY" call adopt "$@"
}

# Persistence belongs to the package installer's ownership ledger. Keep the
# historical verbs as explicit refusals: their old fixed-label implementation
# could replace another installation's service, even with an isolated HOME.
# In particular, daemon-only uninstall must never silently remove client
# integrations through the combined `com8 uninstall` command.
com8_install() {
  printf '%s\n' "communicate: legacy daemon install is disabled; use 'com8 setup --no-clients --service' for ownership-aware persistence (preview with --dry-run)" >&2
  return 1
}

com8_uninstall() {
  printf '%s\n' "communicate: legacy daemon uninstall is disabled; inspect 'com8 doctor', then preview 'com8 uninstall --dry-run'. Managed uninstall also detaches owned client integrations; it preserves identity state. Existing unowned legacy units require explicit migration." >&2
  return 1
}

# Files-as-API: the inbox is a plain JSONL file; reading it needs no daemon.
com8_inbox() {
  local name="${1:-}"; [ -n "$name" ] || die "usage: communicate com8 inbox <name>"
  local f; f="$(com8_state_dir)/mail/$name/inbox.jsonl"
  [ -f "$f" ] || die "no mailbox for '$name' ($f)"
  cat "$f"
}
