# communicate Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `@aadarwal/communicate` — an npx-installable package whose `setup` wires a dual-ecosystem plugin (Claude Code + Codex) teaching agents the communicate layer, with the bash CLI on PATH and a minimal MCP face.

**Architecture:** One in-repo plugin payload (`plugins/communicate/`, sibling `.claude-plugin`/`.codex-plugin` manifests over shared skills/bin/.mcp.json) vendored into a zero-build ESM npm package (`packages/communicate/`) whose node bin dispatches `setup|doctor|serve|version` and exec's the vendored bash CLI for everything else. Installer stabilizes the payload under `~/.local/share/communicate/` and registers it: two settings.json keys (Claude), `codex plugin` CLI (Codex).

**Tech Stack:** bash + python3 (existing CLI, unchanged), Node ≥20 ESM (no build step), `@modelcontextprotocol/sdk` + `zod` (serve only), jq for validation.

**Spec:** docs/superpowers/specs/2026-08-31-communicate-dist-design.md

## Global Constraints

- **No com8**: package/plugin never vendor `lib/com8.py`, `lib/com8.sh`, `lib/com8_seat.py`, never import `packages/com8`; packaged `communicate com8` prints a not-bundled notice.
- Node `>=20`, `"type": "module"`, deps exactly `@modelcontextprotocol/sdk ^1.30.0` + `zod ^3.25.0`, **no build step** (ship `src/` as-is).
- Version `0.1.0` everywhere (strict semver — Codex validates).
- SKILL.md frontmatter: `name` + `description` ONLY; dir name == `name`.
- No hooks anywhere (Codex manifest `hooks` key is contested — ship none).
- `~/.claude/settings.json`: read-modify-write with timestamped backup; never touch `known_marketplaces.json`/`installed_plugins.json`. Never hand-edit `~/.codex/config.toml` — use `codex` CLI or print lines.
- Platform: macOS/Linux; requirements bash, python3, ssh — stated, not hidden.
- Every commit carries the three co-author trailers (Claude, Codex, Com8).

---

### Task 1: Make com8 libs optional in the CLI

**Files:**
- Modify: `bin/communicate` (source block ~lines 50-60; `com8` dispatch arm)

**Interfaces:**
- Produces: a `bin/communicate` that runs identically when `lib/com8.sh` exists, and degrades `communicate com8 …` to exit 1 + notice when it does not. Vendored trees (Task 5) rely on this.

- [ ] **Step 1: Write the failing test** (inline, throwaway): copy repo to `/tmp/cq-nocom8`, delete `lib/com8*.{sh,py}` there, run `COMM_HOME=/tmp/cq-nocom8 /tmp/cq-nocom8/bin/communicate agents`; expect today: hard failure at `source lib/com8.sh`.
- [ ] **Step 2: Edit `bin/communicate`**: change the unconditional `source "$COMM_HOME/lib/com8.sh"` to
```bash
# com8 plane is optional in packaged installs (@aadarwal/communicate ships without it)
[ -f "$COMM_HOME/lib/com8.sh" ] && . "$COMM_HOME/lib/com8.sh"
```
and guard the dispatch arm:
```bash
com8)
  command -v com8_cmd >/dev/null 2>&1 || type com8_cmd >/dev/null 2>&1 || \
    die "com8 plane not bundled in this install — clone github.com/aadarwal/communicate for the full CLI"
  com8_cmd "$@";;
```
(match the actual function name used by the existing arm — read it first; keep behavior identical when present).
- [ ] **Step 3: Re-run Step 1 probe** — `agents` works com8-less; `com8 status` dies with the notice; full-repo `communicate agents` unchanged.
- [ ] **Step 4: `bash -n bin/communicate` and commit** `feat(bin): com8 libs optional so packaged installs run without the com8 plane`.

### Task 2: Plugin + marketplace manifests (4 JSON files)

**Files:**
- Create: `plugins/.claude-plugin/marketplace.json`, `plugins/communicate/.claude-plugin/plugin.json`, `plugins/communicate/.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`

