# Harvest triage: old anu/COM8 → new com8 (what ideas to take, reject, rethink)

Date: 2026-08-16 · Method: subagent read-only catalog of /Users/aadarwal/COM8 + the anu study.
Purpose: decide what IDEAS carry into the clean-slate com8 rebuild. Borrow ideas, never code.
Verified live: both swarm state dirs empty — the swarm layer is dead code.

Frame for every verdict: com8 = durable identity + two planes. **Messages** (identity-addressed,
store-and-forward, acked) = agent↔agent default. **Seats** (keystroke-level, observed, ephemeral)
= explicit escape hatch for surfaces you cannot mailbox (cluster shells, REPLs, TUIs). Anything
whose only job was making tmux-pane-babysitting tolerable dies.

## 1. The `pane` bin (24 verbs) — two disciplines are the real assets
The state **classifier** (title-spinner animation, jittered sampling 0.11/0.17/0.23/0.29,
precedence dead→approval→busy→booting→idle) and the **send discipline** (sanitize → literal
stage → *separate* retried Enter → verify by composer marker → exit-2 for sent-unconfirmed) are
TAKE — as behavioral spec for the **seat driver only**, reimplemented fresh, every timing
constant re-tested. They must never leak into the message plane.

| Verb | Verdict |
|---|---|
| send | SPLIT: agent↔agent → com8 `send` (shipped); surface → `seat_send` (send discipline) |
| ask | TAKE rethought: message `ask` = send + reply_to correlation, block (SHIPPED A1). The 1s-tick turn-loop + 2-tick settle is seat-only → `seat_wait` |
| read | TAKE → `seat_read` (redact obvious secrets by default, `--raw`) |
| state/status | TAKE → `seat_state` (the classifier); bound seats feed roster liveness |
| wait | SPLIT: `wait_for_message` (message) + `seat_wait --for/--gone` |
| spawn | TAKE → `com8 spawn`: headless mailbox-driven default, `--seat pane` when watching, `--device` via seat-granted link; auto-claim+adopt+bind (anu default of always-a-pane inverts) |
| fan | TAKE minimal: N spawns + a group. No topology object |
| consult | TAKE: spawn-or-reuse private peer + correlated ask; mailbox replaces the reply-file/lock machinery |
| broadcast | TAKE → `group_send`; keep the refusal (no unscoped broadcast) |
| reply/gather | RETHINK: the reply-file channel was anu's greatest hit *because anu had no mailboxes*. In com8 the mailbox IS that channel. reply = correlated mail; gather = collect N correlated replies. Keep: atomic delivery, sandbox-boundary crossing, gather's honest labeling ([confirmed]/[no reply yet]/[best-effort screen]). Never report a scrape as an answer |
| respond/approve | TAKE → `seat_respond`, fail-closed (refuse unless classifier=approval; never "always allow"; two-signal detection). Never agent↔agent |
| call | TAKE → `notify` (SHIPPED A1) |
| name/id/whoami | DONE: com8 claim/adopt/whoami already replace the tmux-option registries |
| ls | SPLIT: `agents_list` + `seat_ls` |
| watch/focus | REJECT (subsumed by seat_wait/state; window-jump is notify-UI) |
| stop | TAKE minor: seat interrupt |
| present | REJECT for com8 — stays with nv (editor concern) |
| watchd/needs/notify | RETHINK: ledger + daemon lifecycle survive (§8); pane-polling FSM → optional seat observer. Keep agent-facing vs system-facing split (needs/watchd NOT MCP-exposed) |

## 2. `swarm` topologies — the dead layer, reject nearly all
2926-line fn; state dirs empty; viz `swarms:[]`; rides welded-`C-m` send-keys (no `-l`, no verify).
- start/mixed → REJECT as topology; survives only as `com8 spawn` ×N + a group.
- star/pipe/pair → REJECT: conductor+workers / sequential handoff / pairs are *briefing patterns* an agent runs over spawn/group/ask, not infrastructure. Mail IS the pipeline.
- gate → REJECT the pipeline-gate machinery; human-approval-as-pause survives better as `notify` + human answering mail.
- wt → REJECT: worktree provisioning is the runner's concern (at most `spawn --cwd`).
- **tournament → REJECT** (user-explicit + on merits: it encodes a workflow opinion — competitive rounds, LLM scoring, pruning — into the substrate; a conductor wanting a bake-off runs one with spawn+group+ask). Zero mechanism worth salvaging.
- mesh → REJECT: `tailscale ssh -t` into a pane is not cross-device messaging.
Nothing else survives — not `agent-N` (durable names replace it), not the stale `agents/*.json`.

