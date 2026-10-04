#!/usr/bin/env bash
# npm-face first-run smoke: the non-interactive `setup -y` path a stranger's
# unattended install takes — claim ceremony via --handle, measured self-test,
# doctor's new checks — against a fully isolated env (own HOME so the orphan/
# plist checks never read the real machine). Covers the cli.ts logic the
# plist unit test cannot (review finding: setup()/doctor() had no coverage).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$HERE/packages/com8"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-npx.XXXXXX)"
export COMM_STATE="$T/state"
export COMMUNICATE_DATA="$T/data"
export CODEX_HOME="$T/codex"
export CLAUDE_CONFIG_DIR="$T/claude"
export COM8_SOCK_DIR="$T/socks"
export COM8_SESSIONS_DIR="$T/sessions"
export COM8_SELF="npxhost"
export COM8_TICK=1
unset COM8_SOCK COM8_DAEMON_DIR CLAUDE_CODE_MESSAGING_SOCKET
FAKEHOME="$T/home"
mkdir -p "$COM8_SESSIONS_DIR" "$FAKEHOME"

pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup() { "$COMM" com8 stop >/dev/null 2>&1 || true; rm -rf "$T"; }
trap cleanup EXIT

echo "== build the package (vendored kernel = this checkout)"
rm -f "$PKG/dist/cli.js"   # a stale dist must never masquerade as a build
if ( cd "$PKG" && npm run build >/dev/null 2>&1 ) && [ -f "$PKG/dist/cli.js" ]; then
  ok "dist/cli.js built (build exit 0)"
else bad "dist/cli.js built"; exit 1; fi

echo "== plist unit (P1/P2 pins)"
if ( cd "$PKG" && node test/plist-unit.mjs >/dev/null 2>&1 ); then
  ok "plist unit green"
else bad "plist unit green"; fi

echo "== non-interactive setup -y: ceremony + measured self-test"
out="$(HOME="$FAKEHOME" node "$PKG/dist/cli.js" setup -y --handle npxsmoke --no-mcp --no-persist 2>&1)"
rc=$?
if [ $rc -eq 0 ]; then ok "setup -y exits 0"; else bad "setup -y exits 0 (rc=$rc: $out)"; fi
if printf '%s' "$out" | grep -q "claimed @npxsmoke"; then
  ok "handle claimed non-interactively"
else bad "handle claimed (got: $out)"; fi
if printf '%s' "$out" | grep -q "self-test MEASURED"; then
  ok "self-test ran and measured"
else bad "self-test measured"; fi
if printf '%s' "$out" | grep -q "daemon version"; then
  ok "version gate reported"
else bad "version gate reported"; fi
if printf '%s' "$out" | grep -q "com8 pair\|com8 connect"; then
  ok "next steps name pair/connect (they exist now)"
else bad "next steps name pair/connect"; fi
u="$COMM_STATE/com8/user.json"
if [ -f "$u" ] && grep -q '"npxsmoke"' "$u" && grep -q '"npx-setup"' "$u"; then
  ok "user.json written with claimed_via=npx-setup"
else bad "user.json written by the ceremony"; fi

echo "== setup is idempotent for an already-claimed user"
out="$(HOME="$FAKEHOME" node "$PKG/dist/cli.js" setup -y --no-mcp --no-persist 2>&1)"
if [ $? -eq 0 ] && printf '%s' "$out" | grep -q "user: @npxsmoke"; then
  ok "second setup keeps the claim (no re-prompt, no overwrite)"
else bad "second setup keeps the claim (got: $out)"; fi

echo "== doctor: the new checks"
dout="$(HOME="$FAKEHOME" node "$PKG/dist/cli.js" doctor 2>&1)"
drc=$?
if [ $drc -eq 0 ]; then ok "doctor exits 0"; else bad "doctor exits 0 (got: $dout)"; fi
if printf '%s' "$dout" | grep -q "daemon release parity.*current"; then
  ok "doctor: version parity"
else bad "doctor: version parity (got: $dout)"; fi
if printf '%s' "$dout" | grep -q "user claimed.*@npxsmoke"; then
  ok "doctor: user claimed"
else bad "doctor: user claimed"; fi
if ! printf '%s' "$dout" | grep -q "orphaned legacy state"; then
  ok "doctor: no orphan under the isolated HOME"
else bad "doctor: orphan check"; fi

echo "== doctor detects an orphaned split-brain root (report, never delete)"
mkdir -p "$FAKEHOME/.local/state/com8/mail/stranded"
dout="$(HOME="$FAKEHOME" node "$PKG/dist/cli.js" doctor 2>&1)"
if [ $? -ne 0 ] && printf '%s' "$dout" | grep -q "orphaned legacy state"; then
  ok "orphan reported as a failure"
else bad "orphan reported (got: $dout)"; fi
if [ -d "$FAKEHOME/.local/state/com8/mail/stranded" ]; then
  ok "orphan left untouched (mail may be stranded there)"
else bad "doctor deleted the orphan"; fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