**Interfaces:**
- Produces: marketplace name `communicate`, plugin name `communicate`, enable id `communicate@communicate`. Claude marketplace root = `plugins/`; Codex marketplace root = repo root (`.agents/…` with source `./plugins/communicate`). Installer (Task 8) points at the stabilized copies of these same roots.

- [ ] **Step 1: Write the four files** — exact content:

`plugins/.claude-plugin/marketplace.json`
```json
{
  "name": "communicate",
  "owner": { "name": "Aadarsh Agarwal", "url": "https://github.com/aadarwal" },
  "metadata": { "description": "An agent router: identity + address for every AI coding agent, routed over sockets and ssh.", "version": "0.1.0" },
  "plugins": [ { "name": "communicate", "source": "./communicate", "description": "Talk to any agent by name — Claude sessions, Codex sessions, across devices. Teaches the bus: seeing agents, messaging, renaming, joining." } ]
}
```

`plugins/communicate/.claude-plugin/plugin.json`
```json
{
  "name": "communicate",
  "version": "0.1.0",
  "description": "Agent-to-agent communication: route messages to any Claude or Codex agent by name, rename sessions, join the bus, bridge devices over ssh, wake agents on triggers.",
  "author": { "name": "Aadarsh Agarwal", "url": "https://github.com/aadarwal" },
  "license": "MIT",
  "keywords": ["agents", "messaging", "routing", "codex", "cross-session"]
}
```

`plugins/communicate/.codex-plugin/plugin.json`
```json
{
  "name": "communicate",
  "version": "0.1.0",
  "description": "Agent-to-agent communication: route messages to any Claude or Codex agent by name, rename sessions, join the bus, bridge devices over ssh, wake agents on triggers.",
  "author": { "name": "Aadarsh Agarwal", "url": "https://github.com/aadarwal" },
  "repository": "https://github.com/aadarwal/communicate",
  "license": "MIT",
  "keywords": ["agents", "messaging", "routing", "mcp", "cross-session"],
  "skills": "./skills/",
  "mcpServers": "./.mcp.json",
  "interface": {
    "displayName": "Communicate",
    "shortDescription": "Message any AI coding agent by name, locally or across devices",
    "developerName": "Aadarsh Agarwal",
    "category": "Productivity",
    "capabilities": ["Interactive", "Write"],
    "websiteURL": "https://github.com/aadarwal/communicate"
  }
}
```

`.agents/plugins/marketplace.json`
```json
{
  "name": "communicate",
  "interface": { "displayName": "Communicate" },
  "plugins": [ {
    "name": "communicate",
    "source": { "source": "local", "path": "./plugins/communicate" },
    "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
    "category": "Productivity"
  } ]
}
```
- [ ] **Step 2: Validate** — `jq -e .name` on all four; `jq -e '.plugins[0].source'` on both marketplaces.
- [ ] **Step 3: Commit** `feat(plugin): dual-ecosystem manifests + marketplaces for the communicate plugin`.

### Task 3: The five skills + wire-protocol reference + /agents command

**Files:**
- Create: `plugins/communicate/skills/{communicate,communicate-identity,communicate-codex,communicate-fleet,communicate-wake}/SKILL.md`, `plugins/communicate/skills/communicate/references/wire-protocol.md`, `plugins/communicate/commands/agents.md`

**Interfaces:**
- Produces: skill names == dir names; every description opens with what it is then "Use when …" trigger phrases; bodies reference the `communicate` CLI exactly as shipped (Task 4 shim guarantees PATH in Claude; bodies carry the Codex fallback path `~/.local/share/communicate/current/vendor/bin/communicate`).

