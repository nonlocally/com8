#!/usr/bin/env bash
# Opt-in Claude Code UserPromptSubmit filter.
# It is not installed by setup. Point a hook at this file if you want a prompt
# containing @com8::bus:session to be copied to those registered sessions.
#
# The filter reads stdin, so the prompt is not placed on the command line.
# It always exits 0: a missed mention must not block the host prompt.
# Codex can use the same command from a wrapper:
#   printf '%s' "$PROMPT" | com8 mentions route --json
#   exec codex "$@"
set -euo pipefail

bin="${COM8_BIN:-}"
if [ -z "$bin" ]; then
  if command -v com8 >/dev/null 2>&1; then
    bin=com8
  else
    bin=communicate
  fi
fi

input="$(cat)"
prompt="$(printf '%s' "$input" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except ValueError:
    data = None
if isinstance(data, dict):
    for key in ("prompt", "user_prompt", "userPrompt"):
        value = data.get(key)
        if isinstance(value, str):
            sys.stdout.write(value)
            raise SystemExit
    sys.stdout.write(raw)
else:
    sys.stdout.write(raw)
')"

if [ -z "$prompt" ]; then
  exit 0
fi

result="$(printf '%s' "$prompt" | "$bin" mentions route --json 2>/dev/null || true)"
printf '%s' "$result" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except ValueError:
    raise SystemExit
sent = ["%s:%s" % (row.get("bus"), row.get("name"))
        for row in data.get("deliveries") or [] if row.get("sent")]
codes = [err.get("code") for err in data.get("errors") or [] if isinstance(err, dict)]
if sent or codes:
    sys.stderr.write("com8 mentions: delivered %s; errors %s\n" % (
        ", ".join(sent) or "none", ", ".join(codes) or "none"))
'
exit 0
