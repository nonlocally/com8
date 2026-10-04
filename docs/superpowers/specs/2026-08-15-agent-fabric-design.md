# The Agent Fabric — durable identity, store-and-forward addressing, the com8 daemon, and sandboxed permission planes — Design

Date: 2026-08-15 · Status: **draft, awaiting review** · Home: the `communicate` repo (grows into v0.3)

*Historical note: this document predates the product rename; references to the retired predecessor project (the anu monorepo and its Go engine extraction) were reworded on 2026-10-04 and do not describe this repository.*

> This design merges two systems that each solved one half of the same problem.
> **communicate** delivers a message *as a turn* — an inbound socket message resumes
> an idle Claude session ("a message is a wake"), rendered as an attributed peer
> event, not keystrokes — but it is pure rendezvous: both ends must be alive, which
> is why the cross-fleet bridge was down for 26 hours, twice. **anu** has a durable
> reply channel (`~/.local/share/anu/pane/replies/<id>`, atomic write, survives the
> pane, already crosses the container boundary) — but "nothing reads them to an
> agent's attention." Durability without delivery; delivery without durability. The
> fabric is the process that owns the handoff between them.

## Goal

Give every agent — Claude Code, Codex, a boxed worker, a peer on another fleet — **one
durable identity and one durable address** that survive process death, laptop sleep,
`/rename`, and a device boundary; reachable across your own Tailscale network; with the
planes you care about (agent↔agent vs agent↔pane vs sandboxed) expressed as **typed
addresses on one identity**, not separate namespaces. Communication first. Sandbox
isolation and cross-fleet grants are designed-for in the architecture but **phased to
v2**.

The organizing spine is the four-layer decomposition:

| Layer | What it is | Solved by | Parts that already exist |
|---|---|---|---|
| **1 Identity** | a durable name, decoupled from any process; == the session's `/rename` name | registry object + rename-sync | communicate sidecar `name`; beam's `custom-title` append; Switchboard `aliases:` |
| **2 Address** | a durable mailbox; inbound and outbound are *separate* addresses | store-and-forward + typed reach | cc_peer mailbox `inbox.jsonl`; anu reply-file channel |
| **3 Network mgmt** | the per-device **com8** daemon that routes, probes, and does store→wake | launchd singleton | anu `watchd` lifecycle; Switchboard launchd persistence; communicate socket-wake |
| **4 Permissions** | (a) who may message whom; (b) what compute/fs/net an identity is bound to | mailbox ACL + forward-only key + apple/container | the collaborator's restricted key; `box`; apple/container v1.0.0 |

## Decisions taken (with the fork they resolve)

1. **Home = the `communicate` repo.** The socket protocol, the registry (name→capability),
   the mailbox, and the reply-channel discipline all live here or map cleanly onto it; the
   studies' verdict was "one home." The predecessor engine repository (slug rewritten by the 2026-10-04 rename) — the Go kernel that first named
   "identity is a registry object, not a pane id" — is the thesis but **no longer exists on
   GitHub; the only copy is on this disk** (push it somewhere before designing further on it).

