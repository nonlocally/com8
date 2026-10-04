#!/usr/bin/env bash
# Retained core regression gate. Every runtime resource is isolated by its fixture.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for tool in python3 node npm tmux jq openssl; do
  command -v "$tool" >/dev/null 2>&1 || {
    printf 'UNQUALIFIED: required test dependency missing: %s\n' "$tool" >&2
    exit 127
  }
done
export PYTHONDONTWRITEBYTECODE=1
python3 "$ROOT/scripts/test-com8-entrypoint.py"
python3 -B "$ROOT/scripts/test-stat-portability.py"
python3 "$ROOT/scripts/test-legacy-service-guard.py"
python3 "$ROOT/scripts/test-com8-interface.py"
python3 "$ROOT/scripts/test-model-connections.py"
python3 "$ROOT/scripts/test-com8-seat-unit.py"
python3 "$ROOT/scripts/test-com8-control-token.py"
python3 "$ROOT/scripts/test-provider-qualification.py"
python3 "$ROOT/scripts/test-client-restoration-harness.py"
python3 "$ROOT/scripts/test-bus-bind.py"
python3 "$ROOT/scripts/test-account-boundaries.py"
bash "$ROOT/scripts/test-account-module.sh"
bash "$ROOT/scripts/test-native-fixtures.sh"
bash "$ROOT/scripts/test-com8-adopt.sh"
for suite in com8-seat-menu com8-core com8-seat com8-spawn com8-ask com8-link com8-seat-link com8-pair com8-connect; do
  bash "$ROOT/scripts/test-$suite.sh"
done
bash "$ROOT/scripts/test-bus.sh"
node "$ROOT/scripts/test-com8-mcp-interface.mjs"
npm --prefix "$ROOT/packages/communicate" test
printf 'PASS: retained core, namespace, seat, bus and package regressions\n'