## 3. mesh / meshsync / beam — cross-device
- **beam → TAKE as-is and apart** (orthogonal: session teleport, not agent comms; best-designed cross-device code in the repo). Steal two principles: identity lives in the durable artifact (the custom-title record), and name divergence explicitly. Keep the tool.
- mesh spawn → REJECT (records remote_pane, only ever messages the local proxy pane; a monument to why screen-addressing doesn't cross devices).
- meshsync → REJECT (one-way rsync --delete, last-writer-destroys, syncs the wrong dir). Clean rebuild = **no state replication**: com8 links move envelopes (acked/deduped/backoff/dead-letter); state stays on the owning device and is queried. Seat ops cross as an envelope kind whose ack carries the result.
- device registry → RETHINK: com8's link table is the successor; keep manual-host add; per-link capability grants replace anu's "whatever the human can do, any agent can do on every device". Cross-device auth is greenfield (zero restricted-key prior art to preserve).

## 4. `ncn` — the load-bearing capability (the seat proof case) → TAKE
Mechanism: conductor stays on host and drives a human-authenticated ssh login pane via
send-keys/capture-pane (inherits Duo because the human auth'd in that pane — you cannot mailbox
an HPC login node); consent-gate the irreversible `sbatch` ("it consumes node-hours, get a yes");
the job self-reports coordinates (echoes NCN_NODE/port/token into the job log, conductor greps
them — no cluster-side daemon, works on any Slurm cluster you can ssh to); tunnel through the
login node (compute nodes firewalled); apptainer exec --nv for GPU-in-container; teardown
discipline. Weak spot: recovery state in @ncn_* window options dies with the window.

**Com8 rebuild:** the ssh session is a **tmux seat**; profile skills become *capability patterns
over seat verbs*, not commands: seat_spawn an ssh seat → human authenticates once → conductor
seat_send/read/wait from ANY device (seat ops ride links). Consent gate → `notify` + human reply
(durable, auditable) instead of an ephemeral chat yes. Seat metadata (host/job id/tunnel) moves
from window options into the daemon's seat record, so a crashed conductor respawns onto the same
live seat. Profile dispatch → seat-driver selection. The ncn-cluster runbook survives ~verbatim
as a skill over com8 seat verbs.

## 5. box/cxc contained agents → RETHINK
Anu got right: creds stripped by omission; the container carries the kernel (pane bin + reply
dir mounted so a contained worker's result is [confirmed], not scraped); commit identity as env;
resource caps. Must change: the reply dir is RW+shared across all workers (its own comment admits
this); all boxes share one /root/.claude login (shared credential, no per-agent identity);
ANTHROPIC_API_KEY passed wholesale. **Com8 rebuild:** what crosses the boundary is the com8
client + ONE per-agent credential — each contained agent gets its own claimed identity and a
mount/socket scoped to ITS mailbox only, so a compromised worker spoofs nobody and reads no
sibling's mail. Box seats (exec-channel driver) join later per the sandbox track.

## 6. anu secrets (Infisical) → REJECT for now
Scope table is per-CLI-vendor not per-agent; nothing in the comm layer ever calls it. Remember
cheaply: durable creds never touch argv (com8 per-agent creds should honor this); the need for
per-agent/per-fleet secret scoping returns with cross-fleet grants — note in the restricted-key
track, build nothing tonight. Tailnet identity + per-link grants are com8's whole auth story
until it's exposed beyond the tailnet.

## 7. The MCP plugin → the ONE pattern to keep (CONFIRMED TAKE)
plugins/anu/mcp/server.py is a pure argv shim over the bin — no duplicated logic, no state; "two
faces of one kernel". Principle: **all behavior in one kernel; every interface is a face.** The
CLI is the complete tested definition; the MCP server translates tool call → one kernel call →
result, zero logic. Corollaries: double-bounded timeouts per tool; UX-guardrail refusals may live
in the face but policy in the kernel; system machinery (needs/watchd) NOT exposed as tools
(agent-facing vs system-facing is a real line); the instructions string doubles as the
session-start hook; tool descriptions written from observed agent confusion, not aspiration.

## 8. Everything else
- notification/needs lane → TAKE → `notify` (SHIPPED A1): a durable ledger of "the human is
  needed, here's why", not a transient ping. The watchd lifecycle (singleton per scope, pidfile,
  atomic-mkdir lock, self-enforcement, auto-exit) is the daemon-hygiene checklist com8 already passes.
- nv editor bridge → REJECT for com8 (not comms). "surface behind a socket" (NV_SOCK host:port)
  proves a far-future nvim-RPC seat driver is possible *because* seats are a driver interface.
- viz → REJECT the tool, keep the demand: roster + measured liveness = com8 status/agents_list;
  Haiku pane-captioning is screen-scrape-as-telemetry, dead in an identity-first world (agents say
  what they're doing in mail).
- context carry → REJECT (orchestration above the fabric; spawn + a briefing message covers it).
- agentlog → REJECT as built (only wired to _swarm_send). The need — unified audit — is native:
  **the mailbox IS the log.** Design the store so history is queryable and agentlog falls out free.
- honesty rules → TAKE wholesale: verify by observed state never hope; exit-2-for-unconfirmed; one
  classifier no second heuristic; label degraded results; fail closed on destructive ops; text
  escape hatches (stdin/-f); named precedence, ambiguity is an error.

## Build order (confirmed)
ask/groups/notify (message-plane completion — DONE A1) → seat layer with classifier + send
discipline as behavioral spec (A2) → seat envelopes + link grants (A3) → spawn/bind (A4). The
topology zoo, meshsync, tournament, screen-scrape telemetry, and every tmux-option registry stay
behind in the old repo when it retires.
