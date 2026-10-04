# @aadarwal/com8 — legacy-interface package (new name as of 0.6.0)

COM8 now ships one combined artifact with durable identities and mailboxes,
terminal seats, native Claude/Codex delivery, the bus, and optional workstation
profiles. Install the reviewed archive from
[nonlocally/com8 releases](https://github.com/nonlocally/com8/releases); see the
[combined package guide](../communicate/README.md) for setup and lifecycle details.
The package and MCP registry names follow the product rename (`@aadarwal/com8`,
`io.github.aadarwal/com8`); the earlier names are not provided as aliases. This
does not claim that version 0.6.0 has been published to npm.

This package preserves the legacy durable MCP tool names (`claim`, `send`,
`agents_list`, `seat_spawn`, and others). `com8 serve` still exposes that legacy
MCP face. All other CLI verbs, setup, doctor, update, rollback, and uninstall
delegate to the same combined payload, avoiding a second installer or kernel.
New installations use the combined MCP interface's `com8_` tool names alongside
the existing native and bus tools.

Legacy setup flags remain supported:

```sh
com8 setup --handle your-handle --claim reviewer --no-mcp --no-persist
```

`--handle` explicitly claims the local human handle, `--claim` claims an agent
identity, `--no-mcp` maps to `--no-clients`, and `--no-persist` maps to
`--no-service`. Legacy setup preserves the measured claim/send/inbox self-test.
Without `--no-persist`, legacy setup requests a persistent user service. The
combined installation's stable daemon is preferred to an old legacy daemon path.
All faces of one installation resolve the same `COMM_STATE` and control socket,
so durable state is kept across COM8 updates. An upgrade from the previous
product name does not carry it over: the durable state subdirectory and control
socket moved (see the upgrade notes in [docs/INSTALL.md](../../docs/INSTALL.md)). Node.js 20+ and Python 3.9+ are required; seats require tmux.

MIT license.
