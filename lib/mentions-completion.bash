# Opt-in completion for @com8:: mentions. Source this file; it does not edit shell rc.
# Suggestions come from `mentions complete`, which reads the buses and sessions
# this device is already allowed to see.

_com8_mentions_bin() {
  if command -v com8 >/dev/null 2>&1; then
    printf '%s\n' com8
  else
    printf '%s\n' communicate
  fi
}

_com8_mentions() {
  local cur bin line
  cur="${COMP_WORDS[COMP_CWORD]}"
  case "$cur" in
    *com8::*|@com8*|com8*)
      bin="$(_com8_mentions_bin)"
      COMPREPLY=()
      while IFS= read -r line; do
        [ -n "$line" ] && COMPREPLY+=("$line")
      done < <("$bin" mentions complete --text -- "$cur" 2>/dev/null)
      ;;
  esac
}

complete -F _com8_mentions communicate com8