- [ ] **Step 1: Author `skills/communicate/SKILL.md`** (core). Frontmatter name `communicate`; description: router orientation + triggers ("use when asked to talk to / message / reach / list / coordinate with another agent or session, Claude or Codex, on this or another machine"). Body MUST cover, with exact commands: the bus model (sidecar `name → socket`); `communicate agents` and native ListAgents; `communicate route <name> "<msg>"` / `communicate send <name|socket> [--as NAME] "<msg>"`; prefer native SendMessage from inside Claude (attested mode ⇒ fewer holds); the LANE TABLE (target Claude session → route/SendMessage; existing Codex app/TUI session → `codex queue` (async, reply stays there); fresh headless Codex answer → `codex ask` (sync); Codex as a ListAgents peer → `codex peer`); reply addressing (`from` is the reply address; reply to the `from` of a cross-session message); the inbound gate (mode-parity default, `crossSessionInbound: accept|hold|refuse`, held mail expires ~5 min); safety (transport is your ssh; only bridge to trusted ends); pointer to `references/wire-protocol.md`; one line: durable mailboxes/identity live in the com8 plane, not bundled here.
- [ ] **Step 2: Author `skills/communicate-identity/SKILL.md`**. Triggers: rename a session/agent, name yourself, become addressable, join the bus. Body: a name IS the sidecar entry (`~/.claude/sessions/<pid>.json`); `/rename <name>` is the whole namespace — rename yourself with `/rename`; renaming another live session = drive `/rename` in its UI (communicate does not fake it over the socket — a socket-delivered "/rename" is just text); dormant transcripts: append a `custom-title` record (concept + that `com8 retitle` in the full repo automates it); JOIN THE BUS mechanically = (1) numeric-filename sidecar whose `pid` is a live pid, (2) answer the socket within ~250 ms probe, (3) speak newline-JSON user frames — then ListAgents lists you and route/SendMessage reach you; collisions: newest `startedAt` wins deterministically.
- [ ] **Step 3: Author `skills/communicate-codex/SKILL.md`**. Triggers: message/ask/drive a Codex agent or session. Body: the three lanes in depth; `codex queue <device> <session-name|uuid> "<msg>"` — Codex ≥ 0.151, async, addressed by native name from `~/.codex/session_index.jsonl`, success = enqueued, reply stays in the session (verify via its rollout file `~/.codex/sessions/YYYY/MM/DD/rollout-*<thread>.jsonl`); `codex ask <device> [--dir D] [--thread N] [--new] [--auto] [--model M] "<q>"` — sync `codex exec --json`, continuity cached per (device,thread), read-only sandbox default, `--auto` = workspace-write; `codex peer <device> [name]` / `unpeer` — socket adapter, own thread, appears in ListAgents; `codex probe|forget`.
- [ ] **Step 4: Author `skills/communicate-fleet/SKILL.md`**. Triggers: reach an agent on another device/machine, bridge, list remote sessions. Body: `link <device>` (substrate check: tailscale + ssh + codex + sidecar count); `ls [device]`; `claude bridge <device> [name|pid|newest]` — MUST run inside a Claude session (needs `$CLAUDE_CODE_MESSAGING_SOCKET` as return address), mirrors sockets over `ssh -L/-R` so the remote session appears native (`claude*` in agents); `claude unbridge <device|all>`; `status`; `down` (tears down everything communicate started); trust model: a forwarded socket is exactly as reachable as the ssh login carrying it.
- [ ] **Step 5: Author `skills/communicate-wake/SKILL.md`**. Triggers: wake/nudge/poll an agent on a schedule or event. Body: a message IS a wake (inbound resumes an idle session); `wake <name> --every SEC [--message MSG] [--times N]`; `wake <name> --on-pr owner/repo [--catchup]` (needs authenticated `gh`); `wake ls` / `wake stop <name|all>`; when to prefer native `SendMessage … notify_when_idle` (inside Claude, one-shot) vs wake (recurring/event).
- [ ] **Step 6: Author `references/wire-protocol.md`** — the 13-rule digest verbatim from the spec §2 skill contract: frame JSON shape (`{"type":"user","message":{"role":"user","content":"<string>"},"priority":"next","from":"uds:<abs>","msg_id":"<hex>"}`), string-only content, omit session_id, `from` = reply address, leading-wrapper-only attribution (`<cross-session-message from= from-name=>`), receipts only when same dirname, local-only guard (forwarded socket passes), sidecar schema fields, numeric-filename discovery rule, 250 ms liveness probe, measured-vs-reported liveness, socket dir 0700/socket 0600 hygiene.
- [ ] **Step 7: Author `commands/agents.md`**:
```markdown
---
description: Show the agent routing table — every reachable agent by name (local Claude sessions, bridged remotes, Codex peers) with status and socket.
allowed-tools: Bash
---
Run `communicate agents` (fallback: `~/.local/share/communicate/current/vendor/bin/communicate agents`) and render the table for the user, then one line of guidance: message any row with `communicate route <name> "<msg>"` or, from inside Claude Code, SendMessage to the name.
```
- [ ] **Step 8: Lint** — for each SKILL.md: frontmatter has exactly `name` and `description`; `name` equals its directory; grep bodies for the words "route", "queue", "rename", "sidecar" landing in the right files.
- [ ] **Step 9: Commit** `feat(plugin): five skills, wire-protocol reference, /agents command — the teaching layer`.

