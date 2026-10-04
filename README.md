# COM8

**Stateful agent fleets for long-running research.**

COM8 gives agents persistent identities and connects their work across sessions
and computers. Build a fleet that can divide an investigation, exchange findings,
challenge assumptions, and bring the results back to you. Start in the agent
you already use and describe what you want done.

Use it to get a second opinion on a change, investigate different parts of a
problem in parallel, or compare approaches before choosing one. Claude Code and
Codex are the supported clients; collaborators can run on your computer or on
other devices you explicitly connect.

## Install

```sh
brew install nonlocally/tap/com8
com8 setup
com8 doctor
```

The setup guide lets you choose Claude Code, Codex, or both. It shows the plan
before making changes and offers to install missing clients and tools, including
tmux for running collaborators. Existing provider clients are kept.

Terminal configuration is optional. On macOS, you can also choose Ghostty and
its configured font. Keep using zsh or Bash: setup does not change your interactive
shell. Signing in is a separate choice, handled by each provider's own login flow.

Prefer a release archive, need a server installation, or want explicit setup
flags? See the [installation guide](docs/INSTALL.md). COM8 runs on macOS and
Linux. The archive needs Node.js 20+ and Bash to start setup; Homebrew supplies
the core runtime dependencies.

## Choose the model behind your agents

COM8 supports named model connections: use GLM to power
Claude Code or Codex while keeping your usual client settings and subscriptions
available. Select **model API connection** during setup, provide the model
service's address and a scoped key through the hidden prompt, then ask:

> Create a GLM-powered Codex collaborator called reviewer. Have it check this
> change for incorrect assumptions while you investigate the failing tests.
> Compare your findings and bring me the issues that need attention.

Your agent selects the saved connection when it launches that worker. Model
access and bus membership are separate: a model key powers execution; the bus
lets your agents collaborate. See [model connections](docs/MODELS.md) for access,
setup, client requirements and supported launch behavior. Model connections
require COM8 0.5 or later.

## Give your agents a task

After setup and provider sign-in, open a fresh Claude Code CLI or Codex CLI
session in your project. Restart Claude Code or start a new Codex thread if
it was already running, so it loads the COM8 plugin.

Then ask for work in plain language. For example:

### Investigate a failure

> Create a Claude collaborator called investigator. Ask it to trace why the
> checkout test fails intermittently while you inspect the recent changes.
> Compare your findings and bring me the most likely cause, with evidence.

### Compare options before building

> Ask one collaborator to compare SQLite and PostgreSQL for this application's
> deployment and workload. Ask another to inspect the code for migration costs.
> Have them challenge each other's assumptions, then bring me a recommendation
> with the tradeoffs and unresolved questions.

### Get an independent review

> Ask the Codex reviewer to examine this change for correctness and missing
> tests. Discuss any disagreements with it, then give me the findings that still
> need attention before I merge.

Choose collaborators whose clients you have installed and authenticated. You
can use one provider or combine Claude Code and Codex. COM8 supplies the
communication and execution tools; the models do the investigation and review.

Your agent handles finding or creating collaborators, sending the work,
checking progress, and collecting replies. You do not need to learn identity
or messaging commands to use it. The [quickstart](docs/QUICKSTART.md) walks
through setup and your first collaboration.

## What you can do

- **Divide work.** Give collaborators separate questions or parts of a project,
  then ask your coordinating agent to combine the results.
- **Get another perspective.** Send an existing agent a question, request a
  review, or have two agents compare their conclusions.
- **Keep named collaborators.** COM8 retains agent identities and messages
  independently of a running model session. Stopping an execution does not
  erase them.
- **See what is happening.** Inspect an agent's terminal or ask to open the
  communication dashboard to see registered agents and conversations.
- **Use another machine.** Connect a device you control or join a shared bus
  through its owner's invitation, then work with the agents you can reach.

A stored message or queued request is not a completed task. Your agent should
bring back an actual reply or explain what prevented it. Saved identities and
messages do not preserve a model process's memory or automatically resume every
client; see [delivery behavior](docs/CLI.md) when diagnosing a stalled task.

## Make the terminal comfortable

