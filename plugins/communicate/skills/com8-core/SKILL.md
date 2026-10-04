---
name: com8-core
description: Create persistent COM8 agents, select a configured model such as GLM to power Claude Code or Codex, give workers tasks and collect their answers. Use for creating a researcher, asking a worker to investigate, saved agent messages, or explicit terminal-seat control. Existing sessions and registered buses use their own routes.
---

# COM8 agents, messages and execution

The user states the outcome; you perform the COM8 operations with MCP tools or
the installed CLI. For example, "Create a Claude agent called researcher,
investigate the flaky test, and bring me its answer" is a request to complete
that workflow, not to hand the user a list of claim/start/send commands.

COM8 keeps existing mechanisms behind three explicit command paths. Select the
path that owns the target; a matching label in another path is not a substitute.

| Target | Commands | Meaning |
| --- | --- | --- |
| Durable identity | `com8 agents`, `claim`, `send`, `ask`, `inbox` | A persistent name with saved messages and optional current execution. |
| Existing native session | `com8 native agents`, `route`, `ask`, `codex queue` | An exact supported Claude Code socket or Codex queue target; no pane required. |
| Registered bus agent | `com8 bus status`, `register`, `agents`, `send`, `reply` | Exact registrations under the selected hub's membership rules. |
| Terminal seat | `com8 seat spawn`, `read`, `send`, `state`, `bind` | Explicit terminal control on the configured tmux server or granted remote link. |

`communicate com8` is the kernel's subcommand spelling of `com8`; `communicate`
and `communicate bus` keep their own meanings. The previous release's command,
MCP tool and skill names are not aliased. The plugin identifier remains
`communicate@communicate`.
MCP tools `com8_*` expose durable identities/seats; existing `bus_*` and native
tools retain their meanings. `com8 profile` is optional terminal/mesh setup;
do not alter shell or desktop settings to satisfy a messaging request.

## Start with the intended identity

Inspect `com8 status --json` before assuming the local daemon is available.
If a requested local identity or agent operation needs it, start the daemon
with `com8_start` / `com8 start`; no separate user command is needed. Inspection
alone does not start it, and neither action joins a remote bus.
`com8 agents --json` lists durable identities and their observed bindings.

## Complete an agent task

1. Inspect daemon status and the durable roster. Reuse an intended existing
   identity when appropriate; do not overwrite an unrelated agent with the same
   name. Start the local daemon if needed for the requested work.
2. Use `com8_spawn` with the requested name, provider and project directory.
   If the user selected GLM or another model connection, inspect
   `com8_model_list` / `com8 model list --json` and pass its exact name as
   `model_connection` (`--model-connection` in the CLI). This chooses the model
   powering the coding client itself, not a consultation tool or a bus agent.
   If the requested connection is missing, explain the setup requirement;
   do not fall back to a paid subscription or another model.
   The seat driver creates its session when needed, on the daemon's configured
   tmux server (default: named server `com8`, session `com8-seats`). It does not
   require the human to start tmux or put their current terminal into a pane.
   Keep that server selection; do not replace it with the caller's ambient pane.
   Missing tmux/provider tools or provider sign-in are setup issues to report,
   not reasons to claim a model is running.
3. For Claude, inspect the spawn result's `adopted` flag and the identity's
   reported `route`, `seat` and `surface`. A usable native route is live and
   unambiguous; `ok:true` from spawning alone is insufficient. If adoption is
   incomplete, inspect that returned seat for startup, trust or login prompts.
   Resolve only actions already authorized; do not substitute another session
   or automatically enable keyboard relay. Codex spawning does not bind
   its durable name to a queue. Establish the exact thread belonging to the
   returned seat, then use its verified native or bus queue and reply mechanism.
   Do not guess from the newest transcript or shared cwd; report the blocker if
   that exact thread cannot be established.
4. For a verified durable recipient, claim an unused per-task sender name with
   `com8_claim` when a reply destination is needed. Use `com8_ask` with that
   sender, the actual task and a bounded timeout. Tell the worker to answer using
   the supplied exact reply token (`com8_reply` / `com8 reply`), which is included
   in the delivered request. For a native or bus target, use that route's own
   reply mechanism; do not use a spawned Codex agent's durable name as though
   it were automatically bound to the Codex queue.
5. Bring the returned answer to the user. A stored request, queued turn or idle
   screen is not a completed investigation. If blocked or timed out, report the
   measured state and what remains; do not invent an answer or repeatedly send
   the same task. Leave identities and executions intact unless cleanup was
   requested.

