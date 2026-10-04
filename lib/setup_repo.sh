# shellcheck shell=bash
# communicate :: setup-repo — register THIS checkout as the plugin for
# Claude Code and Codex. The repo-haver's install: no npm, no vendor, no
# frozen CLI copy; after `git pull`, rerun setup-repo to refresh plugin caches.
# The npm package's `setup` remains
# the no-repo/npx path (frozen payload + MCP deps).

_sr_settings() { printf '%s/settings.json' "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; }

# A shared launcher pointer avoids relying on different plugin-root expansion
# syntaxes in Claude and Codex. It is local installer state, not plugin source.
_sr_repo_pointer() {
  local mode="$1" dry="$2"
  python3 - "$mode" "$dry" "$COMM_HOME" "${COMMUNICATE_DATA:-$HOME/.local/share/communicate}" <<'PY'
import os, sys, tempfile
mode, dry, repo, root = sys.argv[1:]
dest = os.path.join(root, "repo-path")
if mode == "keep":
    print("keeping shared checkout MCP path for other plugin clients; setup-repo --uninstall removes it")
    sys.exit(0)
if dry == "1":
    print("[dry-run] would " + ("record checkout MCP path in " if mode == "install" else "remove matching checkout MCP path from ") + dest)
elif mode == "install":
    os.makedirs(root, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".repo-path-", dir=root)
    with os.fdopen(fd, "w") as f:
        f.write(repo + "\n")
    os.replace(tmp, dest)
else:
    try:
        with open(dest) as f:
            matches = f.read().rstrip("\n") == repo
        if matches:
            os.unlink(dest)
    except FileNotFoundError:
        pass
PY
}

# Merge (or remove) the two Claude keys. python3 owns the JSON: backup first,
# merge-not-clobber, refuse an unparsable file.
_sr_claude() {
  local mode="$1" dry="$2" market_root="$COMM_HOME/plugins"
  python3 - "$mode" "$dry" "$market_root" "$(_sr_settings)" <<'PY'
import json, os, shutil, sys, time
mode, dry, market_root, sp = sys.argv[1:5]
dry = dry == "1"
s = {}
if os.path.exists(sp):
    try:
        s = json.load(open(sp))
    except Exception:
        sys.stderr.write(f"refusing to touch unparsable {sp} - fix it first\n"); sys.exit(1)
prev = (s.get("extraKnownMarketplaces") or {}).get("communicate", {}).get("source", {}).get("path")
if mode == "install":
    s.setdefault("extraKnownMarketplaces", {})["communicate"] = {
        "source": {"source": "directory", "path": market_root}}
    s.setdefault("enabledPlugins", {})["communicate@communicate"] = True
    why = f"point marketplace 'communicate' at {market_root} + enable communicate@communicate"
else:
    (s.get("extraKnownMarketplaces") or {}).pop("communicate", None)
    (s.get("enabledPlugins") or {}).pop("communicate@communicate", None)
    why = "remove marketplace 'communicate' + communicate@communicate"
if dry:
    print(f"[dry-run] would {why} in {sp}"); sys.exit(0)
os.makedirs(os.path.dirname(sp), exist_ok=True)
if os.path.exists(sp):
    shutil.copy2(sp, f"{sp}.communicate-backup-{int(time.time()*1000)}")
json.dump(s, open(sp, "w"), indent=2); open(sp, "a").write("\n")
print(f"{why} in {sp} (backup written)")
if prev and mode == "install" and prev != market_root:
    print(f"note: marketplace previously pointed at {prev} - switched to this checkout")
PY
}

_sr_run_codex() { # <args...> -> ok/fail, logged
  if codex "$@" >/dev/null 2>&1; then log "codex $* — ok"; return 0
  else return 1; fi
}

# Enabled settings alone do not refresh Claude's versioned plugin cache. Let
# its CLI own installation state, including upgrades from a packaged payload.
_sr_claude_refresh() {
  python3 - "$1" "$COMM_HOME/plugins" <<'PY'
import json, pathlib, shutil, subprocess, sys
dry, root = sys.argv[1:]
commands = [["plugin", "marketplace", "add", root],
            ["plugin", "update", "communicate@communicate", "--scope", "user"]]
if dry == "1":
    for args in commands:
        print("[dry-run] would run: claude " + " ".join(args))
    sys.exit(0)
if not shutil.which("claude"):
    print("Claude CLI not found; after installing it, run communicate setup-repo --claude again.")
    sys.exit(0)
def run(args):
    try:
        return subprocess.run(["claude", *args], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
if not run(commands[0]):
    sys.exit("Claude marketplace registration failed; run communicate setup-repo --claude again.")
if not run(commands[1]) and not run(["plugin", "install", "communicate@communicate", "--scope", "user"]):
    sys.exit("Claude plugin refresh failed; run claude plugin update communicate@communicate --scope user.")
try:
    expected = json.loads((pathlib.Path(root) / "communicate/.claude-plugin/plugin.json").read_text())["version"]
    result = subprocess.run(["claude", "plugin", "list", "--json"], capture_output=True,
                            stdin=subprocess.DEVNULL, timeout=30, check=True)
    installed = json.loads(result.stdout)
    if not any(p.get("id") == "communicate@communicate" and p.get("scope") == "user" and
               p.get("version") == expected for p in installed):
        raise ValueError("cached plugin does not match source")
except (OSError, ValueError, KeyError, subprocess.SubprocessError):
    sys.exit("Claude plugin version verification failed; run claude plugin update communicate@communicate --scope user.")
print("Claude plugin installed/refreshed via CLI; restart Claude Code to load it.")
PY
}

_sr_codex() {
  local mode="$1" dry="$2"
  local -a cmds
  if [ "$mode" = install ]; then
    cmds=("plugin marketplace add $COMM_HOME" "plugin add communicate@communicate")
  else
    cmds=("plugin remove communicate@communicate" "plugin marketplace remove communicate")
  fi
  if ! command -v codex >/dev/null 2>&1; then
    log "codex CLI not found — run these once it is installed:"
    local c; for c in "${cmds[@]}"; do printf '  codex %s\n' "$c" >&2; done
    return 0
  fi
  if [ "$dry" = 1 ]; then
    local c; for c in "${cmds[@]}"; do log "[dry-run] would run: codex $c"; done
    return 0
  fi
  if [ "$mode" = install ]; then
    # A marketplace named 'communicate' may already exist (e.g. the npm-mode
    # frozen payload). Switching modes IS the point of this verb: remove the
    # stale one and retry once, so the checkout actually wins.
    if ! _sr_run_codex plugin marketplace add "$COMM_HOME"; then
      log "marketplace 'communicate' already registered elsewhere — repointing at this checkout"
      _sr_run_codex plugin marketplace remove communicate || true
      _sr_run_codex plugin marketplace add "$COMM_HOME" || \
        log "codex plugin marketplace add $COMM_HOME — still failing; run it manually"
    fi
    _sr_run_codex plugin add communicate@communicate || \
      log "codex plugin add communicate@communicate — failed; run it manually"
    log "Codex: start a NEW thread to see the plugin."
  else
    local c
    for c in "${cmds[@]}"; do
      # shellcheck disable=SC2086
      _sr_run_codex $c || log "codex $c — failed (may not have been installed)"
    done
  fi
  return 0
}

setup_repo() {
  [ ! -f "$COMM_HOME/release.json" ] || die "installed COM8 release: use com8 setup; setup-repo is only for a source checkout"
  local claude=0 codex=0 dry=0 uninstall=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --claude) claude=1;;
      --codex) codex=1;;
      --dry-run) dry=1;;
      --uninstall) uninstall=1;;
      *) die "usage: communicate setup-repo [--claude] [--codex] [--dry-run] [--uninstall]";;
    esac; shift
  done
  [ "$claude" = 0 ] && [ "$codex" = 0 ] && claude=1 codex=1
  comm_need_python
  [ -f "$COMM_HOME/plugins/.claude-plugin/marketplace.json" ] || \
    die "no plugin payload at $COMM_HOME/plugins — is this a communicate checkout?"
  local mode=install; [ "$uninstall" = 1 ] && mode=uninstall
  if [ "$claude" = 1 ]; then
    _sr_claude "$mode" "$dry" || die "claude registration failed"
    [ "$mode" != install ] || _sr_claude_refresh "$dry" || die "claude plugin refresh failed"
  fi
  [ "$codex" = 1 ] && _sr_codex "$mode" "$dry"
  local pointer_mode="$mode"
  if [ "$mode" = uninstall ] && { [ "$claude" != 1 ] || [ "$codex" != 1 ]; }; then
    pointer_mode=keep
  fi
  _sr_repo_pointer "$pointer_mode" "$dry" || die "checkout MCP path registration failed"
  if [ "$mode" = install ] && [ "$dry" != 1 ]; then
    ok "this checkout is registered — after git pull, rerun setup-repo to refresh cached plugins"
    log "MCP uses this checkout too. Install its Node dependencies once:"
    log "  npm --prefix \"$COMM_HOME/packages/communicate\" install"
  fi
}
