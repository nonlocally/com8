# com8 distribution + MCP server — design (buildable)

Date: 2026-08-16 · Verified now: npm `com8` is TAKEN (dead package) → scoped name required;
`@modelcontextprotocol/sdk` latest = 1.30.0 (no 2026-07-28 beta dist-tag on npm yet); node 26,
python3 3.14; `claude mcp add` (stdio + `--transport http`) and `codex mcp add` both present.

## Package shape — one npm package, two payloads, "two faces one kernel"
A thin TS MCP server (ZERO fabric logic — each tool = one JSON line to the daemon socket) + the
vendored stdlib-only Python kernel.
```
packages/com8/  (published as @aadarwal/com8; registry name io.github.aadarwal/com8)
├── package.json          # name @aadarwal/com8, mcpName io.github.aadarwal/com8, bin com8→dist/cli.js
├── src/  cli.ts server.ts kernel.ts stdio.ts http.ts
├── dist/                 # tsc output (ships)
├── vendor/ com8.py cc_peer.py com8_seat.py VERSION   # copied from lib/ at build; stdlib only, no pip
└── templates/ com.communicate.com8.plist.tmpl  communicate-com8.service.tmpl
```
deps: `@modelcontextprotocol/sdk ^1.30.0`, `zod ^3.25`. engines node>=20.

