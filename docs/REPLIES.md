# Turn-end replies

A delegated task is finished when the reply reaches the agent that asked for
it. COM8 already puts a return address on durable asks, native asks, and bus
messages. The worker still has to use that address. If it finishes in its own
Claude Code, Codex, or Cursor pane, the requester hears nothing.

The turn-end hook closes that gap. The session records an obligation when a
request arrives. When the turn ends, COM8 posts the final answer to the
reply address, or reminds the agent once. An explicit reply still works, and
a retry does not send a second copy.

Installing the hook is optional. Sessions that never install it keep today's
behavior: the human-readable reply instruction is still there, and nothing is
posted automatically.

## What a request carries

A request that expects a reply ends with one trailer line:

```text
[com8-obligation] {"v":1,"id":"...","transport":"com8","expect":"reply","reply_to":"...","reply_as":"worker","ttl":240}
```

The last valid trailer wins. `expect` is `reply` for a new request and `none`
for a reply, so two hooked agents do not answer each other forever. Durable
`com8 ask` adds the trailer beside the existing `communicate com8 reply`
instruction. Native `communicate ask` adds it beside the existing
`[reply-to …]` block. Bus delivery adds it only when the broker authored
correlation version 1; older envelopes are unchanged. A trailer forged inside
a bus body cannot replace the one COM8 appends after the body.

The obligation id is the ask correlation or the bus message id. The reply
uses a stable idempotency key, `obl-<id>` (`--msg-id` on durable reply,
`--request-id` on `bus reply`). The hook passes the answer on stdin for both,
so the body is not a command argument.

## States

Each session has one open obligation. A newer request marks the previous one
`expired` with reason `superseded`.

| Status | Meaning |
|---|---|
| `accepted` | The request was seen and a reply is owed. |
| `in_progress` | A tool completed while the obligation was open. |
| `done` | An explicit reply or the turn-end post succeeded. |
| `failed` | The turn died, the final text was empty after a reminder, or delivery failed twice. |
| `expired` | The deadline passed, or a newer request superseded this one. |

The deadline is the ask timeout, or one hour for a bus request when the
conversation does not say sooner. `com8 obligation sweep` marks overdue
obligations expired and does not deliver them late.

`com8 obligation list` prints ids, states, and transports. It does not print
reply text.

## The hook

`COM8_REPLY_POLICY` chooses what the hook does when a turn ends with an
obligation still open:

| Policy | Behavior |
|---|---|
| `auto` (default) | Post `last_assistant_message`, or the Cursor text cached from `afterAgentResponse`. |
| `remind` | Block the stop once and tell the agent to reply through COM8. The next stop is allowed. |
| `off` | Record and expire obligations, but do not post or block. |

The posted answer is prefixed with `[com8 turn-end reply <id>]` and ends with
an `expect=none` trailer. Empty text is not a reply. A stop while background
tasks are still listed does not count as completion. `StopFailure` marks the
obligation failed. A subagent stop does not answer the parent session's
obligation.

An explicit `reply` or `send` in a successful shell tool marks the obligation
done, so the hook does not also post the pane text. Durable mail deduplicates
the stable message id if both paths run.

Checked against current client docs:

