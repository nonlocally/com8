# Qualifying restoration of existing client plugins

This opt-in check runs actual Claude Code and Codex registry commands against an
extracted COM8 release. It needs **Python 3.11+** for standard-library TOML parsing,
Node, and the selected client CLIs. This testing requirement is separate from
COM8's Python 3.9+ runtime requirement.

```sh
python3 scripts/qualify-client-restoration.py /path/to/com8-VERSION \
  --run-clients --evidence /path/to/new-private-evidence-directory
```

The script is self-contained and can be copied beside an extracted release;
it does not import repository helpers. It verifies the artifact manifest before
invoking the artifact's public `bin/com8`. `--provider claude` or `--provider codex`
limits the clients; repeat `--scenario` to select `enabled`, `disabled`, `policy`,
`marketplace-only`, or `none`. The `policy` case applies only to Codex.

Every case creates its own temporary HOME, Claude configuration, Codex home, COM8
installation and state. No existing authentication, client configuration, agent
socket or session environment is supplied. The original plugin is an inert local
version 0.0.7 fixture with no hooks, MCP servers or skills. Commands inspect and
modify plugin registries only; they do not create model requests, threads, or
services. Service, SSH and terminal tools are blocked. Temporary homes are always
removed after each completed case. Each command owns a separate process group;
its remaining children are stopped on completion or timeout before HOME cleanup.
Evidence directories are mode 0700 and files
are mode 0600. An interrupted/killed process may require removing its reported
temporary directory explicitly.

Each case registers the original plugin through the actual client CLI, captures
its original registry/configuration state, and exercises:

1. COM8 setup and repeat update, followed by selective client uninstall.
2. COM8 reinstall, followed by full uninstall.
3. Exact restoration of the original plugin version, installed/enabled state,
   marketplace source and user settings, including an unrelated setting.
4. Preservation of the other client's configuration and literal COM8 state.

Claude disabled state is set through its CLI. Codex has no disable command, so
the test writes a small fixture-only user configuration using supported `enabled`
and per-tool `approval_mode` fields, then checks effective enabled state through
the CLI. It does not hand-edit a client-owned registry. Comparisons ignore only
empty root `enabledPlugins`, `extraKnownMarketplaces`, `plugins` and `marketplaces`
tables, plus installation timestamps. Explicit empty lists and nested dictionaries
remain significant, and both clients' fixtures include an unrelated empty list.

`report.json` records the source commit, runtime path, CLI versions, cases and
pass/fail/unqualified status. Each case file contains command results and before/
after snapshots. Exit codes are 0 for passed selected cases, 1 for failures and
2 for unavailable selected clients. A passing metadata test does not prove a
running agent loaded the plugin; provider activation has its own qualification.
Repeat update here is intentionally an idempotence check, not a distinct-version
upgrade. Historical archives and replacement registrations require separate
checks.