### Task 4: Plugin bin shim + .mcp.json

**Files:**
- Create: `plugins/communicate/bin/communicate` (mode 755), `plugins/communicate/.mcp.json`

**Interfaces:**
- Consumes: repo CLI at `../../..`/bin (dev checkout) or stabilized payload `~/.local/share/communicate/current/vendor/bin/communicate` (installed).
- Produces: `communicate` on PATH in every Claude session with the plugin enabled; MCP server key `communicate` ⇒ tools `mcp__plugin_communicate_communicate__*`.

- [ ] **Step 1: Write the shim**:
```bash
#!/usr/bin/env bash
# communicate — plugin shim. Prefers the stabilized install, falls back to the
# repo checkout this plugin dir lives in (dev mode), then to PATH.
set -euo pipefail
here="$(cd "$(dirname "$(readlink "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")")" && pwd)"
for c in \
  "${COMMUNICATE_HOME:-}/bin/communicate" \
  "$HOME/.local/share/communicate/current/vendor/bin/communicate" \
  "$here/../../../bin/communicate"; do
  [ -n "$c" ] && [ -x "$c" ] && exec "$c" "$@"
done
echo "communicate: no backing install found (run: npx -y @aadarwal/communicate setup)" >&2; exit 127
```
- [ ] **Step 2: Write `.mcp.json`**:
```json
{ "mcpServers": { "communicate": { "type": "stdio", "command": "npx", "args": ["-y", "@aadarwal/communicate", "serve"] } } }
```
- [ ] **Step 3: Test** — `plugins/communicate/bin/communicate agents` from the repo prints the routing table (falls through to `$here/../../../bin`); `chmod +x` verified via `test -x`.
- [ ] **Step 4: Commit** `feat(plugin): PATH shim + MCP declaration`.

### Task 5: npm package scaffold + vendor script

**Files:**
- Create: `packages/communicate/package.json`, `packages/communicate/scripts/vendor.mjs`, `packages/communicate/.gitignore` (`vendor/`, `node_modules/`, `*.tgz`), `packages/communicate/README.md`

**Interfaces:**
- Produces: `npm run vendor` builds `packages/communicate/vendor/{bin,lib,plugins,.agents,registry,VERSION}` with NO `com8*` files; `prepack` runs it. Tasks 6–8 exec `vendor/bin/communicate` and copy `vendor/` at setup time.