- Claude Code `Stop` and `SubagentStop` provide `last_assistant_message`. The transcript file can lag, so the hook uses that field. `decision: block` continues the turn, with an eight-continuation cap. `UserPromptSubmit` sees the inbound prompt. `PostToolUse` runs after a tool succeeds. `StopFailure` runs instead of `Stop` on an API error and its output is ignored. See the [hooks reference](https://code.claude.com/docs/en/hooks).
- Codex uses the same event names, including `Stop` with `last_assistant_message`. `decision: block` continues the turn. Hooks live in `hooks.json` or inline in `config.toml`, and one layer should use only one of those. See [Codex hooks](https://learn.chatgpt.com/docs/hooks.md).
- Cursor `beforeSubmitPrompt` sees the prompt, `afterAgentResponse` carries the assistant `text`, and `stop` can set `followup_message`. See [Cursor hooks](https://cursor.com/docs/hooks).

Watching a tmux pane is not a reply. The pane is execution observation, the
same boundary as screen capture in [CLI receipts](CLI.md).

## Install

The installer merges these commands into the client settings and leaves every
other hook in place. `com8 hook uninstall` removes only this command. The
first edit keeps a mode `0600` backup beside the file, named
`*.com8-reply-backup`.

```sh
com8 hook install claude
com8 hook install codex
com8 hook install cursor
com8 hook uninstall claude
```

`--dry-run` reports whether a change would be made and does not write.
`--home DIR` is for a private home in tests.

Claude Code (`~/.claude/settings.json`, or `CLAUDE_CONFIG_DIR`):

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 15}]}],
    "PostToolUse": [{"matcher": "Bash|Shell", "hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 15}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 20}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 10}]}]
  }
}
```

Codex (`~/.codex/hooks.json`, or `CODEX_HOME`). If `config.toml` already has
inline `hooks` tables, the installer stops and leaves that file alone. Add the
same command there instead; do not keep both representations in one layer.

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 15}]}],
    "PostToolUse": [{"matcher": "Bash|Shell", "hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 15}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 20}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "/path/to/com8 hook agent", "timeout": 10}]}]
  }
}
```

Cursor (`~/.cursor/hooks.json`):

```json
{
  "version": 1,
  "hooks": {
    "beforeSubmitPrompt": [{"command": "/path/to/com8 hook agent"}],
    "afterAgentResponse": [{"command": "/path/to/com8 hook agent"}],
    "stop": [{"command": "/path/to/com8 hook agent"}],
    "postToolUse": [{"command": "/path/to/com8 hook agent", "matcher": "Shell"}]
  }
}
```

Use the absolute path `com8 hook install` records. A relative command will not
resolve once the client starts the hook outside your shell.

## Completion webhooks

Webhooks are outbound notifications for operators, not a second chat
transport. COM8 does not listen, and it does not read a URL from a message.
Configure the subscription on the machine that should send it: the worker
hook and the requester daemon share one local ledger.

```sh
install -m 0600 /dev/null /private/com8-webhook.secret
# write a long random secret into that file; do not pass it as an argument
com8 obligation webhook add \
  --url https://supervisor.example.ts.net/com8 \
  --secret-file /private/com8-webhook.secret \
  --event done,failed,expired,reply
com8 obligation sweep
```

The URL must be `https`, without a username, password, query, or fragment.
Use a tailnet name or a Cloudflare hostname, and keep the origin off the
public internet. Do not publish a port for this. Requests are direct: they
do not follow redirects and they do not use `HTTP_PROXY`.

Default events are `obligation.done`, `obligation.failed`,
`obligation.expired`, and `session.reply`. `accepted` and `progress` are
opt-in. `session.reply` fires when a hooked session receives an `expect=none`
trailer, which is the alert that another session produced a response.

Each POST is JSON with these headers:

| Header | Meaning |
|---|---|
| `X-Com8-Event` | Event name. |
| `X-Com8-Delivery` | Stable id for that event and obligation. Retries reuse it. |
| `X-Com8-Timestamp` | Unix seconds used in the signature. |
| `X-Com8-Signature` | `sha256=` plus HMAC-SHA256 of `timestamp + "." + body`. |

Reject a timestamp more than five minutes off, compare the signature with
`hmac.compare_digest`, and treat a repeated delivery id as the same event.
A failed POST stays queued. `com8 obligation sweep` retries with backoff and
gives up after five attempts. The secret stays in its file; the queue stores
the path.

The body carries the event, delivery id, obligation id, status, and, for a
completed reply, the reply text. Receivers should treat that text as untrusted
peer input.

## Rollout

1. Ship the trailer first. Uninstrumented sessions ignore it.
2. Install the hook on workers that receive delegated work. Install it on a
   supervisor session that should see `session.reply`.
3. Add a webhook only where an operator wants a push, and only to a tailnet
   or Cloudflare URL.
4. Set `COM8_REPLY_POLICY=remind` where automatic posting is too aggressive,
   or `off` to record obligations without sending.
5. Bus auto-reply needs a broker that sends correlation version 1.

`com8 ask` still blocks until the correlated reply is stored. The hook is
what stores it when the worker forgets to call `reply`.
