// plist.ts — pure rendering of the launchd unit, split out so its two shipped
// bugs stay pinned by a unit test: COMM_STATE must strip exactly ONE level off
// stateRoot() (state root = $COMM_STATE/com8 — stripping two orphaned the
// daemon under ~/.local/state/com8 while every client looked in
// ~/.local/state/communicate/com8), and PATH must be baked because launchd's
// default PATH hides homebrew tmux/claude/codex from seat/spawn.
import path from "node:path";
import { stateRoot } from "./kernel.js";

export const LAUNCHD_LABEL = "com.communicate.com8";
// Mirrors the repo installer (lib/com8.sh): seats invoke bare `tmux`, spawn
// invokes bare `claude`/`codex` — under launchd those exist only through PATH.
export const LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin";

export function renderPlist(python: string, daemon: string, self: string): string {
  // Mirror the repo installer's env preservation (lib/com8.sh): a user with
  // COM8_SOCK_DIR / COM8_SESSIONS_DIR exported must not get a daemon that
  // binds sockets and plants sidecars in dirs their clients never look at.
  const extra =
    (process.env.COM8_SOCK_DIR
      ? `\n    <key>COM8_SOCK_DIR</key><string>${process.env.COM8_SOCK_DIR}</string>`
      : "") +
    (process.env.COM8_SESSIONS_DIR
      ? `\n    <key>COM8_SESSIONS_DIR</key><string>${process.env.COM8_SESSIONS_DIR}</string>`
      : "");
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>${python}</string><string>${daemon}</string><string>daemon</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>COMM_STATE</key><string>${process.env.COMM_STATE || path.dirname(stateRoot())}</string>
    <key>COM8_SELF</key><string>${self}</string>
    <key>PATH</key><string>${LAUNCHD_PATH}</string>${extra}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>${path.join(stateRoot(), "daemon.log")}</string>
  <key>StandardOutPath</key><string>${path.join(stateRoot(), "daemon.log")}</string>
</dict></plist>`;
}
