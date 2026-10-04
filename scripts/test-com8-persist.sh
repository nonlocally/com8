#!/usr/bin/env bash
# MANUAL launchd persistence test (macOS). Installs a TEST LaunchAgent under
# an isolated label + state dir, kill -9s the daemon, asserts KeepAlive
# resurrects it, then uninstalls. Touches launchd for real — run by hand.
set -uo pipefail
[ "$(uname -s)" = "Darwin" ] || { echo "skip: launchd test is macOS-only"; exit 0; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-persist.XXXXXX)"
export COMM_STATE="$T/state"
export COM8_SOCK_DIR="$T/socks"
export COM8_SESSIONS_DIR="$T/sessions"
export COM8_SELF="persistest"
export COM8_LABEL="com.communicate.com8-test"
mkdir -p "$COM8_SESSIONS_DIR"
COM8S="$COMM_STATE/com8"

pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup() { "$COMM" com8 uninstall >/dev/null 2>&1 || true; rm -rf "$T"; }
trap cleanup EXIT

echo "== install (isolated label: $COM8_LABEL)"
if "$COMM" com8 install >/dev/null 2>&1; then ok "pm install"; else bad "pm install"; fi
if "$COMM" com8 status >/dev/null 2>&1; then ok "managed daemon answers"; else bad "managed daemon answers"; fi

echo "== KeepAlive: kill -9 must not be fatal"
PID1="$(cat "$COM8S/daemon.pid" 2>/dev/null)"
[ -n "$PID1" ] || bad "read pid"
kill -9 "$PID1" 2>/dev/null
deadline=$((SECONDS + 15)); revived=0
while [ $SECONDS -lt $deadline ]; do
  PID2="$(cat "$COM8S/daemon.pid" 2>/dev/null)"
  if [ -n "$PID2" ] && [ "$PID2" != "$PID1" ] && "$COMM" com8 status >/dev/null 2>&1; then
    revived=1; break
  fi
  sleep 0.5
done
if [ "$revived" = "1" ]; then ok "launchd respawned the daemon (pid $PID1 -> $PID2)"; else bad "launchd respawned the daemon"; fi

echo "== QoS: a LaunchAgent with no ProcessType is spawned as a DAEMON, which is"
echo "   BACKGROUND QoS, and EVERY process it spawns inherits the throttle. No"
echo "   error, no log line — work just takes several times longer. Measured on"
echo "   this machine: identical CPU-bound work 0.34s at default, 1.23-1.70s"
echo "   under background QoS."
PL="$HOME/Library/LaunchAgents/$COM8_LABEL.plist"
if grep -q "ProcessType" "$PL" 2>/dev/null; then
  ok "the generated plist declares a ProcessType"
else bad "the generated plist declares a ProcessType (silent background throttle)"; fi
if grep -A1 "ProcessType" "$PL" 2>/dev/null | grep -qi "Interactive"; then
  ok "ProcessType is Interactive"
else bad "ProcessType is Interactive"; fi
# The plist is what we WROTE; launchd is what actually runs. Ask launchd.
if launchctl print "gui/$(id -u)/$COM8_LABEL" 2>/dev/null | grep -q "spawn type = interactive"; then
  ok "launchd agrees: spawn type = interactive"
else
  st="$(launchctl print "gui/$(id -u)/$COM8_LABEL" 2>/dev/null | grep -o "spawn type = [a-z]*" | head -1)"
  bad "launchd reports ${st:-no spawn type} (the plist is not what runs)"
fi

echo "== uninstall"
if "$COMM" com8 uninstall >/dev/null 2>&1; then ok "pm uninstall"; else bad "pm uninstall"; fi
sleep 1.5
if "$COMM" com8 status >/dev/null 2>&1; then bad "gone after uninstall"; else ok "gone after uninstall"; fi
if [ ! -f "$HOME/Library/LaunchAgents/$COM8_LABEL.plist" ]; then ok "plist removed"; else bad "plist removed"; fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