- [ ] **Step 1: `package.json`**:
```json
{
  "name": "@aadarwal/communicate",
  "version": "0.1.0",
  "description": "An agent router: message any Claude or Codex agent by name, locally or across devices. Installs the CLI, skills, and MCP face for Claude Code and Codex.",
  "type": "module",
  "license": "MIT",
  "repository": { "type": "git", "url": "https://github.com/aadarwal/communicate.git", "directory": "packages/communicate" },
  "bin": { "communicate": "src/cli.mjs" },
  "files": ["src", "vendor", "README.md"],
  "engines": { "node": ">=20" },
  "os": ["darwin", "linux"],
  "scripts": { "vendor": "node scripts/vendor.mjs", "prepack": "node scripts/vendor.mjs", "test": "node test/mcp-smoke.mjs && node test/setup-smoke.mjs" },
  "dependencies": { "@modelcontextprotocol/sdk": "^1.30.0", "zod": "^3.25.0" }
}
```
(`files: ["vendor"]` is safe because vendor.mjs is the allowlist — it only ever copies what Step 2 names.)
- [ ] **Step 2: `scripts/vendor.mjs`** — node ESM; from repo root (`../../` of the script): rm -rf + recreate `vendor/`; copy `bin/communicate` → `vendor/bin/`; copy `lib/*.sh` EXCEPT `com8.sh` and `lib/cc_peer.py` (assert: no file matching `/com8/` lands; throw if one would); copy `plugins/` → `vendor/plugins/`; copy `.agents/` → `vendor/.agents/`; copy `registry/` → `vendor/registry/`; write `vendor/VERSION` = package.json version + git short sha; chmod 755 the executables.
- [ ] **Step 3: Run + verify** — `node scripts/vendor.mjs`; assert `vendor/lib/com8.sh` absent, `vendor/lib/codex.sh` present, `COMM_HOME=$PWD/vendor vendor/bin/communicate agents` prints the table and `vendor/bin/communicate com8 status` prints the not-bundled notice (proves Task 1 in the packaged shape).
- [ ] **Step 4: README.md** — install one-liner (`npx -y @aadarwal/communicate setup`), what setup writes (exact keys/paths), requirements (bash, python3, ssh; macOS/Linux), uninstall, pointer to repo for the com8 plane.
- [ ] **Step 5: Commit** `feat(pkg): @aadarwal/communicate scaffold + com8-free vendor step`.

### Task 6: CLI dispatcher `src/cli.mjs`

**Files:**
- Create: `packages/communicate/src/cli.mjs`

**Interfaces:**
- Consumes: `vendor/bin/communicate` (sibling of `src/` via `new URL('../vendor/', import.meta.url)`).
- Produces: `communicate version|setup|doctor|serve|<anything>`; exports nothing (bin only). `setup`/`doctor` implemented in Task 8 (`./setup.mjs` — `runSetup(args)`, `runDoctor()`), `serve` in Task 7 (`./serve.mjs` — `runServe()`); until then cli.mjs lazy-imports and a missing module errors cleanly.

- [ ] **Step 1: Write it**:
```js
#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";

const pkgDir = fileURLToPath(new URL("..", import.meta.url));
const vendorCli = path.join(pkgDir, "vendor", "bin", "communicate");
const [cmd, ...rest] = process.argv.slice(2);

const version = () => {
  const v = JSON.parse(readFileSync(path.join(pkgDir, "package.json"), "utf8")).version;
  const stamp = existsSync(path.join(pkgDir, "vendor", "VERSION"))
    ? readFileSync(path.join(pkgDir, "vendor", "VERSION"), "utf8").trim() : "unvendored";
  console.log(`@aadarwal/communicate ${v} (payload ${stamp})`);
};

switch (cmd) {
  case undefined: case "help": case "--help": version(); console.log("verbs: setup | doctor | serve | version | <any communicate CLI verb>"); break;
  case "version": case "--version": version(); break;
  case "setup": (await import("./setup.mjs")).runSetup(rest); break;
  case "doctor": (await import("./setup.mjs")).runDoctor(); break;
  case "serve": (await import("./serve.mjs")).runServe(); break;
  default: {
    if (!existsSync(vendorCli)) { console.error("payload missing — reinstall @aadarwal/communicate"); process.exit(127); }
    const env = { ...process.env, COMM_HOME: path.join(pkgDir, "vendor") };
    const r = spawnSync(vendorCli, [cmd, ...rest], { stdio: "inherit", env });
    process.exit(r.status ?? 1);
  }
}
```
(Confirm against `bin/communicate`: it derives `COMM_HOME` itself from its own path — the env override is belt-and-braces; keep only if the script honors `COMM_HOME`, else drop.)
- [ ] **Step 2: Test** — `node src/cli.mjs version` prints both stamps; `node src/cli.mjs agents` prints the routing table via vendor.
- [ ] **Step 3: Commit** `feat(pkg): cli dispatcher — node verbs + bash passthrough`.

