# Fabric v2 — the seat plane, com8 spawn, and the from-scratch plugin — Design

Date: 2026-08-16 · Status: **draft, awaiting review** · Builds on: `2026-08-15-agent-fabric-design.md` (v1, shipped as com8 in PR #10)

> v1 shipped the message plane: durable identities, mailboxes, store→wake, links.
> v2 completes the fabric so the anu plugin (`~/COM8/plugins/anu`) can retire —
> which requires restoring something the first v2 sketch wrongly discarded:
> **the seat plane**. The v1 spec already reserved `pane:` as a typed reach on an
> identity; the cluster-conductor pattern (an agent driving a live, Duo-authenticated
> ssh pane on Engaging via send-keys/capture-pane, consent-gating `sbatch`) is the
> proof case. You cannot mailbox an HPC login node. What dies is only
> *TUI-babysitting of our own agents as the default coordination path* — the verbs
> survive, generalized and fabric-native.

## Goal

Make `plugins/anu` useless by making com8 complete: (1) finish the message plane
(reply-correlated `ask`, groups, notify); (2) add the **seat plane** — drive any
interactive surface, local or cross-device, with the old pane discipline
reimplemented fresh; (3) add **`com8 spawn`** — the fabric creates agents, not
just routes them; (4) build the **plugin from scratch** — a thin MCP server over
the control socket, derived from a two-loop dogfood, published to npm + the MCP
Registry, installable by any MCP client (`claude mcp add` / `codex mcp add` /
mcp.json).

## The two planes (the model, corrected)

| Plane | Nature | Default for | Verbs |
|---|---|---|---|
| **Messages** | durable, identity-addressed, store-and-forward, acked | talking *to an agent* (any runtime, any device) | send, ask, inbox, wait, groups |
| **Seats** | interactive, ephemeral, observed, keystroke-level | driving *a surface* (cluster shells, REPLs, TUIs, ssh panes, watched agents) | ls, spawn, send, read, state, wait, respond, bind |

`seat bind <identity>` joins them: an identity's registry `reach:` gains a seat
route, and the roster shows both — `communicate → mail: live · seat: mini-2:%9986 (busy)`.

## Decisions taken (with the fork they resolve)

1. **Seats are first-class, not legacy.** *Rejected:* retiring pane ops (the
   first v2 sketch) — it contradicted the v1 spec's `pane:` reach and would kill
   the cluster/RL/REPL capability class. What retires is only the *default* of
   babysitting our own agents' TUIs; mailboxes stay the agent↔agent default, and
   the plugin's tool descriptions say so.

2. **Seat v1 = tmux panes only** (user-locked). An ssh session to a cluster runs
   *in* a pane, so tmux seats already cover the ncn/Engaging conductor. `box:`
   exec-channel seats and `job:` seats join in the sandbox track. The seat layer
   is written against a small driver interface so those slot in later.

3. **Cross-device seats ride the existing links** — a new envelope kind
   `{"v":1,"kind":"seat","op":"ls|spawn|send|read|state|wait|respond","seat":"%42",...}`
   whose **ack carries the result** (read output, state word, spawned id). This
   formalizes what anu never did (mesh-spawn recorded remote pane ids and never
   used them). Same queue, backoff, dedup, and dead-letter semantics as mail.
   *Rejected:* a separate seat transport.

4. **Seat capability is opt-in per link, upgradable in place** (user-locked).
   Links carry mail by default; `com8 link <dev> --allow-seats` grants seat ops —
   run against an existing link it **upgrades in place** (same mechanism as
   re-linking with a new addr); `--revoke-seats` downgrades. A seat envelope
   arriving on an ungranted link gets a negative ack and dead-letters on the
   sender. Deny-all-then-grant-one, the collaborator-key philosophy applied to seats.

5. **Borrow the ideas, never the code.** The studies in `docs/studies/` are the
   *behavioral spec*: the state classifier (title-spinner animation at jittered
   intervals; precedence dead→approval→busy→booting→idle), the send discipline
   (sanitize → literal stage → **separate** retried Enter → verify by composer
   marker → exit-2 for sent-unconfirmed), two-signal approval detection,
   fail-closed respond. All reimplemented fresh in com8's codebase with our own
   test suites; every timing constant ported as a deliberate, tested decision.
   Not one line copied from the 1,576-line pane bin. The dogfood phase doubles
   as re-verification of the scar tissue against live TUIs.

6. **The seat module lives in the com8 daemon** as an optional backend: probe
   for tmux at startup; on headless devices seats are simply absent and mail is
   unaffected. Seat state can feed the identity roster (a bound seat's
   classifier state is a richer activity signal than a socket probe).

7. **`com8 spawn <identity> --cli claude|codex [--seat pane|headless] [--device D]`**
   is the creator verb: headless mailbox-driven workers by default; a visible
   tmux seat when watching matters or the target is a TUI; `--device` spawns via
   a seat-granted link. Spawn auto-claims + adopts (rename-sync) + binds.
   `fan` = N spawns + a group; `consult` = spawn-or-reuse a private peer + ask.
   Box spawning joins with box seats.

8. **Message-plane completion:** `ask` (send with `reply_to` correlation, block
   until the correlated reply or timeout — msg_id is already end-to-end),
   **groups** (send to a set/role), **notify** (the pane_call successor: summon
   the human with a durable reason). `respond`/`approve` survive as *seat* verbs
   with the fail-closed rule, for arbitrary TUIs — never the agent↔agent path.

9. **The plugin is built from scratch, after the CLI is proven** (user-locked
   methodology): two-loop dogfood (below), then a thin MCP server on the
   **official TypeScript SDK** — stdio + Streamable HTTP, every tool one JSON
   line to the control socket, zero logic (two faces, one kernel — the single
   architectural idea kept from the old plugin). Tool descriptions are written
   from dogfood transcripts: we document what agents actually misread.

10. **MCP posture:** build on the official SDK's version negotiation; comply now
    with the 2026-07-28 rules that are backward-benign (deterministic tool
    order, cacheable list results, no Sampling/Roots/Logging dependence,
    stateless-friendly design) so the spec bump is a version change, not a
    rewrite. Non-Claude inbound wake = `wait_for_message` long-poll tool (MCP
    has no general server-push); Claude sessions keep native socket-wake; the
    cc_peer codex adapter remains as the push path where it already works.
    Streamable HTTP binds on the tailnet → any device's MCP client can drive
    any com8; auth = tailnet identity in v2 (spec OAuth only if ever exposed
    beyond it). Distribution: npm package (vendoring the stdlib-only Python
    daemon — no pip; `npx com8 setup` installs launchd/systemd + claims), then
    the MCP Registry via `mcpName` + `mcp-publisher`.

## The complete tool surface (~20 tools, five groups)

| Group | Tools |
|---|---|
| Identity / roster | `agents_list` (roster + measured liveness + provenance), `claim`, `release`, `adopt`, `whoami` |
| Messaging | `send`, `ask` (correlated, blocking), `inbox_read`, `wait_for_message` (long-poll), `group_send` |
| Seats | `seat_ls`, `seat_spawn`, `seat_send`, `seat_read`, `seat_state`, `seat_wait`, `seat_respond` (fail-closed), `seat_bind` — all device-transparent |
| Creation | `spawn`, `consult` |
| Links / human | `status`, `link_status`, `notify` |

Resources: `com8://routes`, `com8://inbox/<name>`, `com8://seats` — with `ttlMs`
cache hints. Descriptions steer: messaging is the default plane; seats are the
explicit interactive escape hatch.

## Build methodology (the two-loop dogfood)

1. **Loop 1 — feature loop, CLI only.** Every capability lands as
   `communicate com8 …` verbs with isolated test scripts, exactly like v1
   (ask/groups/notify; the seat layer + classifier + send discipline; seat
   envelopes + link grants; spawn/bind). The CLI is the complete, tested
   definition. No plugin exists yet.
2. **Loop 2 — dogfood with real agents.** Use com8 to test com8: spawn a small
   team (headless claude workers, a codex worker, one pane-seated agent) briefed
   to complete tasks only achievable through fabric verbs — (a) a
   mailbox-coordination task (workers produce a joint report using send/ask/
   inbox only), (b) a cluster-conductor rehearsal (drive a seated ssh session
   with seat_send/read/wait). Read mailboxes and transcripts; fix friction in
   the CLI, not the plugin. Iterate until agents use it fluently.
3. **Freeze → plugin.** Generate the MCP server from the frozen verbs;
   descriptions from observed confusion; dogfood round 2 with agents using the
   MCP tools from Claude Code *and* codex, across devices.
4. **Publish** (npm + Registry; per-client install lines) and **retire**: when
   no client config references `plugins/anu` and its call count is zero for two
   weeks, delete it from `~/COM8`. `pane_present` stays with nv (editor
   concern); beam survives untouched.

## Out of scope (v2)

Box/job seat drivers (sandbox track); cross-fleet seat grants (restricted-key
track); OAuth/CIMD on the HTTP transport (tailnet identity suffices until com8
is exposed beyond it); a TS rewrite of the daemon (the Python daemon stays; the
Node layer is a thin client).

## Open questions

- npm name/scope (`com8` is likely taken; `@aadarwal/com8` + registry name
  `io.github.aadarwal/com8`?).
- Dogfood scale/budget per iteration (a handful of agents per loop, more only
  if friction warrants).
- Whether `seat_read` output should redact obvious secrets before returning
  (terminal screens can contain tokens) — lean yes, with a `--raw` override.

## Build order

**A1** ask/groups/notify → **A2** seat layer (tmux driver, classifier,
send-discipline, local) → **A3** seat envelopes + link grants (cross-device) →
**A4** spawn/bind (+ fan/consult) → **Loop 2 dogfood** → **B** plugin from
scratch + dogfood 2 → publish → **C** retirement. Each step lands with tests,
per the v1 discipline.
