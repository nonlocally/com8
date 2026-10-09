# Choose the model behind your agent

A COM8 model connection lets GLM power a Claude Code or Codex CLI session.
The client still supplies its coding tools, project access and COM8 plugin;
the selected model generates its answers and tool calls through the model API.
You can ask that session to investigate code, work with collaborators and
return findings as usual.

Model connections are optional. Choose one when launching a session or
spawning a collaborator. Saving a connection does not change your usual
client launch, account subscription or default model. A model API credential
is separate from a COM8 bus invitation and from your Claude or OpenAI login.

## Get access to a model API

Use the central model API supplied by your OpenWeb operator, with a named,
expiring credential issued to your own account and permitted to use `glm`.
Model credentials can be revoked separately from your account login.

The operator must supply the canonical API base URL and the model IDs your
credential permits. On the central OpenWeb service, the signed-in account's
`GET /api/v1/model-access/models` response supplies `api_base_url` and its
catalog. An ordinary website API key, an upstream GLM provider key and a
central inference credential are different things.

For a connection usable by both clients, the API needs these routes:

| Client or operation | Route |
| --- | --- |
| Model discovery | `GET /v1/models` |
| Codex CLI | `POST /v1/responses` |
| Claude Code CLI | `POST /v1/messages` |

A working Chat Completions endpoint alone does not establish either client's
compatibility. Use the central inference API rather than the website's
`/api/chat/completions` route, which has its own chat processing and model
settings. The central path does not require an OpenWeb browser, terminal or
computer to perform the coding work: that work runs through your local client.

The nonlocally deployment is not a public service: it serves only the accounts
it admits, such as MIT sign-in. Without such an account, use another deployment
you have access to that provides the routes above.

For the nonlocally deployment, use `https://mit.nonlocally.org/v1` with model
`glm`. Claude's compatible base origin is `https://mit.nonlocally.org`, so its
requests reach `/v1/messages`. Sign in and open
[API & clients](https://mit.nonlocally.org/workspaces/developer) to create a
named, expiring model API key permitted to use `glm`. Use the model key beginning
with `nlm_`; the website account API key beginning with `sk-` does not grant model
access. Installing COM8 does not grant model access. The older
`llm.nonlocally.org` route is not the connection to use for this release.

For another deployment, obtain its endpoint and access from its operator.
Availability and supported client/model combinations depend on that deployment.

## Save a connection privately

Install your selected coding clients and COM8 integrations through
[`com8 setup`](INSTALL.md). Setup can offer a model connection as an optional
choice; keeping your existing model setup is valid too.

Your agent can run the configuration commands once you provide the endpoint
and a private credential file. Keep the key out of chat, command arguments,
shell history, screenshots and source control. The input file must be an
absolute path to a regular file owned by your account, without symlinks and
with no access for other users, normally mode `0600`.

```sh
com8 model add research-glm \
  --base-url https://mit.nonlocally.org/v1 \
  --anthropic-base-url https://mit.nonlocally.org \
  --model glm \
  --key-file /absolute/private/model-key
```

Use the canonical `/v1` base for Codex. Claude needs the compatible origin
without `/v1`; it appends `/v1/messages`. Include `--anthropic-base-url` when
you intend to use Claude Code. Both URLs must refer to the same origin.

Set `--context-window N` only from the operator's verified serving limit.
A model's advertised maximum may exceed the context available on this endpoint.

For a secret supplied on stdin:

```sh
com8 model add research-glm \
  --base-url https://mit.nonlocally.org/v1 \
  --anthropic-base-url https://mit.nonlocally.org \
  --model glm \
  --key-stdin < /absolute/private/model-key
```

Use one credential input method. Do not put the literal key into an `echo`
command. `--dry-run` previews the connection without reading or storing the
key; add `--json` for a structured result. Connection names use lowercase
letters, digits, hyphens or underscores.

COM8 copies the credential into its private model configuration directory,
normally `~/.config/com8/models`. It respects `XDG_CONFIG_HOME`; an explicit
`COM8_MODEL_CONFIG` selects another absolute configuration directory. Newly
created directories are private and configuration files are readable only by
their owner. The original input file remains yours to manage.

## Check access, then choose a client

```sh
com8 model list --json
com8 model doctor research-glm --json
```

The model doctor makes an authenticated catalog request and checks that `glm`
is listed. It does not generate text or run a model task. Its result explicitly
reports `inference_tested: false`; catalog access does not prove streaming,
tool use, model readiness or coding quality.

Launch the selected connection explicitly:

```sh
com8 model run research-glm --cli codex
com8 model run research-glm --cli claude
```

Normal client arguments go after `--`. Arguments that would replace the
selected model, provider profile or settings are refused. The client's normal
permission choices still apply; selecting GLM does not grant additional tool
or terminal permissions.

COM8 uses a launch-specific client configuration and credential environment.
It preserves the installed COM8 integration and leaves the usual client
configuration in place. The selected API receives the conversation and tool
results the client sends, so choose an endpoint you trust with that project.

Model launches require **Codex CLI 0.156 or later**. COM8 launches it with
`--no-daemon` so its selected credential reaches that execution without changing
an existing shared server. To resume a GLM session, use its exact session ID
through the same connection:

```sh
com8 model run research-glm --cli codex -- resume SESSION_ID
```

The temporary launch profile is removed when the client exits. An ordinary
shared-server resume or queued turn is not a qualified substitute for this
launcher. Background, remote and cloud launch modes that bypass the selected
local connection are refused. Claude's bare mode is also incompatible with
this plugin-preserving bearer-authentication path. Existing subscription-account
panes must use a fresh COM8 seat so account rotation cannot replace the selected
model.

For a collaborator, ask your agent:

> Create a Codex collaborator called investigator using my research-glm
> connection. Have it inspect the failing tests and send back its findings.

The corresponding operation is:

```sh
com8 spawn investigator --cli codex --model-connection research-glm --cwd "$PWD"
```

The identity name and model connection name are separate: `investigator` is
the collaborator; `research-glm` selects the API, credential and model for
that execution. The same connection can power a Claude collaborator when its
Messages route is supported.

## Replace or remove access

```sh
com8 model remove research-glm --json
```

Removal deletes the unchanged files COM8 owns for that saved connection. It
does not revoke the server credential or stop already-running clients. Revoke
the key through the issuing service when you intend to end its access, and
close the sessions using it. To change an endpoint or rotate a key, remove the
old saved connection, then add it again with the new private credential.

If connection files were changed outside COM8, the command refuses to use or
remove them. Inspect that change before replacing them; do not bypass the
ownership check by copying keys into ordinary client configuration.

## What a successful setup establishes

These commands target the Claude Code CLI and Codex CLI. They do not configure
desktop apps. Claude Code with a non-Claude model requires deployment-specific
compatibility qualification; it is not vendor certification.

Administrator-managed client policy may take precedence over a launch profile.
COM8 preserves that policy; confirm the actual selected endpoint and model in
such environments before treating a run as qualified.

For a new endpoint, verify an actual read/edit/test task with each intended
client, including a tool result returned to the model, streamed completion and
cancellation. Match those calls to the issuing account's usage records where
available. Test longer sessions before promising compaction or resume support.
An unavailable model should be reported as unavailable, without silently
substituting your usual paid provider or another model.
