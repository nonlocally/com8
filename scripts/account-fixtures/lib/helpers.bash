# shellcheck shell=bash
# ==============================================================================
# Shared fixtures for anu bash tests: repo paths, temp dirs, hermetic git,
# and command stubs (tmux et al.) so functions that shell out can be tested
# without a live tmux server, ssh, or network.
# ==============================================================================

# Root is explicit so donor fixtures cannot fall back to a live Anu install.
ANU_ROOT="${COM8_TEST_ROOT:?isolated fixture runner required}"
export ANU_ROOT
# --- temp dirs, auto-cleaned on exit -----------------------------------------
declare -a _ANU_TMP=()
mktmp() {
  local d
  d=$(mktemp -d "${TMPDIR:-/tmp}/anu-test.XXXXXX")
  # Resolve symlinks (macOS $TMPDIR is /var → /private/var) so paths match what
  # `git rev-parse` and `pwd -P` report from inside the fixture.
  d=$(cd "$d" && pwd -P)
  _ANU_TMP+=("$d")
  printf '%s' "$d"
}
_anu_cleanup() { local d; for d in "${_ANU_TMP[@]:-}"; do [[ -n "$d" ]] && rm -rf "$d"; done; }
trap _anu_cleanup EXIT

# --- command stubs -----------------------------------------------------------
# new_stubdir : a dir prepended to PATH where fake executables live.
new_stubdir() {
  local d; d=$(mktmp)
  mkdir -p "$d/bin"
  printf '%s/bin' "$d"
}

# stub <stubdir> <name> <body> : write an executable <name> running <body>.
# The body sees the stubbed command's args as "$@".
stub() {
  local dir="$1" name="$2" body="$3"
  { printf '#!/usr/bin/env bash\n'; printf '%s\n' "$body"; } > "$dir/$name"
  chmod +x "$dir/$name"
}

