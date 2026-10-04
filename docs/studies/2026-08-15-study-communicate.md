# Study: the communicate repo (evidence base for the agent-fabric design)

Date: 2026-08-15 · Method: subagent deep-read of the full repo + PR/issue history
Companion studies: anu-com8, apple-container, collaboration. Consumed by
`docs/superpowers/specs/2026-08-15-agent-fabric-design.md`.

## 1. Wire protocol

**Sidecar (identity + address).** `docs/MECHANISM.md:10-33`: every Claude Code session writes
`${CLAUDE_CONFIG_DIR:-~/.claude}/sessions/<pid>.json`:

```json
{"pid":67034,"sessionId":"c87d2c71-…","cwd":"/Users/you/src/app",
 "messagingSocketPath":"/tmp/cc-socks/67034.sock","name":"agent-2",
 "kind":"interactive","status":"idle","peerProtocol":1}
```

> "The two load-bearing fields are **`name`** (the identity) and **`messagingSocketPath`**
> (the address). Together, the set of sidecars *is* a routing table: name → socket."
> (`docs/MECHANISM.md:31-33`)

The full field set communicate writes when it fabricates one is `lib/cc_peer.py:172-188` —
adds `startedAt`, `updatedAt`, `statusUpdatedAt` (epoch ms), `version:"communicate-peer"`,
`entrypoint:"cli"`, `kind:"interactive"`, `status:"idle"`.

**Socket path** (`docs/MECHANISM.md:37-45`): `${XDG_RUNTIME_DIR || CLAUDE_CODE_TMPDIR ||
"/tmp"}/cc-socks/<pid>.sock`; parent dir mode `0700`, socket `0600`; live path exported as
`$CLAUDE_CODE_MESSAGING_SOCKET`. Mirrored in `lib/common.sh:27-33`.

**Frames on the wire** (`docs/MECHANISM.md:64-82`): newline-delimited JSON, **one object per
connection**, written then half-closed. Receiver-enforced rules, verbatim:
- "`message.content` **must be a nonempty plain string** (content-block arrays are rejected)."
- "`from` is `uds:<absolute-socket-path>`; it is the reply address."
- "`session_id`, if present, must equal the target's own session id — otherwise the message
  is **dropped**. Omit it."
- "`priority` defaults to `next`."

The object communicate emits (`lib/cc_peer.py:73-79`):
`{"type":"user","message":{"role":"user","content":…},"priority":"next",
"from":"uds:"+from_sock,"msg_id":<uuid4 hex>}`. `session_id` deliberately absent
(`lib/cc_peer.py:66-69`).

**Attribution envelope** (`docs/MECHANISM.md:86-90`): the content is wrapped as
`<cross-session-message from="uds:/tmp/cc-socks/99999.sock" from-name="codex-local">…body…
</cross-session-message>`. Built at `lib/cc_peer.py:52-61`, which also defends against a body
forging the closing tag (line 57).

**Liveness sweep / GC** (`docs/MECHANISM.md:50-61`): `ListAgents` reads every sidecar and
connects to each `messagingSocketPath` with a ~250 ms timeout; **accepting the connection is
enough**. Then: "if a sidecar's socket probe **fails** *and* its pid is **not a live local
process**, the sidecar is unlinked." Two defeats: the Claude bridge re-plants every 5 s
(`lib/claude.sh:205-225`); the Codex adapter names its sidecar by **its own live pid** so the
pid test always passes, and re-plants every 3 s (`lib/cc_peer.py:175`, `224-229`).

**The local-only guard** (`docs/MECHANISM.md:106-114`): addresses must match
`^(?:uds|bridge|did):[A-Za-z0-9%:_/.\-]{1,200}$`; `http(s)://host` is rejected. The
load-bearing sentence:

> "**you cannot point Claude at a remote address.** But a unix socket **forwarded over an SSH
> tunnel** appears as an ordinary local path — so it passes every guard. That single fact is
> what makes cross-device peering possible without touching Claude."

**Reply/receipt routing** (`docs/MECHANISM.md:98-104`): the automatic receipt
`{type:"control", action:"peer_message_status", …}` goes back to `from` **only if**
`dirname(from) === dirname(receiver's own socket)` and it ends in `.sock`. Different dirs →
messages still flow, receipts suppressed. Hence communicate mirrors sockets to identical
paths (`lib/claude.sh:145-152`).

**Held-message approval gate:** `README.md:70-71`, `157-159` — a bypass-mode session holds an
inbound peer message for human approval. It is *Claude's* gate; communicate neither
implements nor bypasses it.