### Task 7: MCP server `src/serve.mjs` + smoke test

**Files:**
- Create: `packages/communicate/src/serve.mjs`, `packages/communicate/test/mcp-smoke.mjs`

**Interfaces:**
- Consumes: vendored CLI as Task 6.
- Produces: `runServe()` — stdio MCP server, name `communicate`, version from package.json, **fixed append-only** tool array: `agents_list{}`, `whereis{name}`, `route{name,message}`, `send{target,message,as?}`, `codex_queue{device,session,message}`, `codex_ask{device,message,dir?,thread?,fresh?,auto?}`, `status{}`.

- [ ] **Step 1: Write serve.mjs** — pattern per tool (full file, ~140 lines):
```js
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const vendorCli = path.join(fileURLToPath(new URL("..", import.meta.url)), "vendor", "bin", "communicate");
const run = (args) => execFileSync(vendorCli, args, { encoding: "utf8", timeout: 120000, maxBuffer: 8 * 1024 * 1024 });
const text = (s) => ({ content: [{ type: "text", text: s }] });
const fail = (e) => ({ content: [{ type: "text", text: String(e.stdout || e.message || e) }], isError: true });

// APPEND ONLY — order is the tools/list contract.
const TOOLS = [
  { name: "agents_list", desc: "Routing table: every reachable agent (local Claude sessions, bridged remotes, Codex peers) with status and socket.", schema: {}, argv: () => ["agents"] },
  { name: "whereis", desc: "Resolve an agent name to type/via/socket.", schema: { name: z.string() }, argv: (a) => ["whereis", a.name] },
  { name: "route", desc: "Send a message to any agent by name (Claude or Codex, local or bridged). Reply returns to the caller's socket when one exists.", schema: { name: z.string(), message: z.string() }, argv: (a) => ["route", a.name, a.message] },
  { name: "send", desc: "Raw-inject one message into a peer socket or named peer, with optional from-name attribution.", schema: { target: z.string(), message: z.string(), as: z.string().optional() }, argv: (a) => a.as ? ["send", a.target, "--as", a.as, "--", a.message] : ["send", a.target, "--", a.message] },
  { name: "codex_queue", desc: "Enqueue a turn into an EXISTING Codex session by native name/UUID (async; reply stays in that session). Codex >= 0.151.", schema: { device: z.string(), session: z.string(), message: z.string() }, argv: (a) => ["codex", "queue", a.device, a.session, "--", a.message] },
  { name: "codex_ask", desc: "Ask a headless Codex agent synchronously (codex exec, remembered thread continuity, read-only sandbox unless auto).", schema: { device: z.string(), message: z.string(), dir: z.string().optional(), thread: z.string().optional(), fresh: z.boolean().optional(), auto: z.boolean().optional() }, argv: (a) => ["codex", "ask", a.device, ...(a.dir ? ["--dir", a.dir] : []), ...(a.thread ? ["--thread", a.thread] : []), ...(a.fresh ? ["--new"] : []), ...(a.auto ? ["--auto"] : []), "--", a.message] },
  { name: "status", desc: "Active bridges, codex peers, and wakes started by communicate on this machine.", schema: {}, argv: () => ["status"] },
];

export async function runServe() {
  const server = new McpServer({ name: "communicate", version: "0.1.0" });
  for (const t of TOOLS) server.registerTool(t.name, { description: t.desc, inputSchema: t.schema },
    async (args) => { try { return text(run(t.argv(args ?? {}))); } catch (e) { return fail(e); } });
  await server.connect(new StdioServerTransport());
}
```
- [ ] **Step 2: Write `test/mcp-smoke.mjs`** — spawn `node src/cli.mjs serve`, speak JSON-RPC over stdio: `initialize`, `tools/list` (assert exactly the 7 names in order), `tools/call agents_list` (assert result text contains `NAME` header or `no reachable agents`); kill child; exit nonzero on any mismatch.
- [ ] **Step 3: `npm install` then run** `node test/mcp-smoke.mjs` → PASS.
- [ ] **Step 4: Commit** `feat(pkg): minimal MCP face — 7 tools, 1:1 CLI wrap + stdio smoke test`.

