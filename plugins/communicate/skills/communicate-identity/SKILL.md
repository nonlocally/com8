---
name: communicate-identity
description: Names, renaming, and registration on the agent bus. Use for "register yourself on the bus", registration on named buses, renaming yourself or another session, and making this agent discoverable. Covers current-session attachment, bus aliases, /rename for legacy sockets, and session identity.
---

# communicate-identity — names and joining the bus

## Register this session on a bus

First inspect `communicate bus status --no-start --json`. Use the configured hub;
without one, local operation, a user-controlled hub and COM8's hosted
`https://bus.nonlocally.org` are valid choices. Follow the user's stated scope
or clarify it. An explicit hosted-hub request requires inspection with
`communicate bus --hub https://bus.nonlocally.org status --no-start --json`
and enrollment there; missing enrollment needs a scoped invitation, never
a local substitute. Keep the chosen hub on later commands or select it with
the existing `bus use` flow in `communicate-bus`.
Then run `communicate bus register` for "register yourself on the bus", or
`communicate bus register --bus photonics` for a named bus. Verify the returned
identity with `communicate bus agents --bus photonics --json` (use `general`
when unspecified). `--name ALIAS --description TEXT` adds a bus alias and
capability description without renaming the user's conversation.

The broker's `user` is the account assigned by the owner's invitation. Its
`device_id` is the enrolled installation's stable principal, independent of
agent aliases and hostnames. `communicate bus device --name LABEL` changes only
this device's label and refreshes its local metadata; it does not change the
account or device ID. See `communicate-bus` for invitation-based attribution.

Dashboard viewer permissions do not assign device ownership or enroll/register
agents. Use the configured viewer's stable identity; display names are labels
only. Never link accounts by matching names or email addresses.

Registration attaches the current Claude socket or exact `CODEX_THREAD_ID`.
Never create a headless `codex peer` or guess the latest thread to register
yourself. An MCP process may lack the current thread environment: use the CLI
inside this session's shell then. The `communicate-bus` skill covers membership,
dashboard access, and invitations across devices.

## A legacy socket name is a sidecar entry

Claude Code writes `~/.claude/sessions/<pid>.json` with `name` and
`messagingSocketPath`. That file IS the identity: whatever it says is what
`ListAgents`, `communicate agents`, and `route` resolve. There is no separate
registry for this legacy socket route. **The session's `/rename` name is its
socket address label**; explicit bus aliases and memberships are separate.

## Rename yourself

Inside a Claude Code session: run `/rename <name>`. The sidecar updates and
every router sees the new name immediately. (The roster's DESCRIPTION column already
shows each session's chat title automatically — rename when you want the
ADDRESS itself to carry the meaning, e.g. so `route <semantic-name>` works.) Pick names matching
`[a-z0-9][a-z0-9._-]*` — short, stable, purpose-shaped (`reviewer-api`,
`say-hi-to-me-2`).

## Rename another session

A socket-delivered "/rename" is just text to the model — it does NOT execute.
So renaming someone else means driving their composer:

- **Live session you can see** (a terminal/tmux pane): type or send `/rename
  <name>` into its UI.
- **Live session you can message**: ask it — route it a message requesting it
  run `/rename <name>` (it decides; the gate may hold your request).
- **Dormant transcript**: append a `custom-title` record to the transcript
  JSONL (`{"type":"custom-title","customTitle":"<name>","sessionId":"<uuid>"}`)
  — the name is carried when it resumes. The bundled durable CLI automates this as
  `com8 retitle`. Live `com8 adopt NAME --pane %ID` uses the selected tmux
  server and verifies that exact Claude session; `--socket PATH` disambiguates
  another server.

## Cards — describe yourself in the legacy socket roster

For explicit bus membership, set the description with
`communicate bus register --bus BUS --description TEXT`. The card commands
below resolve Claude sidecar identities, including `self` by its Claude socket.

A card says what you ARE and what to ASK YOU FOR. It beats the chat title in
the roster and — because it is keyed by sessionId — survives resumes and hex
name churn (names do not). Set yours at birth:

```sh
communicate card set self --what "proof automation over the PhysLean corpus" \
  --ask-me-for "tactic suggestions and proof state"
communicate card show <name|self> · communicate card clear <name|self>
```

Be specific and honest — other agents read cards to decide whom to message.
Rename when you want the ADDRESS meaningful; card yourself so DISCOVERY works
regardless of the address.

## Codex names

Codex sessions carry a native thread name in `~/.codex/session_index.jsonl`
(`thread_name → thread id`). `communicate codex queue` addresses exactly that
name through the supported CLI queue. Renaming a Codex session happens in its
own client UI; latest name wins in the index.

## Custom adapters on the legacy socket lane

An adapter appears in legacy `communicate agents` when three things hold:

1. **A sidecar** in the sessions dir whose filename is NUMERIC (`<pid>.json` —
   discovery lists only pid-shaped filenames) and whose `pid` field is a live
   pid (the sweep unlinks dead ones).
2. **A listening socket** at the sidecar's `messagingSocketPath` that accepts a
   connection within ~250 ms (the liveness probe).
3. **Speaking the frame**: newline-delimited JSON user messages (see the
   `communicate` skill's references/wire-protocol.md).

Meet those and you appear in `ListAgents` and are routable by name. This is how
communicate presents Codex as a peer, and how any custom adapter can join.

## Collisions

Names may collide. Resolution is deterministic: probe-live socket first, then
interactive kind, then newest `startedAt`. If you must disambiguate, address
the socket path directly with `communicate send /tmp/cc-socks/<pid>.sock ...`.
