# Fabric observations — 2026-08-17 deep study

A full-fabric study made before designing the user-identity layer (@handles):
three parallel readers over the daemon, the distribution path, and the repo's
history; an interview with the live builder session; two adversarial design
reviews. This file is the observation record — problems and inefficiencies
found and **not** fixed here, so they survive the conversation that found
them. Items the @handles work fixes on its way are marked [handles].

## Verified bugs

- **[handles] Installer split-brain state root.** `packages/com8/src/cli.ts:62`
  writes `COMM_STATE = dirname(dirname(stateRoot()))` into the launchd plist —
  strips both `com8` AND `communicate`, so the launchd daemon binds
  `~/.local/state/com8/` while every client looks in
  `~/.local/state/communicate/com8/`. Result: a persistent daemon nobody talks
  to plus a second autostarted daemon in the right place (singleton lock is
  per-state-root); `doctor` reports ok because it reaches the second.
  `StandardErrorPath` in the same plist uses the correct root, so logs and
  state diverge.
- **[handles] npm plist has no PATH.** `cli.ts` bakes only `COMM_STATE` +
  `COM8_SELF`; the repo plist deliberately sets PATH (`lib/com8.sh:118`).
  Under launchd's default PATH, bare `tmux`/`claude`/`codex` are invisible →
  seat/spawn/fan/consult all fail on a package-installed daemon.
- **[handles] No daemon version anywhere.** `build_status` returns no version;
  a `git pull` never restarts the daemon and nothing says it's stale. Live on
  this machine at study time: the running daemon predated the four-axis merge
  — `identities.json`, rewritten the same day, lacked the axes fields the
  checked-out code persists.
- **[handles] Fingerprint pinning is theater.** `key_fp` flows from the card
  (`lib/com8.py:2863`), is persisted (`:1738`), and has **no consumer**;
  `federate accept` displays the card's self-declared `fingerprint` field
  rather than computing one from the pubkey it actually installs; `_ssh_cmd`
  (`:1898-1908`) sets no `StrictHostKeyChecking`/known-hosts pinning. "The
  fingerprint is the identity" is written law the code does not enforce.
- **[handles] Petname symmetry bug.** My outbound fleet socket =
  `peer_inbound_dir/<MY fleet name>.sock` (`:2859`) while my inbound socket
  for them is named by MY petname for them (`link_in_sock :1727`). Mail flows
  only if each side's freely-chosen petname equals the other side's
  `COM8_FLEET`. Mismatch = silent queue-forever.
- **[handles] `federate accept` never prints the counter-card** (design said
  it would): the loop stays open unless the human knows to run `invite` back;
  a one-way link dead-letters B→A mail.
- **[handles] Fleet name is env-derived inside the daemon.**
  `os.environ.get("COM8_FLEET", getpass_user())` at `:1846`/`:1819`, never
  persisted (`fleet.json` designed, never built), and neither launchd nor
  systemd unit passes `COM8_FLEET` — exporting it in your shell does not
  change the card the daemon hands a collaborator; you silently federate as
  `$USER`.