The command examples below are for you to execute as needed. The user's normal
workflow is setup, provider sign-in, and a natural-language request in a fresh
client session.

```sh
com8 claim reviewer
com8 spawn worker --cli claude --cwd /path/to/project
com8 spawn checker --cli codex --cwd /path/to/project
```

Claim creates an address without launching a model. Spawn creates a claim and
an execution using the configured provider; authentication remains the user's.
Never spawn a replacement to satisfy a request to register an existing session.
Do not infer the current session from a newest transcript or shared cwd.

## Choosing a model

Existing model connections are private local settings, separate from identities
and bus registration. A request such as "Have a GLM-powered Codex worker review
this change" selects the configured connection for that new execution:

```sh
com8 model list --json
com8 model doctor glm --json
com8 spawn checker --cli codex --model-connection glm --cwd /path/to/project --json
```

`doctor` checks authenticated model discovery only; `inference_tested:false`
must not be reported as a successful coding run. Verify the actual worker's
task and reply normally. Per-launch selection preserves ordinary client defaults
and logins. A running session's model is not changed by sending it a message.

If configuration is requested, use `com8 setup --model` for the human's hidden
key prompt, or `com8 model add` with an explicitly supplied private key file or
stdin. Never ask for a key in chat, read an unrelated credential store, put keys
in arguments or forward them to another agent. Use the model service's scoped
key, not a bus invitation or provider/master credential. Connection metadata is
safe to list; keys are not. CLI support does not establish desktop-app support.

For a live Claude composer in the caller's tmux server, `com8 adopt NAME --pane
%ID` submits `/rename` using the existing seat driver and confirms that pane's
session sidecar. Use `--socket PATH` for another exact server. This renames the
native session; claiming a durable address is a separate `com8 claim NAME`.
`com8 retitle TRANSCRIPT_OR_UUID NAME` changes a dormant Claude transcript's
title for resume. A restart is process supervision, not proof of restored
conversation state. Surface that distinction when reporting recovery.

## Send and receive

```sh
com8 send reviewer --from worker -- "Review this result"
com8 ask reviewer --from worker --timeout 90 --json -- "What should change?"
com8 inbox reviewer
com8 wait reviewer --timeout 60 --json
com8 reply EXACT_RETURN_TOKEN --from reviewer -- "Here is my answer"
```

Use a claimed sender so replies have a durable destination. Preserve the exact
return token. Existing ask also supports a best-effort natural-reply fallback;
do not describe such a match as explicitly correlated. Timeout means the wait
ended, not that the request was never stored or will never receive a reply.

Stored, submitted, and replied are different evidence. Messages can persist while
its execution is absent. A socket write or Codex queue receipt does not prove
the model consumed the turn. Reading terminal output does not prove a reply.
Follow the adapter's reported state, including held, unavailable and unknown.

## Execution control is explicit

```sh
com8 seat spawn 'bash --norc --noprofile' --cwd /path/to/project
com8 seat read %ID
com8 seat send %ID 'a command requested by the user'
com8 seat state %ID
com8 seat bind %ID reviewer
```

Panes are scoped to their device and tmux server lifetime. A pane ID is not a
globally durable identity, an authenticated principal, or a sandbox boundary.
Ordinary shell input can execute commands. Do not deliver agent messages into a
shell just because native messaging is unavailable. Keyboard relay requires the
existing explicit `seat bind --relay` opt-in and agent-surface checks.

`com8 seat interrupt` sends Escape; `seat kill` terminates a pane. `com8 release`
releases a durable claim, and `com8 stop` stops the daemon. These are distinct
from deleting saved messages and state. Do not use them interchangeably.

## Other devices and the bus

Existing durable device links use `com8 link DEVICE --addr USER@HOST`; paired
devices use `com8 pair USER@HOST`. Provisioning and SSH access are intentional
operations, not consequences of discovering a hostname. Remote seat control is
separately granted with the existing `--allow-seats` option.

Before bus discovery or registration, inspect `com8 bus status --no-start --json`
and use the configured hub. For an explicit hosted request, inspect
`com8 bus --hub https://bus.nonlocally.org status --no-start --json`; it requires
enrollment there, never a local fallback. Use `communicate-bus` for the existing
connect/use flow, local or self-hosted buses, graph and human inbox.
A bus registration is not automatically a durable COM8 claim.
Bus replies must retain their hub/registration/message identity; durable COM8
replies must retain their return token. Remote enrollment, publication and
compute-control permission remain distinct.
