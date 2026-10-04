# communicate distribution: dual-ecosystem plugin + npx installer

Date: 2026-08-31 · Status: approved (design) · Branch: `communicate-dist` (origin/main + rescues)

## Goal

Make the **communicate layer** installable and self-teaching for both Claude Code
and Codex: one `npx @aadarwal/communicate setup` gives any agent on a machine
the CLI on PATH, skills that teach how to see/talk/rename/join the bus, and an
optional MCP face — with zero manual wiring.

## Non-goals (explicit)

- **No com8.** The durable-mailbox plane (`communicate com8 *`, `lib/com8.py`,
  `packages/com8`) is not packaged, not depended on, and not taught. The npm
  vendor list excludes `lib/com8*.py` and `lib/com8.sh`; `communicate com8`
  in the packaged CLI prints a one-line "not bundled — see repo" notice.
  Unification is a later, separate goal.
- No switchboard merge. No npm-registry publish (tarball built + verified;
  `npm publish` is the operator's act).
- No Windows support (CLI is bash+python3+ssh; stated honestly).

## Already done on this branch (rescue)

- `62f09d2` codex queue re-fitted onto main (+ test, passing).
- `de5a83f`, `931845f` peer-bridge commits cherry-picked from stranded local `main`.

## 1. Repo-side prep

- Make com8 libs **optional at source time** in `bin/communicate`:
  `[ -f "$COMM_HOME/lib/com8.sh" ] && source …`; the `com8` dispatch guards on
  the function existing, else dies with "com8 plane not bundled in this install;
  clone the repo". Repo behavior unchanged (files present ⇒ identical).
- Root `AGENTS.md`: single-sourced orientation (condensed from the core skill).
- Root `CLAUDE.md`: 3-line stub that `@AGENTS.md`-imports it (proven pattern).

## 2. Plugin source of truth: `plugins/communicate/`

```
plugins/communicate/
  .claude-plugin/plugin.json    # name/version/description/author/license/keywords
  .codex-plugin/plugin.json     # + strict semver, skills:"./skills/",
                                #   mcpServers:"./.mcp.json", interface{...}; NO hooks key
  skills/communicate/SKILL.md
  skills/communicate/references/wire-protocol.md
  skills/communicate-identity/SKILL.md
  skills/communicate-codex/SKILL.md
  skills/communicate-fleet/SKILL.md
  skills/communicate-wake/SKILL.md
  commands/agents.md            # /agents — render the routing table
  bin/communicate               # shim → resolves installed payload (Claude auto-PATHs this)
  .mcp.json                     # {"communicate":{"type":"stdio","command":"npx",
                                #   "args":["-y","@aadarwal/communicate","serve"]}}
```

Marketplace files: `plugins/.claude-plugin/marketplace.json` (marketplace name
`communicate`, plugin source `./communicate`) and `.agents/plugins/marketplace.json`
at the payload root for Codex (policy: AVAILABLE / ON_INSTALL, category set).

### Skill contract

Frontmatter is `name` + `description` ONLY; the description carries trigger
phrases ("use when the user asks to talk to / message / rename / wake another
agent…"). Bodies teach with exact commands and the judgment layer:

- **communicate** (core): the bus model (name → socket sidecars); see who's here
  (`communicate agents`, native ListAgents); talk (`route`/`send`; prefer native
  SendMessage from inside Claude — replies attest mode); the **lane table**
  (Claude socket / codex queue / codex ask / codex peer — when each); reply
  addressing; inbound-gate semantics (`crossSessionInbound`, mode parity, ~5 min
  held-mail expiry); safety model (ssh-only transport, who can reach a socket).
- **communicate-identity**: a name IS the sidecar entry; `/rename` is the
  namespace; rename another session (live: drive `/rename`; dormant: transcript
  retitle concept); **join the bus** = numeric-named sidecar + live pid +
  answering socket + newline-JSON; collision resolution (newest wins).
- **communicate-codex**: three lanes in depth; queue needs Codex ≥ 0.151, is
  async, replies stay in the target session (read rollout to verify); ask =
  sync `codex exec` with per-(device,thread) continuity, read-only sandbox by
  default, `--auto` opts into writes; peer = socket adapter, its own thread.
- **communicate-fleet**: `link` (substrate check), `ls`, `claude bridge`
  (must run inside a Claude session; forwarded sockets look local — that is the
  design), `unbridge`, `status`, `down`; trust = your ssh keys.
- **communicate-wake**: timer + `--on-pr` triggers; a message IS the wake;
  `wake ls/stop`; needs authenticated `gh` for PR mode.
- **references/wire-protocol.md**: the 13-rule digest (frame JSON, string-only
  content, omit session_id, `from` = reply address, leading-wrapper-only
  attribution, receipts same-dir rule, local-only guard, sidecar schema,
  numeric-filename rule, liveness probe, measured-not-reported).

## 3. npm package: `packages/communicate/`

`@aadarwal/communicate` 0.1.0 · ESM, node ≥ 20 · deps: `@modelcontextprotocol/sdk`, `zod`.
**No build step**: plain-JS `src/` shipped as-is.

- `bin.communicate → src/cli.mjs`, a dispatcher:
  - `setup` / `doctor` / `serve` / `version` handled in node;
  - anything else → `exec` the vendored bash CLI (`vendor/bin/communicate`).
- `scripts/vendor.mjs` (prepack): copies `bin/communicate`, `lib/*.sh`,
  `lib/cc_peer.py`, `lib/directory.sh` deps, `plugins/` payload, `registry/`
  → `vendor/`; **excludes `lib/com8*`**; stamps `vendor/VERSION`.
- `files`: `["src", "vendor/bin/*", "vendor/lib/*.sh", "vendor/lib/cc_peer.py",
  "vendor/plugins", "vendor/VERSION", "README.md"]` — tight allowlist, no leaks.

### `setup [--claude] [--codex] [--dry-run] [--uninstall] [-y]` (default: both)

1. Stabilize payload → `~/.local/share/communicate/<version>/` + `current` symlink
   (npx cache is ephemeral; directory marketplaces are used in place).
2. **Claude**: read-modify-write `~/.claude/settings.json` (timestamped backup
   first): `extraKnownMarketplaces.communicate = {source:{source:"directory",
   path:<current>/plugins}}`, `enabledPlugins["communicate@communicate"]=true`.
   Never touch `known_marketplaces.json` / `installed_plugins.json` (CLI-owned).
3. **Codex**: if `codex` on PATH → `codex plugin marketplace add <current>` +
   `codex plugin add communicate@communicate`; else print those lines plus the
   `codex mcp add communicate -- npx -y @aadarwal/communicate serve` fallback.
   Never hand-edit `config.toml`. Remind: new Codex thread required.
4. Print doctor table; `--uninstall` reverses (settings keys out, codex plugin
   remove lines, payload left unless `--purge`).

### `serve` — minimal MCP face (~7 tools, 1:1 CLI wrap, own code)

`agents_list`, `whereis`, `route`, `send`, `codex_queue`, `codex_ask`, `status`.
Fixed append-only tool array (byte-stable `tools/list`). Each handler spawns the
vendored CLI and returns stdout; `isError` on nonzero exit. Server key
`communicate` ⇒ Claude tools namespace `mcp__plugin_communicate_communicate__*`.
No `${*_PLUGIN_ROOT}` anywhere (the npx launcher sidesteps the dual-root hazard).

## 4. Verification

- `bash -n` all vendored shell; existing `scripts/test-codex-queue.sh` stays green.
- Manifest validation: run Codex's bundled
  `~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py` against
  `plugins/communicate`; jq structural checks on all four manifest/marketplace files.
- `npm pack` → install tarball into a temp prefix → `communicate agents` and
  `communicate version` run; `setup --dry-run` prints the exact writes;
  MCP smoke: drive `serve` over stdio with a scripted client (initialize +
  tools/list + one `agents_list` call).
- New `scripts/test-communicate-dist.sh` wraps the above.

## 5. Risks / uncertainties (from local-contract research)

- Claude `plugin.json` `mcpServers` key unproven locally ⇒ we rely on root
  `.mcp.json` + convention dirs (proven twice on this machine).
- Codex reading `.claude-plugin/marketplace.json` is observed-works, not
  contract ⇒ we ship `.agents/plugins/marketplace.json` too.
- Codex manifest `hooks` key contradiction ⇒ we ship no hooks at all.
- `settings.json` is concurrently written by live CLIs ⇒ merge-write + backup.

## Out of scope / later

com8 unification (single bus story), switchboard console, registry growth,
Windows, npm publish, MCP coverage beyond the 7 core tools.