**One message end-to-end, two devices (Claude↔Claude bridge).** Device A runs
`communicate claude bridge B newest`:
1. `_claude_sidecars_on B` cats `~/.claude/sessions/*.json` over ssh (`lib/claude.sh:17-20`);
   `_claude_pick` selects newest interactive (`lib/claude.sh:27-52`).
2. Safety: `_valid_sockpath` requires `*/cc-socks/*.sock` and no `..` (`lib/claude.sh:107-113`);
   pid-collision refusal (`lib/claude.sh:153`); `comm_ensure_socket_dir` enforces owner+0700
   (`lib/common.sh:61-72`).
3. Detached supervisor (`lib/claude.sh:171`) runs `ssh -o StreamLocalBindUnlink=yes
   -o ExitOnForwardFailure=yes -N -L "$lfwd:$rpath" -R "$rfwd:$mpath" "$dev"`
   (`lib/claude.sh:216-217`). `-o StreamLocalBindMask=0177` keeps forwarded sockets 0600
   (`lib/common.sh:56`).
4. `_replant` writes B's sidecar locally with `messagingSocketPath` rewritten to `$lfwd`, and
   A's sidecar on B rewritten to `$rfwd` (`lib/claude.sh:205-210`, rewrite at `:103`).
5. `SendMessage` to `<remote-name>`: Claude resolves name→`$lfwd` from the planted sidecar,
   opens the local unix socket (passes the guard), writes one newline-JSON `type:"user"`
   frame, half-closes.
6. sshd on B relays the bytes into B's real `/tmp/cc-socks/<rpid>.sock`. B renders it as a
   cross-session message and (if bypass-mode) holds it for approval.
7. B replies to `from` = `uds:$rfwd` — a local path on B, reverse-forwarded back to A's real
   socket.

For **Codex**, the far end is `lib/cc_peer.py serve`: accepts, reads a line, extracts text,
runs `communicate codex ask <device> --thread peer-<name> … -- <text>`
(`lib/cc_peer.py:151-157`), then `deliver()`s the reply back to `sender`. Concurrency capped
at 4 (`_SLOTS`, `lib/cc_peer.py:110`).

## 2. Identity model

A "name" is **a string in a sidecar JSON file**, nothing more. No key, no signature, no
registration.

- **Where it lives:** `name` in `~/.claude/sessions/<pid>.json`. Claude sets it for real
  sessions (renameable via `/rename`). communicate sets it for synthetic peers
  (`lib/peer.sh:37`, `44`; `lib/cc_peer.py:185`).
- **Uniqueness:** none enforced. Resolution is "newest wins": `max(startedAt)` among matching
  sidecars (`lib/peer.sh:20-24`, `lib/router.sh:73-74`). A second agent claiming an existing
  name silently captures all future routing. The only collision check anywhere is on **pid**
  (`lib/claude.sh:153`).
- **`--as NAME` is unauthenticated spoofing.** Free-text, becomes the rendered `from-name`
  (`lib/peer.sh:113`, `126`). `router_route` hardcodes `--as "router"`; the dispatch tool
  sends `--as orchestrator`. Anyone who can write to a socket can claim any `from-name`.
  Reply-address fallback outside a Claude session
  (`from="…/communicate-cli.sock"`, `lib/peer.sh:124`) is a path nothing listens on —
  replies dead-letter.
- **"Orchestrator mailbox identity" (d12d9f7):** `cc_peer.py mailbox`
  (`lib/cc_peer.py:285-373`) — "a persistent peer identity (a listening socket + a maintained
  sidecar) that does NOT back a codex agent — instead every inbound message is appended to a
  JSONL mailbox file. This is the orchestrator's return address." Sweep-proof the same way.
  Mailbox lines: `{"ts":…,"from":…,"text":…}`. Fragile coupling: sidecars located by grepping
  compact JSON (`lib/claude.sh:88`, `lib/cc_peer.py:309-311`).