**Node→daemon (kernel.ts):** socket path mirrors com8.py state_root() exactly
(`${COMM_STATE|XDG_STATE_HOME|~/.local/state}/communicate/com8/com8.sock`; override COM8_SOCK).
Call = net.createConnection, write one JSON line, shutdown write, read to first \n, parse (10s;
long-poll ops timeout_s+5). Python = COM8_PYTHON→python3→/usr/bin/python3 (require ≥3.9). Daemon
file = COM8_DAEMON_DIR → ~/.local/share/com8/daemon/current/com8.py → the package's own vendor/
(for a bare npx before setup). Autostart on ECONNREFUSED (spawn `python3 <daemon> daemon`
detached; the daemon's control-socket-answers singleton test makes a launchd race harmless).
CRITICAL: state root / sockdir / sidecar / launchd label are IDENTICAL to the git-checkout com8 —
a repo-managed and npm-managed com8 are two faces of ONE per-device daemon, never two daemons.

## `npx @aadarwal/com8 setup` (idempotent, -y non-interactive)
1. Preflight: resolve python3 (--python), platform darwin|linux, COMM_STATE creatable; warn if
   no tailscale (selfname falls back to hostname; links/HTTP degrade).
2. Stabilize the vendor: copy vendor/* → ~/.local/share/com8/daemon/<version>/ (temp+rename),
   repoint .../current symlink (npx cache is ephemeral; launchd needs a stable path). Keep old
   versions for rollback.
3. Respect an incumbent: probe control socket with {op:status}; if a daemon answers, print it and
   SKIP persistence unless --force (a repo-managed com8 may own this device — never double-bind).
4. Install persistence: render template ProgramArguments=[abs python3, .../current/com8.py,
   daemon]; env COMM_STATE + COM8_SELF (resolved once via `com8.py selfname` — install-time bake,
   same as com8.sh); label com.communicate.com8 (SAME label — one daemon slot per device).
   macOS bootout+bootstrap; Linux systemd --user enable --now; wait ≤10s for socket.
5. --claim <name> optional → durable address immediately.
6. Print client install lines:
   `claude mcp add com8 -- npx -y @aadarwal/com8 serve`
   `codex  mcp add com8 -- npx -y @aadarwal/com8 serve`
   tailnet HTTP: `com8 serve --http` then `claude mcp add --transport http com8-<dev> http://<ts-ip>:7433/mcp`
`com8 doctor` = checks only; `com8 uninstall` = bootout/disable+remove unit (never touches mail;
--purge removes daemon copies, state stays). Exit: 0 ok / 2 python missing / 3 conflict / 4 fail.

## MCP tool surface (each tool = 1:1 projection onto a daemon op; [new]=Loop-1 daemon work)
Descriptions STEER: messaging is default; seats are the explicit interactive escape hatch.
agents_list (roster+liveness; "start here; prefer send/ask") · whoami · send (default way to talk;
durable) · ask [long-poll] · inbox_read · wait_for_message [long-poll; your inbound wake when your
runtime has no socket push — Claude gets native wake] · claim/release · adopt · group_send ·
seat_ls/spawn/send/read/state/wait/respond/bind (ESCAPE HATCH — cluster shells/REPLs/TUIs, not
agent talk) · spawn (headless default; seat only when watching) · consult [long-poll] · status ·
link_status · notify.
Long-poll ops need ONE additive daemon change: the control handler may hold the connection and
reply late (per-name Condition signaled from _store); current _read_line(timeout=2) only bounds
the REQUEST read. [com8 ask already does this — holds the control thread.]
Ordering (2026-07-28 cache rule, backward-benign): tools registered from ONE fixed authored array;
new tools APPEND ONLY → tools/list byte-identical across restarts. Resources: com8://routes
(ttlMs 30000), com8://inbox/<name> (ttlMs 1000; the natural subscriptions/listen target — _store
is the change event), com8://seats (ttlMs 2000). On 1.30/2025-11-25 clients the hints ride _meta
and are ignored (benign).

## Transport
stdio (default local): client spawns node → local unix socket → daemon; autostart covers cold.
Streamable HTTP (tailnet): `com8 serve --http [--port 7433]` binds ONLY the device's tailscale IP
(verify in 100.64.0.0/10; refuse 0.0.0.0 unless --unsafe-bind); SDK transport STATELESS
(sessionIdGenerator undefined) matching 2026-07-28 stateless core. Auth = tailnet identity: per
request `tailscale whois --json <addr>`, reject non-tailnet peers, default fabric `from` to
`mcp:<login>@<device>`. If ever public, spec OAuth/Client-ID-Metadata-Documents slot into http.ts
as middleware — one file, zero tool changes.
**Remote daemons rule:** the Node server NEVER dials a remote daemon socket. (a) default — MCP
server talks to its LOCAL daemon; cross-device (name@device, seat envelopes) rides the postmaster
links (queue/ack/backoff/dead-letter; works when the far device sleeps). (b) thin client — a
device with NO com8 points its MCP client at another device's `com8 serve --http` URL; that server
drives ITS OWN local daemon. One trust boundary, no socket-forwarding machinery.

## Local verify tonight (NO publish — deferred to morning)
```
cd packages/com8 && npm run build && npm pack        # → aadarwal-com8-0.1.0.tgz
T=$(mktemp -d); cd $T; npm init -y; npm install /path/aadarwal-com8-0.1.0.tgz
npx com8 setup --claim demo; npx com8 doctor
claude mcp add com8-local -- node $T/node_modules/@aadarwal/com8/dist/cli.js serve
node .../dist/cli.js serve --http --port 7433 & ; claude mcp add --transport http com8-http http://$(tailscale ip -4):7433/mcp
```
Acceptance: (1) tools/list twice byte-identical; (2) send{to:demo} from a Claude session → line in
mail/demo/inbox.jsonl same msg_id; (3) wait_for_message{demo} unblocks within a tick of a shell
`communicate com8 send demo hi` — both faces drive one kernel; (4) uninstall→setup round-trips.
Deferred (designed): `npm publish --access public` then `mcp-publisher login github && publish`
(Registry metadata-only; npm hosts the artifact; io.github.aadarwal namespace authenticates via GitHub).

## Compat posture
Pin sdk ^1.30.0 (negotiates 2025-11-25 with today's Claude Code + Codex; no 2026-07-28 beta tag
yet). Comply now with backward-benign 2026-07-28 rules (deterministic append-only order,
ttlMs/cacheScope hints, ZERO Sampling/Roots/Logging; server never initiates so MRTR costs nothing;
Node layer fully stateless — dropping initialize/sessions is transport config). server.ts is
transport/version-agnostic; stdio.ts/http.ts own transports; the daemon protocol never sees MCP.
A 2026-07-28 upgrade = SDK bump + enable server/discover + subscriptions/listen (inbox resources
are the ready subscription target) — a version change, not a rewrite.

Build order tonight: scaffold packages/com8 → kernel.ts + stdio.ts + tools whose ops exist today
(status, agents_list as a status reshape, send, claim, release, + a small daemon inbox op) →
setup/doctor → verify. Long-poll ops + seats land with the Loop-1 CLI verbs (A1-A4).
