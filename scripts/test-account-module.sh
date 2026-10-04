#!/usr/bin/env bash
# Donor account/observer regressions use only disposable state and command stubs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for tool in bash python3 jq perl shasum column; do
  command -v "$tool" >/dev/null || { echo "UNQUALIFIED: account fixture dependency missing: $tool" >&2; exit 127; }
done
TEST_ROOT="$(mktemp -d /tmp/com8-accounts.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT
mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/bin" "$TEST_ROOT/fixtures/bash"
cp -R "$ROOT/scripts/account-fixtures/lib" "$TEST_ROOT/fixtures/"
for command in claude codex ssh curl tmux launchctl systemctl security secret-tool op infisical; do
  cat > "$TEST_ROOT/bin/$command" <<'BLOCK'
#!/usr/bin/env bash
printf 'UNQUALIFIED: fixture attempted a real integration: %s\n' "${0##*/}" >&2
exit 97
BLOCK
  chmod +x "$TEST_ROOT/bin/$command"
done
python3 - "$ROOT" "$TEST_ROOT" <<'PY'
from pathlib import Path
import sys
root, temp = map(Path, sys.argv[1:])
for path in (root / 'scripts/account-fixtures/bash').glob('*.sh'):
    text = path.read_text().replace('/@ACCOUNT_LAUNCH@ launch/', 'index($0, "@ACCOUNT_LAUNCH@ launch")')
    text = text.replace('@ACCOUNT_LAUNCH@', str(root / 'profiles/runtime/accounts/account'))
    (temp / 'fixtures/bash' / path.name).write_text(text)
PY
# Keep old account variables available in the module for explicit migration,
# but remove the developer's inherited values from these isolated tests.
while IFS= read -r name; do
  case "$name" in ANU_*|COM8_*|CLAUDE_*|CODEX_*|ANTHROPIC_*|OPENAI_*) unset "$name" ;; esac
done < <(compgen -e)
unset BASH_ENV ENV TMUX TMUX_PANE
export HOME="$TEST_ROOT/home" TMPDIR="$TEST_ROOT" COM8_TEST_ROOT="$ROOT"
export XDG_CONFIG_HOME="$HOME/.config" XDG_STATE_HOME="$HOME/.local/state"
export XDG_RUNTIME_DIR="$TEST_ROOT/run" PATH="$TEST_ROOT/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -m 700 "$XDG_RUNTIME_DIR"
for suite in "${@:-account_test.sh account_codex_test.sh pane_test.sh}"; do
  for file in $suite; do bash "$TEST_ROOT/fixtures/bash/$file"; done
done