2. **COM8 per device, not per agent.** One lightweight launchd-managed daemon owns the
   mailboxes, the route table, the liveness loop, and the store→wake handoff. Individual
   agents do **not** need a stable reachable address; the com8 does. This collapses the
   N×N socket-management mess (PR #9's whole struggle) into one stable link per device pair.
   *Rejected:* per-agent adapters only (Switchboard shape) — cannot self-probe, cannot
   store-and-forward, cannot close the received→acted-on gap. *Rejected:* a full herdr-style
   process supervisor — more to own than communication-first needs; revisit if agents must
   survive lid-close as *processes*, not just as addresses.

3. **One global identity; the plane is an address *type*.** `mail:` (agent↔agent, durable),
   `pane:` (agent↔pane, ephemeral tmux route), `box:` (sandboxed) — one name, many typed
   reaches. *Rejected:* two separate registries — two tables to keep honest and reconcile,
   for no gain.

4. **The identity name *is* the session `/rename` name, synced bidirectionally.** Not a
   parallel scheme. Read the live name from the sidecar `name` field (Claude already
   maintains it; `/rename` updates it); drive a rename by sending `/rename <name>` to a live
   session, or appending a `custom-title` record to a dormant transcript (beam's proven
   method). The com8 reconciles registry-name ⟷ sidecar-name continuously.

5. **v1 = local + your own Tailscale network.** Full-SSH transport is acceptable *on your own
   tailnet* because you own both ends; the forward-only restricted key is a cross-**fleet**
   (foreign-user, foreign-tailnet) concern deferred to v2. Sandbox-identity via
   apple/container is v2. *This keeps v1 the communication substrate and nothing else.*

6. **Reuse communicate's wire protocol verbatim; add the delivery semantics it drops.**
   communicate already generates a `msg_id` and accepts an `orig_msg_id` it never
   correlates. The fabric turns those into real ack + at-least-once delivery. No new wire
   format; the frame stays `{"type":"user","message":{...}}` written to a unix socket.

## Layer 1 — Identity

**A name is a claimed, owned registry object — never a bare string.** Today communicate
resolves name collisions by "newest `startedAt` wins," silently hijacking all future
routing, and `--as NAME` lets any socket-writer forge attribution. The fabric closes both:
the com8 is the authority for name ownership; a name is *claimed* (first claimant holds
it until it releases or is measured dead), and attribution on the wire is stamped by the
com8, not chosen by the sender.

**Registry entry (extends the PR #2 schema, unchanged fields plus typed `reach:`):**

```yaml
name: <their-agent>        # durable identity == the session's /rename name
kind: claude-code | codex | mailbox | box
operator: <their-operator>
reach:
  mail:  <their-agent>@<collaborator-host>   # agent↔agent, durable (com8-owned)
  pane:  air-2:%82                           # agent↔pane, ephemeral (tmux route)
  box:   box-fdtd-3 / 192.168.64.5           # sandboxed (v2)
aliases: [agent-2, librarian]     # historical / session names still resolve
updated: 2026-08-15
```

**Rename-sync (the mechanism behind decision 4):**
- *session → fabric (read):* the sidecar `~/.claude/sessions/<pid>.json` `name` field mirrors
  `/rename` live. The com8 watches it; a human rename updates the route table (and adds
  the old name to `aliases:`).
- *fabric → session (write):* to make the human-visible name match a fabric-assigned identity,
  send `/rename <name>` into a live session; for a dormant transcript, append a `custom-title`
  record (`beam:326`'s method).
- **Open question:** driving `/rename` cleanly into a *live* session is unproven — beam's
  file-append is validated only for not-yet-resumed transcripts. v1 must test this or fall
  back to send-as-input.

## Layer 2 — Address

**Per-identity durable mailbox, owned by the com8, not by any session.** This is the
principle beneath "the mailbox": *an address must not die with a process.* Store-and-forward
converts the hard synchronization problem (both ends alive at once — which fails constantly
for intermittent agents) into an easy durability problem (a file the com8 controls).

- **Mailbox** = `~/.local/share/communicate/mail/<identity>/inbox.jsonl`, append-only, atomic
  write (the anu reply-channel discipline). Lines: `{"ts","from","msg_id","text","reply_to"}`.
- **Inbound ≠ outbound.** The old `ssh -N -L …-R …` welded receive and send onto one tunnel,
  so a peer's stale socket litter disabled *your* sending (issue #7/#8). The com8's
  inbound listener and outbound sender are **independent channels that fail independently**.
- **Delivery semantics the wire already hints at:** every message carries a `msg_id`; the
  receiving com8 writes an ack referencing it. At-least-once delivery; the sender
  retries until acked or dead-lettered. (communicate generates `msg_id` and drops it today.)
- **Liveness is measured and provenance-labelled, never inferred from a file existing** (the
  26-hour bug). Ladder: `probed > reported > reachable-host > declared`. "A socket file is not
  a listener" → probe = connect then measure *time-to-EOF* (dead ends EOF in ~5 ms; a live
  listener holds the line). **Self-probe is a first-class duty**: the com8 measures its
  *own* published addresses, not just peers' — the rule that cost 4 days when applied only
  outward.

## Layer 3 — Network management: the com8 daemon

**A single per-device singleton**, launchd `RunAtLoad` + `KeepAlive`, so it survives sleep,
restart, network change, and ssh-agent wedge — the durable state the hand-rolled tunnel never
had ("there is no state to reattach to when it dies"). Lifecycle is anu `watchd`'s proven
shape: pidfile, atomic-mkdir lock, self-enforcement, auto-exit when its substrate vanishes.

**Duties:**
1. **Own the mailboxes** (Layer 2) — durability independent of any session.
2. **Maintain the route table** `identity → {mail address, live pane/socket route if any,
   measured liveness+provenance}` — the authority anu's `pane` layer lacks (where `%NN` dies
   with the pane and every richer name dies with its window).
3. **Liveness + self-probe loop** on a timer (Layer 2).
4. **The store→wake handoff — this closes your received→acted-on gap.** On inbound: append to
   the mailbox (durable) **then** attempt attention-delivery into a live session via
   communicate's socket protocol (which surfaces it as a peer *turn* / wakes an idle session).
   If no live session: hold; deliver the instant one appears (a new sidecar shows up). "A
   message sitting in inbox.jsonl doesn't wake anything" — now the com8 is the thing
   that does.
5. **Cross-device = com8 ↔ com8.** Exactly one link per device pair. On your own
   tailnet (v1): ordinary SSH. Cross-fleet (v2): the forward-only restricted key. **GitHub
   remains the auditable slow-path fallback** (the collaborator's #8 argument: the only channel that
   worked all day, and it leaves a trail).

**Efficiency:** one mostly-idle process — a probe timer, a kqueue/inotify watch on the mailbox
dirs, and a listener socket. Not N daemons. Per-agent launchd adapter sockets are added only
where an agent must stay *reachable while its own session is down* (the Switchboard durability
case); the com8 handles everything else.

**Integration seam with anu (validated as unusually clean):** `pane` is one bin with
separable `resolve / classify / transport` concerns, and everything (the MCP server, `box`,
`context carry`, `ncn`) calls it as a subprocess. The fabric **replaces `_resolve` only** —
teach resolution to return a *route* (local tmux pane / remote pane via the com8 /
socket peer / mailbox) instead of a bare `%id`. `_classify` + `cmd_send` become the
**local-tmux backend**, one backend among several. The reply-file dir becomes the fabric's
response bus (it already crosses the box boundary and survives the pane). Nothing above
`_resolve` changes — `pane send`'s deliver-and-verify discipline, the state classifier, and
`gather`'s `[confirmed]` vs `[best-effort screen]` honesty all survive verbatim.

## Layer 4 — Permissions (designed-for; v2)

Your segregation insight, made concrete: **two different permission problems, not one.**

**(a) Comms permission — who may message whom.** Name ownership (Layer 1) + a mailbox ACL +
Claude Code's own held-message approval gate (a bypass-mode session already holds an inbound
peer message for human approval). Fixes today's "any local process that can reach a forwarded
socket can inject a turn."

