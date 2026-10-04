#!/usr/bin/env bash
# Distribution acceptance: build + pack the npm package, install the TARBALL into
# a throwaway project (as a downloader would), run setup (no launchd), then drive
# the MCP server as a client — proving a message sent through an MCP tool lands
# real mail in the daemon store. Fully isolated: its own COMM_STATE + socket.
set -uo pipefail
command -v node >/dev/null 2>&1 || { echo "skip: node not installed"; exit 0; }
PKG="/Users/aadarwal/src/aadarwal/communicate/packages/com8"
T="$(mktemp -d /tmp/com8-mcp.XXXXXX)"
export COMM_STATE="$T/state" COM8_SOCK="$T/state/com8/com8.sock"
export XDG_DATA_HOME="$T/share" COM8_SELF="mcphost" COM8_TICK=1 COM8_NO_PERSIST=1
cleanup(){
  # stop the isolated daemon; it was never installed to launchd (--no-persist)
  node "$T/node_modules/@aadarwal/com8/dist/cli.js" >/dev/null 2>&1 <<< "" || true
  pkill -f "$T/.*com8.py daemon" 2>/dev/null || true
  rm -rf "$T"
}
trap cleanup EXIT

echo "== build + pack"
( cd "$PKG" && npm run build >/dev/null 2>&1 && npm pack >/dev/null 2>&1 ) || { echo "FAIL build/pack"; exit 1; }
TARBALL="$(ls -t "$PKG"/aadarwal-com8-*.tgz | head -1)"
echo "   tarball: $(basename "$TARBALL")"

echo "== fresh install of the tarball"
( cd "$T" && npm init -y >/dev/null 2>&1 && npm install "$TARBALL" >/dev/null 2>&1 ) || { echo "FAIL install"; exit 1; }
CLI="$T/node_modules/@aadarwal/com8/dist/cli.js"
[ -f "$CLI" ] && echo "ok   installed dist/cli.js" || { echo "FAIL no cli.js"; exit 1; }

echo "== setup (no persist) + doctor"
node "$CLI" setup -y --no-persist >/dev/null 2>&1
node "$CLI" doctor 2>&1 | sed 's/^/   /'

echo "== MCP client drives the server"
node "$PKG/test/mcp-smoke.mjs" "$CLI"
rc=$?

node "$CLI" >/dev/null 2>&1 <<< "" || true
COMM_STATE="$T/state" COM8_SOCK="$T/state/com8/com8.sock" python3 "$PKG/vendor/com8.py" call stop >/dev/null 2>&1 || true
exit $rc