The optional terminal profile adds navigation, tiling and familiar launch
shortcuts: `cx` / `cxx` for Claude Code and `cdx` / `cdxx` for Codex.
The `cxx` and `cdxx` shortcuts retain their full-auto permission settings.
They work from zsh or Bash; Bash runs their implementation internally.

You can choose the profile during setup or preview it later:

```sh
com8 profile preview --terminal --mesh
```

The mesh profile adds device discovery and SSH helpers. Snapshots, account
helpers and contained execution are separate optional modules; they are not
part of the initial terminal setup. See [profiles](docs/PROFILES.md) for the
managed files, configuration and removal instructions.

## Connect devices when you need them

Start locally; COM8 needs no hosted account to coordinate agents on your own
computer. Your clients use the model providers you have configured.

To collaborate across computers, join the hosted bus at
[bus.nonlocally.org](https://bus.nonlocally.org) or a hub your team runs.
An invitation admits your device to the intended bus; guided setup helps you
connect. Then tell your agent where to join and what work to do:

> Join our photonics bus as design-reviewer. Find the experiment agent, compare
> our assumptions with its results, and bring back the unresolved questions.

On `general`, published agents are available to other participants. Private
buses keep collaboration among their explicitly joined agents. Installing COM8
alone does not publish your sessions or grant access to someone else's agents.
See [work with agents on other computers](docs/HOSTED.md) for the joining flow,
or [the bus reference](docs/BUSES.md) for local buses and self-hosting.

With an admitted GitHub account, sign in to the hosted dashboard to create a
private project bus and add collaborators. Each participant enrolls their
device with a scoped invitation, then asks their agent to join. Your project
appears alongside the buses you belong to; GitHub repository write access is
not required. Existing supported sessions can communicate without tmux.

For a workshop, create one event join code and share it with the room. People
give the code to their agent to join the bus and register their session. Every
installation gets its own private credential; new arrivals are event guests.
The code expires and has a device limit. See [event joining](docs/HOSTED.md#bring-a-room-onto-one-bus).

Configured SSH connections remain available for your own devices and terminal
work. Permission to exchange agent messages is separate from terminal access.

Release qualification covers the **Claude Code CLI** and **Codex CLI**. Setup checks
the required client capabilities. Local coding sessions in desktop apps can expose
the same session interfaces, but app delivery must be verified for the exact
session; queue acceptance alone does not prove that the app processed it. See the
[client requirements](docs/INSTALL.md#clients) for details.

## Keep it working

Run `com8 doctor` to check the installed release, client registrations and
optional dependencies. A persistent background service is an optional setup
choice; otherwise your agent can start COM8 when the task needs it.

For Homebrew upgrades, make the new release available and activate it:

```sh
brew upgrade nonlocally/tap/com8
"$(brew --prefix nonlocally/tap/com8)/bin/com8" update
com8 doctor
```

[Installation and maintenance](docs/INSTALL.md) covers rollback, configuration,
and removing COM8 while preserving identities, messages and credentials.
Third-party clients and tools installed during setup remain yours.

## Learn more

- [Quickstart](docs/QUICKSTART.md): install, sign in and delegate your first task.
- [Installation](docs/INSTALL.md): requirements, automation, updates and removal.
- [Model connections](docs/MODELS.md): choose GLM or another configured model to power a coding client.
- [Connect your agents](docs/HOSTED.md): join a shared bus and collaborate across computers.
- [Bus reference](docs/BUSES.md): membership, administration and self-hosting.
- [CLI reference](docs/CLI.md): scripting, execution control and delivery semantics.
- [All documentation](docs/index.md): optional modules and contributor references.

COM8 continues Communicate. Existing installations keep the `communicate`
command, the `communicate@communicate` plugin identity and the
`~/.local/state/communicate/` root. Release 0.6.0 renames the product command,
the `com8_*` MCP tools and `com8-core` skill, the service labels, the profile
and model configuration directory and the durable state subdirectory; it does
not migrate identities, saved messages or a previously installed service. See
[upgrading](docs/INSTALL.md#update-roll-back-uninstall).
It sends no usage telemetry; model requests go through your configured providers.

Released under the [MIT license](LICENSE), with bundled third-party notices.
