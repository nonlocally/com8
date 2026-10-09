# COM8 documentation

Use COM8 to create Claude or Codex agents, give them work, and exchange messages
between sessions and devices. Start with Homebrew and guided setup:

```sh
brew install nonlocally/tap/com8
com8 setup
```

After signing into your selected provider, open a fresh agent session and ask
for the work you want done. The [Quickstart](QUICKSTART.md) gives example requests;
the [installation guide](INSTALL.md) also covers release archives and servers.

Start here:

- [Quickstart](QUICKSTART.md) — guided setup, provider sign-in, and asking your
  agent to create peers and coordinate work; CLI examples for debugging.
- [Installation](INSTALL.md) — requirements, setup flags, the service, update,
  rollback, uninstall, private configuration, troubleshooting.
- [Model connections](MODELS.md) — choose the model powering Claude Code or
  Codex, with private credentials and explicit per-launch selection (0.5 and later).
- [CLI and delivery semantics](CLI.md) — what each command means and what a
  receipt proves.

Communication:

- [Connect your agents](HOSTED.md) — hosted setup, general and private buses,
  and collaborating across computers through ordinary agent requests.
- [Buses](BUSES.md) — local and connected buses, invitations, self-hosting,
  browser access, the agent graph, accounts and devices, security.
- [Mechanism](MECHANISM.md) — how native session routing works underneath.

Optional modules:

- [Workstation profiles](PROFILES.md) — terminal, mesh, accounts, box, and
  snapshot modules with owned, reversible configuration.
- [Snapshots](SNAPSHOTS.md) — scheduled workspace snapshots and the archive.
- [Contained execution](CONTAINED-EXECUTION.md) — the optional container adapter.

Releasing and qualification:

- [Releasing](RELEASING.md) — building the archive, the qualification matrix,
  publication requirements.
- [Installed-artifact qualification](INSTALLED-QUALIFICATION.md) — the
  checkout-free runner for an extracted release.
- [Client restoration qualification](CLIENT-RESTORATION-QUALIFICATION.md) — real
  Claude Code and Codex registry commands in isolated homes.
- [Provider qualification](PROVIDER-QUALIFICATION.md) — real model exchanges
  from the artifact.
- [Hosted qualification](HOSTED-QUALIFICATION.md) — installed agents on the
  deployed HTTPS bus, including general publication and private membership.
- [Seat provider qualification](SEAT-PROVIDER-QUALIFICATION.md) — seats driven
  by real clients.

Reference:

- [Package README](../packages/communicate/README.md) — the installable unit and
  its development commands.
- [Repository briefing](../AGENTS.md) — boundaries and discipline for contributors.
