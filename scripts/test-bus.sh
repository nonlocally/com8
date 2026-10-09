#!/usr/bin/env bash
# Complete bus verification using temporary state, fake sessions, local sockets,
# and a locally generated/trusted TLS certificate. No external network, live
# agent, account credentials, user config, or public gateway is used.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for tool in python3 node openssl; do
  command -v "$tool" >/dev/null 2>&1 || {
    printf 'test-bus: required executable not found: %s\n' "$tool" >&2
    exit 127
  }
done
export PYTHONDONTWRITEBYTECODE=1
python3 "$ROOT/scripts/test-bus-broker.py"
python3 "$ROOT/scripts/test-bus-conversations.py"
python3 "$ROOT/scripts/test-bus-gateway.py"
python3 "$ROOT/scripts/test-bus-viewers.py"
python3 "$ROOT/scripts/test-bus-chat.py"
python3 "$ROOT/scripts/test-bus-graph.py"
python3 "$ROOT/scripts/test-bus-sso.py"
python3 "$ROOT/scripts/test-bus-identity.py"
python3 "$ROOT/scripts/test-bus-ownership.py"
python3 "$ROOT/scripts/test-bus-events.py"
python3 "$ROOT/scripts/test-bus-service-compat.py"
python3 "$ROOT/scripts/test-bus-service.py"
python3 "$ROOT/scripts/test-bus-client.py"
python3 "$ROOT/scripts/test-bus-chat-client.py"
python3 "$ROOT/scripts/test-bus-rehome.py"
python3 "$ROOT/scripts/test-bus-lifecycle.py"
python3 "$ROOT/scripts/test-bus-remote-fixture.py"
node "$ROOT/scripts/test-bus-ui.js"
printf 'PASS: complete bus suite (broker, existing-session client, verified TLS, dashboard)\n'