### Task 8: setup / doctor `src/setup.mjs` + sandboxed test

**Files:**
- Create: `packages/communicate/src/setup.mjs`, `packages/communicate/test/setup-smoke.mjs`

**Interfaces:**
- Consumes: `vendor/` payload.
- Produces: `runSetup(argv: string[])`, `runDoctor()`. Flags: `--claude`, `--codex`, `--dry-run`, `--uninstall`, `--purge`, `-y`. Honors `$HOME` (tests sandbox it) and `$COMMUNICATE_DATA` override for the payload root.

- [ ] **Step 1: Write setup.mjs** with exactly this behavior:
  - paths: data root `${COMMUNICATE_DATA:-$HOME/.local/share/communicate}`; payload `<root>/<version>`; symlink `<root>/current` (atomic: mkdtemp copy → rename; re-point symlink).
  - stabilize(): recursive-copy `vendor/` → payload; also copy `src/` + `package.json` beside it so `current/vendor/bin/communicate` AND future `serve` resolve without npx cache.
  - stabilize() also REWRITES the stabilized copy's `vendor/plugins/communicate/.mcp.json` to `{"command":"node","args":["<current>/src/cli.mjs","serve"]}` — the installed plugin must not depend on the npm registry (repo copy stays npx-canonical for the published artifact).
  - claude(): read `~/.claude/settings.json` (missing ⇒ `{}`; unparsable ⇒ abort with message, no write); write `settings.json.communicate-backup-<epoch>`; deep-merge ONLY `extraKnownMarketplaces.communicate = { source: { source: "directory", path: "<current>/vendor/plugins" } }` and `enabledPlugins["communicate@communicate"] = true`; write pretty 2-space JSON.
  - codex(): if `codex` on PATH → `spawnSync codex plugin marketplace add <current>/vendor` then `codex plugin add communicate@communicate` (surface stdout; nonzero ⇒ print the manual lines, don't abort); else print manual lines incl. `codex mcp add communicate -- npx -y @aadarwal/communicate serve`. Always remind: new Codex thread required.
  - `--dry-run`: print every path/JSON key/command that WOULD run, write nothing.
  - `--uninstall`: remove the two settings keys (backup first), print `codex plugin remove` + `codex plugin marketplace remove` lines (run them if codex present), leave payload unless `--purge`.
  - runDoctor(): table — payload present + VERSION, `current` target, settings keys present?, codex registered? (`codex plugin list` grep), bash/python3/ssh/gh on PATH.
- [ ] **Step 2: Write `test/setup-smoke.mjs`** — mkdtemp fake `$HOME` with a settings.json containing a sentinel key + existing enabledPlugins entry; run setup with `HOME=<tmp>`, `--claude` only (codex covered by dry-run assertion); assert: backup file exists; sentinel + pre-existing plugin entry survived; the two new keys exact; payload + `current` symlink exist; then `--uninstall` restores (keys gone, sentinel intact); `--dry-run` writes nothing (dir mtime/inode set unchanged).
- [ ] **Step 3: Run** `node test/setup-smoke.mjs` → PASS.
- [ ] **Step 4: Commit** `feat(pkg): setup/doctor — stabilize payload, register both ecosystems, reversible`.

### Task 9: Root AGENTS.md + CLAUDE.md stub

**Files:**
- Create: `AGENTS.md`, `CLAUDE.md`

**Interfaces:**
- Produces: repo briefing single-sourced; Codex reads AGENTS.md natively, Claude reads CLAUDE.md → `@AGENTS.md`.

- [ ] **Step 1: Write `AGENTS.md`** (~60 lines): what this repo is (agent router; name → socket); the three planes and where com8's boundary is; dev commands (`scripts/test-*.sh`; `npm --prefix packages/communicate run vendor|test`); THE OPERATING GUIDE FOR AGENTS IN THIS REPO — see who's here (`communicate agents`), talk (`route`, native SendMessage), codex lanes incl. queue, rename = `/rename`, join-the-bus mechanics one-paragraph, PATH fallback line for non-Claude harnesses (`~/.local/share/communicate/current/vendor/bin/communicate`); style rules (bash: shellcheck-clean, die() on misuse; python: stdlib only; no secrets in repo).
- [ ] **Step 2: Write `CLAUDE.md`**:
```markdown
# CLAUDE.md
The repository briefing is single-sourced in `AGENTS.md`, so Claude Code and
Codex read the same instructions and neither can drift.

@AGENTS.md
```
- [ ] **Step 3: Commit** `docs: single-sourced repo briefing (AGENTS.md + CLAUDE.md stub)`.

### Task 10: End-to-end verification + dist test script

**Files:**
- Create: `scripts/test-communicate-dist.sh`
- Modify: `README.md` (Install section gains the npx path)

**Interfaces:**
- Consumes: everything above.
- Produces: one command proving the distribution: manifests valid, tarball installable, CLI + MCP + setup work from the packed artifact.

- [ ] **Step 1: Write `scripts/test-communicate-dist.sh`** (set -uo pipefail; each check prints ok/FAIL, exit nonzero on any FAIL):
  1. jq validity of the 4 manifests; SKILL.md frontmatter lint (exactly name+description; dir==name) via a python one-liner;
  2. Codex validator if present: `python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/communicate` (skip+warn if absent);
  3. `npm --prefix packages/communicate run vendor` then assert no `vendor/lib/com8*`;
  4. `npm pack` → install tarball into `$(mktemp -d)` via `npm install --prefix`; run `<tmp>/node_modules/.bin/communicate version` and `… agents`;
  5. `HOME=$(mktemp -d) <tmp>/node_modules/.bin/communicate setup --dry-run` prints plan, writes nothing;
  6. `node packages/communicate/test/mcp-smoke.mjs`; `node packages/communicate/test/setup-smoke.mjs`; `scripts/test-codex-queue.sh`.
- [ ] **Step 2: Run it** → all green.
- [ ] **Step 3: README Install section** — add: `npx -y @aadarwal/communicate setup` (both ecosystems), what it writes, uninstall line; keep git-clone path.
- [ ] **Step 4: Commit** `test(dist): end-to-end distribution check + README install path`.

### Task 11: Real-machine install (this Mac) + live proof

**Files:** none (operates on `~/.claude/settings.json`, `~/.local/share/communicate/`, codex registration)

- [ ] **Step 1:** `node packages/communicate/src/cli.mjs setup --claude` (real run; backup asserted). Verify keys with jq; verify `~/.local/share/communicate/current/vendor/bin/communicate agents` works.
- [ ] **Step 2:** Codex side: attempt `codex plugin marketplace add` + `codex plugin add`; if the CLI refuses headless, fall back to printing lines + verify `codex mcp add`-free path by confirming skills reachable via plugin OR record exact manual step for the user.
- [ ] **Step 3:** Live proof of the taught behavior from the INSTALLED artifact: `~/.local/share/communicate/current/vendor/bin/communicate codex queue local codex-in-app "<marker>"` → read rollout reply; `… route say-hi-to-me-2 "<marker>"` (gate now accept ⇒ delivered).
- [ ] **Step 4:** `runDoctor()` output pasted into the final report; note restart caveats (new Claude session / new Codex thread see the plugin).
- [ ] **Step 5: Commit** nothing (no repo changes) — report instead.
