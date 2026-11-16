# Prompt mentions

An opt-in filter copies a prompt to registered bus sessions named in the prompt.
Plain `@name` stays with the host application. COM8 mentions use a handler
prefix so later magic commands can share the same shape.

```text
@personal-assistant pls make sure you contribute to this.. eg @com8::dirk-personal:neo4j-ops and @com8::dirk-personal:fleet-ops
```

`@personal-assistant` is ignored. Each `@com8::bus:session` is one delivery on
the configured hub, through the existing `bus send` path. A full example is in
[examples/mentions-prompt.txt](../examples/mentions-prompt.txt).

Nothing installs a hook or changes a hub. Run the filter yourself, or point a
host hook at it.

## Grammar

```text
mention  = "@" handler "::" payload
handler  = [a-z] [a-z0-9_-]{0,31}
```

`com8` is the first handler. An unknown handler (`@other::foo`) is left alone,
the same as `@name` without `::`.

```text
payload  = bus ":" session
bus      = [a-z0-9] [a-z0-9._-]{0,62}
session  = [A-Za-z0-9] [A-Za-z0-9._-]{0,127}
```

Bus names are the broker's bus names: lowercase letters, digits, dots, dashes,
and underscores. Session names are the registered display name, or the exact
agent id. A mention is not glued to the previous word.

Trailing `" ' ) ] } , . ; : ! ?` is sentence punctuation, not part of the name.
`@com8::dirk-personal:wilde-qit-astra"` addresses `wilde-qit-astra`. A dot or
dash inside the name stays. A session whose stored name ends in `.` is not
addressable this way; send to its agent id with `bus send`.

These are not mentions and are not errors:

- `@personal-assistant`
- `com8::dirk-personal:neo4j-ops` without `@`
- `@com8:dirk-personal:neo4j-ops` (one colon)

`::` separates the handler from its payload, so the payload can be
`bus:session` and another handler can take a different payload later.

These are invalid `com8` mentions. They are reported and not delivered:

- `@com8::`
- `@com8::dirk-personal` and `@com8::dirk-personal:`
- a bus that is not a bus name, such as `@com8::DirK:neo4j-ops`
- more than one colon, such as `@com8::dirk-personal:neo4j-ops:extra`

Fenced code blocks and inline backtick spans are not scanned. An invalid
example inside a span is not an error.

## Resolution

The resolver uses the authenticated snapshot from `communicate bus agents`.
It does not open another directory.

| Situation | Result |
|---|---|
| The bus is not in the snapshot | `unknown_bus` |
| No addressable agent on that bus has that name or exact id | `unknown_session` |
| Several addressable agents on that bus share the name | `ambiguous`, with candidate agent ids |
| The token is one agent id on that bus | that agent |
| One matching name | that agent |

A retired registration is not addressable. An offline registration still
resolves: `bus send` already accepts it, and the worker holds the lease until
the session can take the message. Completion does not suggest offline agents.
The same display name on another bus is a different address.

Results keep `id`, `name`, `kind`, `status`, and `last_seen`. Device metadata,
descriptions, invitations, tokens, and sockets are not copied into the result.

## Delivery

Distinct agents each receive one copy, in the order the mentions first appear.
Repeating a mention, or naming an agent and also its id, is one send.

The body is the full prompt. With `--context` or `--context-file`, a blank
line and the context follow the prompt. Every recipient gets that same body.
The sender is the existing bus sender: `self` by default, or `--from`, using
the same resolution as `communicate bus send`.

The router calls `bus.run` for `send`, with the agent id as the target and the
mention's bus as the bus. Membership and allowlists stay in the broker. The
receipt means the broker accepted the message. It does not mean the session
read it.

Claude and Codex are not a second transport:

- A registered Claude session, whether it is in tmux or a Claude daemon that
  published its messaging socket, is delivered by the existing worker as one
  user message on that socket. The filter does not type into the pane.
- A registered Codex session is delivered by the existing worker on that
  thread's queue. The filter does not start another thread and does not type
  into the pane.
- A service adapter uses the existing service queue.

The label on a dry-run row records which of those existing paths the worker
will use: `claude-socket`, `codex-queue`, or `service-bridge`.

Each recipient is independent. A failure after the broker has accepted another
recipient does not retract that acceptance.

Read the prompt from stdin or `--prompt-file`. Do not pass it as an argument.

```sh
com8 mentions route --dry-run --json < examples/mentions-prompt.txt
printf '%s' "$PROMPT" | com8 mentions route --json --from self
com8 mentions route --context-file /path/to/notes.txt < prompt.txt
```

`communicate mentions` is the same command. `--hub` matches `bus --hub`.

## Completion

```sh
com8 mentions complete -- '@com8::'
com8 mentions complete --text -- '@com8::dirk-personal:'
```

| Partial | Suggestions |
|---|---|
| empty, `@`, `com`, `@com8` | handlers, written as `com8::` (a typed `@` is kept) |
| `com8::` or `com8::dirk` | bus names, alphabetically, each ending in `:` |
| `com8::<bus>:` or `com8::<bus>:neo` | `live` and `queueable` sessions on that bus, most recently active first |

Offline and retired agents are omitted. At most 50 sessions are returned.
JSON is the default. `--text` prints one insertion string per line.
`(@com8::dir` keeps the leading parenthesis.

Shell helpers, both opt-in:

```sh
source /path/to/com8/lib/mentions-completion.bash
source /path/to/com8/lib/mentions-completion.zsh
```

## Safety

- The filter does not type over in-progress input. It only calls bus send.
- The broker still enforces membership, grants, and ambiguous names.
- The configured hub is used. Local operation and an enrolled remote hub stay
  distinct. The command does not select `https://bus.nonlocally.org` unless
  that hub is already configured, and it does not change the hosted bus.
- At most 16 distinct mentions are accepted in one prompt. At most 30
  deliveries are accepted in a rolling 60 seconds. The rate file stores
  timestamps only (`mentions-rate.json` in the bus state directory). A corrupt
  file refuses further sends. Dry-run consults the limit and does not record a
  stamp. Over the per-prompt cap, nothing is sent.
- A body larger than the broker message limit (32768 bytes) is refused whole.
- Errors, completion, and dry-run JSON do not include invitation codes, device
  tokens, sockets, or device metadata. Dry-run reports the target list and a
  SHA-256 of the body, not a second copy of the prompt.
- `--dry-run` does not call bus send and does not start the worker.

The CLI exits non-zero when any mention failed. The example hook below exits 0
so a miss does not block the host prompt.

## Opt-in hook

[examples/mentions-user-prompt-submit.sh](../examples/mentions-user-prompt-submit.sh)
reads a Claude Code `UserPromptSubmit` payload, or a raw prompt, and calls
`com8 mentions route`. Setup does not register it.

A Codex wrapper is the same filter, then the client:

```sh
printf '%s' "$PROMPT" | com8 mentions route --json
exec codex "$@"
```

Preview without sending:

```sh
com8 mentions route --dry-run --json < examples/mentions-prompt.txt
```
