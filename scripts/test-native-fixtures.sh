#!/usr/bin/env bash
# Native routing fixtures without provider credentials, SSH, or shared sockets.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d /tmp/com8-native.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT
mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/run" "$TEST_ROOT/bin" "$TEST_ROOT/codex"
chmod 700 "$TEST_ROOT/run"

# Each fixture supplies its own fake provider/SSH command where necessary.
# Any accidental fallback to a real provider, service manager or host fails.
for command in claude codex ssh tailscale launchctl systemctl; do
  cat > "$TEST_ROOT/bin/$command" <<'BLOCK'
#!/usr/bin/env bash
printf 'UNQUALIFIED: fixture attempted a real external integration: %s\n' "${0##*/}" >&2
exit 97
BLOCK
  chmod +x "$TEST_ROOT/bin/$command"
done

unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDECODE CODEX_THREAD_ID CODEX_SESSION_ID
unset COMM_CODEX_INDEX COMM_CODEX_PATH COMM_HOME COM8_PACKAGE_CLI BASH_ENV ENV
unset ANTHROPIC_API_KEY OPENAI_API_KEY
for suite in cards roster-desc codex-queue ask; do
  env HOME="$TEST_ROOT/home" PATH="$TEST_ROOT/bin:$PATH" \
    XDG_RUNTIME_DIR="$TEST_ROOT/run" XDG_STATE_HOME="$TEST_ROOT/state" \
    XDG_CONFIG_HOME="$TEST_ROOT/config" COMM_STATE="$TEST_ROOT/state/communicate" \
    COMMUNICATE_DATA="$TEST_ROOT/data" CLAUDE_CONFIG_DIR="$TEST_ROOT/claude" \
    CODEX_HOME="$TEST_ROOT/codex" \
    bash "$ROOT/scripts/test-$suite.sh"
done
printf 'PASS: isolated native cards, roster, Codex queue and socket reply fixtures\n'