**(b) Sandbox permission — what an identity is bound to.** Two primitives:

- **Cross-fleet grant = the recovered forward-only restricted key** (issue #4, verbatim):
  `restrict,port-forwarding,command="/usr/bin/false" ssh-ed25519 …` — deny-all-then-grant-one:
  socket forwarding works, **no shell, no exec, no pty**. Plus `StreamLocalBindUnlink yes` at
  the *sshd* end (grants no new authority; converts "renegotiate after every restart" into
  "reconnects on its own") and a tailnet ACL to exactly one `host:22`. **Grant capability, not
  credentials** — the Engaging/Slurm FDTD campaign ran on the collaborator's cluster without you ever
  holding an Engaging allocation; the registry entry + a message channel beat account
  provisioning.
- **Sandbox-identity = apple/container** (v1.0.0, this host is macOS 26 with the full
  networking story). Each box is its own VM with a routable IP; **a unix socket crosses the VM
  boundary** (`--publish-socket` / virtiofs) — so a boxed agent exposes a `cc-socks` socket to
  the host com8, gets a sidecar, and becomes a **first-class peer**: sandbox identity and
  mailbox identity unify. Bind `agent-N ↔ box ↔ dedicated IP ↔ per-agent key ↔ label`.
  Hardening `box` needs today (it runs as guest root, worktree fully writable, Claude creds and
  reply-dir shared across all workers, full internet): `--user`, `--cap-drop ALL`, per-agent
  subdirs, and `--network none` + a host-broker socket as the *only* egress channel (egress is
  otherwise coarse: all / host-only / none). **Must-measure before relying on it:** whether a
  box can reach the tailnet, and driving rename into a live session.

## What v1 actually ships

Identity with `/rename`-sync · the durable per-identity mailbox · the com8 daemon
(launchd) with the store→wake handoff · cross-device over **your own tailnet** (ordinary SSH)
· the anu `pane` `_resolve` adapter so `pane send <device>:<name>` routes through the fabric
and confirmed replies come back over the reply-file bus · ack/at-least-once delivery.

**Explicit v1 non-goals:** the forward-only cross-fleet key; container sandbox-identity;
multi-hop routing (every path is one hop from the com8); a UI (PR #9's switchboard is
the eventual operator console, not v1).

## Invariants (testable properties, in order of what they cost to learn)

1. **An address must not die with a process.** (Cost: 4 days of failed sends.)
2. **Never advertise what you haven't measured — including your own side.** Self-probe.
3. **Decouple failure domains.** Inbound and outbound never share a channel.
4. **Liveness is measured with provenance, never file-existence.** (Cost: a 26-hour outage.)
5. **A reply is proof of completion, never a screen-scrape guess.**
6. **Patching does not move an agent** — home is where it runs; a patch is a line to it.
7. **Fail closed on anything destructive** (respond only at a real approval prompt; never pick
   "always allow"; refuse to message self or an unscoped set).
8. **Grant capability, not credentials.**
9. **Honesty that costs something:** dated registry entries; sign messages with the
   addressable name; degrade the fast socket path to the slow auditable one, never to nothing.

## Risks / open questions

- Driving `/rename` into a **live** session cleanly (beam's append is dormant-only).
- **Tailnet reachability from inside a box** is unverified — measure before Layer 4 depends on it.
- **The com8 can inject a turn into any local session** — it is a privileged local
  daemon; its own socket must be `0700`/owned, and it must honor Claude's held-message gate.
- **The predecessor engine repository is one disk copy from lost** — preserve it; it's where the core idea is stated.
- **Merge-order hazard:** PR #9's honest-liveness rule contradicts `communicate directory`'s
  file-existence join — the merge must pick the probe ladder and delete the other.
- **Name-ownership mechanism** (claim/release/expiry) needs a concrete protocol, not just "the
  com8 decides."

## What each source contributes

| Source | Contribution that survives |
|---|---|
| communicate protocol | wire frame; socket-as-turn delivery ("a message is a wake"); across-accounts by construction; MECHANISM.md |
| anu `pane` | deliver-and-verify; the one state classifier; the durable reply-file channel; the clean `_resolve` seam |
| PR #9 Switchboard | provenance liveness; "a socket file is not a listener"; launchd stable-path persistence; `aliases:`; "patching doesn't move an agent" |
| the predecessor's Go engine extraction | "identity is a registry object, not a pane id" — the four separable planes |
| apple/container | per-VM isolation; unix socket crosses the boundary → sandbox identity == peer identity |
| the collaborator work | the forward-only restricted key; grant-capability-not-credentials; GitHub as auditable fallback |
