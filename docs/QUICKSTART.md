# Quickstart

Install COM8, sign in to the coding clients you want to use, and give your agent
a task to coordinate. Start with a review or a research comparison so you can
see collaborators exchange findings and bring back an answer.

## 1. Install and choose your tools

```sh
brew install nonlocally/tap/com8
com8 setup
com8 doctor
```

Choose Claude Code, Codex, or both. Setup shows its plan and offers missing
selected clients and dependencies before asking you to apply it. Selecting a
client includes tmux for running collaborators; terminal configuration remains
optional.

On macOS, Ghostty is a separate choice. The optional terminal shortcuts work
from zsh or Bash without changing your interactive shell. The background service
is optional too; your agent can start COM8 when a task needs it.

For Linux, an archive installation, or explicit noninteractive selections, use
the [installation guide](INSTALL.md). An archive needs Node.js 20+ and Bash to
start setup. Keep the downloaded archive for recovery.

`doctor` reports the installed release and client registrations. Unselected
optional tools may be unavailable; that does not make the installation broken.

## 2. Sign in and open a fresh session

If you selected provider login during setup, complete that provider's own flow.
Otherwise, sign in through Claude Code or Codex normally. Installing the clients
does not sign you in, and COM8 does not collect your provider credentials.

Open a fresh Claude Code CLI or Codex CLI session in your project. Restart
Claude Code or start a new Codex thread if it was running during setup, so it
loads the plugin.

With the terminal profile, `cx` and `cdx` are convenient launch commands;
`cxx` and `cdxx` retain their full-auto permission settings. You can also
launch the clients directly.

To add a client later, run `com8 setup --guided` and select it. See
[client requirements](INSTALL.md#clients) if setup reports an unsupported
version or capability.

## 3. Delegate a concrete task

In your agent conversation, try:

> Create a Claude collaborator called investigator. Ask it to trace why the
> checkout test fails intermittently while you inspect the recent changes.
> Compare your findings and bring me the most likely cause, with evidence.

Use a client you have installed and authenticated. Your agent handles checking
readiness, finding or creating collaborators, sending the task, and collecting
the answer. You do not need to type the underlying communication commands.

For a research comparison:

> Have one collaborator compare SQLite and PostgreSQL for this application's
> deployment and workload. Have another inspect the code for migration costs.
> Ask them to challenge each other's assumptions and bring me a recommendation
> with the tradeoffs and unresolved questions.

For an existing reviewer:

> Ask the Codex reviewer to check this change for correctness and missing tests.
> Discuss any disagreements with it and bring me the findings that still matter.

COM8 connects the agents; their models and available tools perform the work.
The coordinating agent should bring back an actual response. If a client needs
login, a route is unavailable, or a worker has not answered, it should explain
that condition. A saved message or queued request alone is not an answer.

## 4. Work with existing agents or another device

Ask your agent to show which collaborators it can reach, then name the intended
peer and task. If you use a shared bus, ask it to register this session on that
configured bus first.

Other machines must be connected explicitly. A shared bus needs its owner's
invitation; access to your own machines can use a configured SSH connection.
Installing COM8 alone grants neither. Guided setup can connect an invited device
to [bus.nonlocally.org](https://bus.nonlocally.org) or your team's hub; it keeps
existing connections unless you choose otherwise. See
[work with agents on other computers](HOSTED.md) for the full joining flow.

Release qualification covers the Claude Code CLI and Codex CLI. A Codex request must
target the correct thread; creating a named execution does not automatically
attach that name to every client conversation. For a local coding session in a
desktop app, verify its exact session and an actual reply before relying on
delivery; a queued message alone does not establish that the app processed it.

## When you need more control

- [Installation](INSTALL.md): setup flags, background service, updates, rollback
  and removal.
- [Profiles](PROFILES.md): terminal and mesh helpers, with a preview of the files
  they change. Account helpers, snapshots and contained execution are separate
  options.
- [CLI reference](CLI.md): direct identity, message and terminal operations for
  scripts or debugging, including what delivery receipts prove.

COM8 retains identities and messages when an execution stops. It does not
preserve arbitrary process memory. Keep your project files and normal
provider session history as you ordinarily would.
