#compdef communicate com8
# Opt-in completion for @com8:: mentions. Source this file; it does not edit shell rc.

_com8_mentions() {
  local bin
  if (( $+commands[com8] )); then
    bin=com8
  else
    bin=communicate
  fi
  local -a vals
  vals=("${(@f)$("$bin" mentions complete --text -- "$PREFIX" 2>/dev/null)}")
  compadd -Q -a vals
}

compdef _com8_mentions communicate com8
