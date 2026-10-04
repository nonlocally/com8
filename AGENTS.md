# COM8 — repository briefing

COM8 continues Communicate: native Claude/Codex session routing, explicit buses,
and durable COM8 identities/mailboxes are packaged as one product. Reuse the
existing implementations. Do not invent a transport or migrate every identity
merely to unify naming.

## Boundaries

- `bin/com8`: public entry point. Durable operations by default; `bus` and `native`
  explicitly select their address spaces; `profile` is optional.
- `bin/communicate`, `lib/*.sh`, `lib/cc_peer.py`: compatible native routing.
- `lib/bus.py`, `lib/bus_broker.py`, `lib/bus_ui.html`, `packages/bus-graph`:
  exact-session registration, membership, broker, graph and human inbox.
- `lib/com8*.py`, `lib/com8.sh`: persistent identity, mail, seats and remote links.
  Message permission and seat-control permission remain separate.
- `packages/communicate`: combined CLI/MCP distribution and installation.
  `packages/com8` is the legacy entry; its npm and MCP registry names follow the
  product rename, while `@aadarwal/communicate` and the `communicate`
  plugin/marketplace identifiers remain.
- `plugins/communicate`, `.agents`: compatible Claude/Codex plugin identities.
- `profiles`: optional terminal/mesh defaults, helpers and owned configuration.

Browser, phone, research and application workspaces are outside the release.
Preserve the bus graph/dashboard/inbox: those are communication interfaces.

## Agent operations

Inspect `com8 bus status --no-start --json` before registration. Use the configured
hub; local/self-hosted and invited remote operation are distinct choices. Register
this exact session, never a newest-transcript guess or a new headless process
presented as self. Preserve supplied reply identities and correlation.

`com8 native agents`, `com8 agents`, and `com8 bus agents --json` expose their
respective native, durable and broker rosters. Do not silently mix their addresses.
Use files/stdin for awkward messages rather than interpolating shell syntax.
Queue acceptance, runtime submission and a correlated reply are different results.

## Development discipline

Runtime state and credentials stay outside source and artifacts. Preserve state,
command and service identifiers unless a tested migration changes them; the COM8
rename is the recorded exception, which changed those identifiers without a
migration, and the upgrade paragraph in docs/INSTALL.md describes the manual
steps. Installers
merge owned settings, retain backups, and remove only objects they still own.
Ordinary uninstall preserves identity, mail and user data.

Use isolated homes, state, sockets, ports and tmux servers in tests. Never run live
launchd/systemd mutation suites as ordinary unit tests. Missing prerequisites are
unqualified coverage, not successful qualification. Start with affected existing
suites and changed-boundary tests, then test CLI/MCP/install from the packed artifact.

`scripts/build-release.py` produces the locked runtime archive. Fresh-client plugin
discovery and absence of source-checkout fallback require runtime acceptance;
manifest lint alone is insufficient. See `docs/RELEASING.md`.

Before publication, audit retained Git history as well as source and artifacts.
Do not expose personal deployment details through universal instructions.