- **Boxed sockets unlinked at shutdown.** `shutdown()` (`:2663-2686`) unlinks
  every identity socket including boxed ones — the exact operation
  `_do_release` (`:1602-1605`) guards against ("never unlink it out from
  under a running box"). A daemon restart strands running boxes.
- **`acquire_singleton` third-retry fall-through.** (`:257-309`) the stale-lock
  path can `rmdir` the lock and exit the loop without re-creating it — daemon
  runs lock-less; `shutdown()` then rmdirs a lock it doesn't own.
- **Duplicated `_resolve_ask_natural` call** (`:1267-1268`) — copy-paste;
  racy if `_drop_ask` lands between the two calls (second call resolves the
  *next* pending ask with the wrong text).
- **`_ssh_run` doesn't catch `TimeoutExpired`** — a hung ssh raises out of
  `_move_run`.
- **Unknown inbox cursor → `[]` forever.** `_inbox_entries` (`:1010-1021`)
  never signals an invalid `after_msg_id`; clients get empty pages and
  permanent `wait` timeouts with no diagnosis.
- **tmux-native seat targets misparsed.** `_seat_target_device` (`:781-785`)
  treats any `:` as `<device>:<pane>`, so `session:1.0` fails as
  "device not linked: session".
- **[handles] Fleet-qualified proxies are second-class.** Stored as
  `name@fleet` (`:2153`) but `_do_inbox`/`_do_wait` reject `@` names
  (`:1024`, `:1033`) — the daemon creates identities it cannot be asked about.
- **[handles] sun_path unguarded.** The ≈104-byte AF_UNIX limit is a verified
  design fact with a designed fallback (`/tmp/com8-$UID`), and no guard
  exists at any `bind()`.

## Security posture notes

- **[handles] Outbound auto-grant is permanent and unscoped** (`:1249-1256`):
  one message from any local agent to a foreign fleet durably grants that
  fleet a return path to the agent. Reachable via the MCP `send` tool — a
  trust mutation on an agent-driven path; a prompt-injected "thank
  @attacker" opens a durable inbound channel.
- **[handles] M-3: unbounded proxy minting.** A granted peer varying `from`
  mints a new proxy (socket + sidecar + thread) per name; no per-fleet cap.
- **Ask auto-claims identities forever.** `_do_ask` (`:698-705`) claims the
  `--from` name (defaults `asker`/`cli`/`mcp`); no GC — typos and defaults
  permanently populate the roster and sessions dir.
- **Linked devices can create identities here** (`:2168-2176`) — by design
  ("local mail never bounces"), but a peer device can populate your identity
  table with invented names.
- **Control token**: single per-device unscoped secret, no rotation;
  non-constant-time compare (`:2569`; `hmac.compare_digest` is the right
  call).
- **Boundary rewrite mutates content.** (`:2158-2160`) any prose matching the
  ask-token shape is rewritten at fleet boundaries; the token belongs in an
  envelope field, not in body text.
- The forward-only ssh grant is **indivisible** on OpenSSH 10.2 (verified) —
  bounded at other layers by design; documented as the accepted boundary.
- Unlocked `self.links` read in `_do_link` (`:1975`) while all other readers
  take `self.mu`.
- Sidecar numbering pid-check is TOCTOU (`:334-361`, self-documented risk).

## Scaling / efficiency

- **Whole-mailbox `readlines()` on every hot path** (`_inbox_lines :485-490`):
  per identity in `build_status` (twice), on every local send, every `wait`
  poll, every boxed reconcile tick. O(total mail) per cycle, no rotation.
- **Status measures serially on the tick thread.** 0.35 s probe/identity +
  ~0.5 s tmux measure/seated identity share the thread that runs
  `reconcile()` (store→wake). ~45-50 seats saturates the 30 s probe gate; a
  hung tmux costs 10 s per seated identity. The single biggest scaling wall.
- **Unbounded in-memory dedup** vs 200-line reseed: `self.seen` grows for the
  daemon's life but restarts forget all but 200 ids — a replay window.
- **No depth/budget/quota anywhere** in the fabric; the MCP server registered
  at user scope means agents spawned by agents inherit the tools — recursion
  is unbounded by construction.
- `class Com8` ≈2,530 lines, 8 locks, hand-documented lock orderings; mail,
  links/federation, and spawn/move are the natural next module extractions
  (workspace/seat extractions set the pattern).

- **Reboot-reattach is designed but not exercised** (flagged by the
  collaborator in #8, 2026-08-19): `test-com8-persist.sh` proves KeepAlive
  kill-respawn — a strict subset of reboot (cold launchd, cleared /tmp, all
  sockets gone are not in the kill path) — and is the MANUAL suite outside
  the automated count. Close with one measured before/after at the next
  natural laptop reboot; never cite it under the broader label meanwhile.

## Ontology / model gaps

- **The four axes do not survive `move`.** `_do_arrive` re-claims with no cwd
  → workspace null, an authored card is destroyed by re-derivation,
  supervision null (the moved agent cannot be restarted). Consequence:
  second-hop moves (A→B→C) skip the workspace gate entirely.
- **`_measure_surface` has no device routing** — every remote seat measures
  `dead`; the roster lies about remote seats and `restart` will never kill a
  remote pane (leaks it).
- **`aliases` has no writer.** Declared (`:1289`), persisted (`:370`),
  move-carried (`:1645`), written by nothing — the registry-law failure mode
  the field's own comment warns about. Populate it or delete it.
- **Multi-fleet-per-person unmodelled** (work vs personal): one `COM8_FLEET`
  per device; petname-keyed grants would mean two disjoint grant sets.
- **npm face prefers the stabilized daemon over the vendored one**
  (`kernel.ts:45-57`): an `npx` upgrade keeps running the old daemon
  indefinitely; no version check between the two. The repo plist hardcodes
  the checkout path (`lib/com8.sh:112-114`); both installers share the
  launchd label and can silently fight.

## Faces / periphery

- **openwebui**: `setup.sh` does not create the admin account or register the
  tool/model its README and spec describe; `dispatch_tool.py` hardcodes the
  repo path in a valve; every webui user shares one com8 identity
  (`caller_name` valve); the old webui secret key remains in git history —
  rotate before the repo goes public.
- **Legacy surfaces await their own migration**: `lib/router.sh`,
  `lib/peer.sh`, `lib/wake.sh`, `lib/claude.sh`, `lib/codex.sh` still do
  what com8 does not; kept deliberately, but they carry the pre-com8 flaws
  (bare-string identity, no delivery semantics).
- **COM8-engine is one disk copy from lost** (`~/src/QPG-MIT/COM8-engine`,
  no longer on GitHub) — the repo where identity-as-registry-object was
  first stated. Preservation hazard already recorded in the v1 spec; still
  outstanding.
- M-5 (recorded in the overnight handoff, still open): `depart` has a
  sub-millisecond window where a send between release and proxy-rebind sees
  "unknown identity"; the clean fix is build-then-swap.