- **Registry (PR #2 + #3):** `registry/<peer-name>.md`, filename = wire name. Schema
  (`registry/README.md:14-26`): `name`, `kind`, `model`, `device`, `operator`, `updated`,
  `reach:`, `availability`; PR #5 added `workdir:`; PR #9 adds `aliases:`. Discipline: "an
  entry describes what the agent can do **now**, not aspirationally."
- **Registry↔live join:** `communicate directory` sets `live = name in {local sidecars}`
  (`lib/directory.sh:20-25`) — a pure string join, the exact bug PR #9 indicts.

## 3. Trust / permission model as implemented

**What SSH grants:** every remote op is `ssh <dev> "$*"` with args joined into one shell
string (`lib/common.sh:90-97`). Required access = full non-interactive login shell.
**All-or-nothing SSH** — no restricted command set, no forced command, no per-agent credential.

**Blast radius of `claude bridge`:** writes/removes remote `~/.claude/sessions/` files,
creates/chmods the remote socket dir, `rm -f`s a remote socket path, and establishes a
persistent `-L`/`-R` pair. Once up, **anything that can write to the local forwarded socket
can inject a user turn into the remote session**.

**Blast radius of `codex peer`:** every inbound socket message becomes `codex exec` on the
target, in `--dir`. Default sandbox read-only; `--auto` flips to workspace-write for the whole
peer (`lib/codex.sh:81`, `lib/peer.sh:62`). Injection hygiene is real: message text via stdin
/ single argv element, never a command line (`lib/codex.sh:5-7`, `97-99`;
`lib/cc_peer.py:150`).

**Inconsistent defaults:** the orchestrator dispatches every codex agent with `--auto`
(`openwebui/dispatch_tool.py:221`) while the CLI default is read-only.

**Across-accounts property** (`README.md:138-141`): filesystem+socket substrate connects
sessions on different Anthropic accounts — account-agnostic by construction.

**`communicate down`** = `wake_stop all; claude_unbridge all; codex_unpeer all`
(`bin/communicate:101-106`). Does **not** stop: the orchestrator mailbox daemon (spawned
untracked, `openwebui/dispatch_tool.py:89-96`), the mesh-demo tunnel, openwebui services, or
PR #9 launchd peers. "Nothing is left behind" is true for the three tracked kinds only.

## 4. Orchestrator (PR #5)

`list_agents` (`dispatch_tool.py:169-200`): `communicate directory --json` +
`dispatchable = (codex && workdir) || (claude && device)` + first registry prose paragraph.
`ask_agent` (`:202-231`): `communicate codex ask <device> --dir <workdir> --thread
peer-<name> --auto -- <message>`; device rewritten to `local` when it names this host.
Thread `peer-<name>` matches the peer daemon's, so a specialist has one continuous memory
from either surface (spec decision 6).

**API tool-calling contract** (`openwebui/README.md:52-59`, verbatim): "Model-attached tools
execute server-side only on the **UI/chat** path. The bare OpenAI-compatible endpoint
(`/api/chat/completions`) follows the OpenAI contract instead: pass `"tool_ids":
["agent_dispatch"]` … Without `tool_ids`, the model gets no tool specs at all — a small local
model will then *roleplay* tool results, convincingly and wrongly."

**Cross-device reply capture v0.2** (`openwebui/README.md:74-90`): mailbox daemon on
`/tmp/cc-socks/orchestrator.sock` + sidecar named `orchestrator`; auto-bridge uses the
orchestrator socket as `$CLAUDE_CODE_MESSAGING_SOCKET` so the `-R` carries replies home;
poll `mailbox.jsonl`. Caveat: "a remote **interactive** Claude only answers when it takes a
turn … replies are best-effort for human-driven sessions."

**Mini-agent (3c7fe06):** codex PATH fix for non-interactive ssh (`lib/codex.sh:39-48`,
`$COMM_CODEX_PATH`); `mini-agent` = real codex assistant on `aadarshs-mac-mini-2` confined to
`~/agents/mini`; verified ~5 s round trip. Constraint: "only one reverse-forward can own
`orchestrator.sock` on a remote at a time" (`openwebui/README.md:113-115`).

## 5. PR #9 Switchboard (open, 4 commits, +1533/-0, no reviews)

**Thesis:** "The router answers *what is this name's socket.* The registry answers *who is
this agent.* Neither answers the operator's question: **Who is up, across every device — and
which of them can talk to each other right now?** And: *connect those two.*"

**Surface:** `communicate switchboard [show|serve|json|install|uninstall]`,
`communicate connect <a> <b>`, `disconnect`. Console binds 127.0.0.1:8787.

**Patch matrix:** N×N "row can reach column" with five states — `#` patched, `+` patchable,
`=` direct (same host+user), `~` granted (foreign fleet, negotiated, not self-serve), `x` no
path. Clicking a cell actually patches/unpatches.

**Four design rules it paid for:**
1. *Liveness is never inferred from a file existing.* Provenance ladder
   `probed > reported > reachable-host > declared`. "`communicate directory` reported a peer
   **LIVE for a 26-hour outage** because it only checked that a sidecar file existed."
2. *A socket file is not a listener.* `probe_socket()` connects then `recv(1)`, 0.35 s: EOF ⇒
   dead; timeout ⇒ a live listener holds the line. "Probing measures *time to EOF*."
3. *Patching must not move an agent.* "An agent's **home is where it actually runs**; a patch
   is a line to it."
4. *Actions always re-collect* — a cached snapshot "must never authorize a patch."

**Identity contribution:** "The registry supplies identity; the system supplies measurement."
A registry entry names a **role**; a session name is ephemeral (`/rename`). The operator
declares `aliases:`; the board leads with the durable role name.

**Persistence:** per-agent launchd plists (`KeepAlive`, `RunAtLoad`) on **stable** paths
`/tmp/cc-socks/peer-<name>.sock` instead of pid-derived ones — "peers were children of
whichever session started them, so the board was empty every morning."

**Status:** author-verified across 5 devices / 37 endpoints / 2 fleets; no external review.

## 6. Why communicate messages render differently from tmux injection

A tmux-injected message is **keystrokes into a pty** — staged in the input widget, needing a
submit, rendered as if the human typed it. A communicate message never touches the terminal:
the delivered object is `{"type":"user", …}` written to the session's unix socket, entering
through the **peer-messaging listener**. The runtime constructs a user turn directly, with
`priority` scheduling and a `from` reply address, rendered as an attributed
`<cross-session-message from-name="…">` event. Two consequences of being protocol-level: an
inbound message **resumes an idle/finished session** ("a message is a wake",
`lib/wake.sh:1-4`), which keystrokes into a dead pane cannot do; and the bypass-mode
held-message gate applies — a control point that exists only because the message arrives as a
peer event rather than as typing.

## 7. Honest status

**Verified E2E:** Tier 1 (Claude↔Claude bridge), Tier 3 (Codex-as-peer); Tier 2 (`codex ask`
+ resume) locally. Real tests: `test-codex-peer.sh`, `test-codex.sh`,
`test-orchestrator-reply.sh`. Orchestrator: real GDS file, real tidy3d validation, real
mini-agent generation.

**Aspirational/thin:** replies from human-driven remote sessions (best-effort by
construction); `--on-pr` untested; the peer-fleet registry entry never measured from here;
PR #9 unmerged/unreviewed.

**Dead code:** `_codex_peer_serve` + `__codex-peer-serve`; `read_file_on`;
`deliver(orig_msg_id)` accepted and dropped (no message-id correlation anywhere);
`cc_peer recv/respond` test-only; `usage()` prints a hardcoded sed line-range.

**Known gaps:** no multi-hop; name collisions silently newest-wins; `--as`/`from`
unauthenticated; socket-dir hardening bypassed by the two newest code paths
(`dispatch_tool.py:83-88`, `mesh-demo-agent.sh:25`); wake counts a socket write as a wake;
`directory` liveness lies; `down` over-promises; sidecar-grep requires compact JSON;
`dispatch_tool.py:17` hardcodes a worktree path.

## What this repo got right (must survive any merge)

1. Reuse the incumbent's protocol instead of inventing one — every Claude guard *satisfied*,
   not bypassed.
2. Local-only check + SSH-forwarded unix socket = remote peering with zero agent changes.
3. Identity = `name`, address = socket, sidecar set = routing table.
4. Adapter, not fork: any process becomes a peer by binding a socket + planting a sidecar.
5. name→socket and name→capability as separate layers (router vs registry).
6. Sweep-proofing two correct ways (own live pid; supervised replant).
7. Read-only sandbox default; untrusted text never on a command line.
8. MECHANISM.md — the reverse-engineered spec that makes it all mergeable.
9. PR #9's epistemics (provenance liveness, probe-not-file, actions re-collect).
10. Cross-account by construction.

## Sharp edges / mess

1. Identity is a bare unauthenticated string (spoofable, collidable).
2. Permissions are all-or-nothing SSH.
3. A live bridge is an open injection port for any local process that can reach the socket.
4. Two liveness truths coexist (directory join vs PR #9 probe ladder) — a merge must pick one.
5. `communicate down` over-promises.
6. Inconsistent sandbox posture (orchestrator hardcodes `--auto`).
7. Socket-dir hardening bypassed in newest paths.
8. Single reverse-forward contention on `orchestrator.sock`.
9. Textual/positional fragility (compact-JSON grep, sed ranges, hardcoded paths).
10. No delivery semantics: no ack, no retry, no ordering, no multi-hop.
11. Scale ceilings: N×N matrix, 4-slot semaphore, ever-growing `seen` files.
12. PR #9 reimplements frontmatter parsing already in `lib/directory.sh`.
