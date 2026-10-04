# Qualifying an extracted COM8 release

Copy `scripts/qualify-installed.py` and the release archive to the target machine.
Extract the archive; no repository checkout, Python packages, model credentials,
or fixture tree are required. The machine needs Node, Python 3.9+, and Bash 4+.

```sh
python3 qualify-installed.py /path/to/com8-VERSION > qualification.json
```

The exit status is zero when the exercised checks pass. JSON separates passing
and failing checks from unqualified behavior, and records actual artifact,
installed CLI, daemon source, MCP launcher, profile payload, and executable paths.
All installation and runtime state lives under a private temporary directory.
The supplied artifact is read-only; tests use a disposable copy. Temporary files
are removed on exit unless `--keep-temp` is given for investigation.

Checks use the artifact's actual public CLI, plugin MCP launcher, durable kernel,
and profile installer. They verify manifests, fresh installation, repeat update,
mailbox operations through MCP, source resolution, artifact relocation, core
uninstall/reinstall preserving mail, and profile preview/install/repeat/uninstall/
reinstall preserving user edits and private overlays. Provider, network, Git,
package-manager, service-manager and tmux commands are blocked; attempts fail the
run. No user services or current terminal sessions are changed.
Fresh login and non-login Bash startup must resolve helpers from the installed
profile; fresh zsh is checked to remain unchanged. Profile configuration and
ownership/backups must be private to the owner.
All optional modules are selected during the profile lifecycle check. Their
installed help, scoped snapshot preview, and private literal file reply work
without contacting providers or a service manager, including after core
uninstall. The immutable profile payload must remain unchanged. This proves
package independence, not actual account handoff, container execution, or
scheduled snapshots.

To qualify an upgrade and rollback, supply a second **real** release:

```sh
python3 qualify-installed.py /path/to/candidate \
  --previous-runtime /path/to/previous > qualification.json
```

The two release manifests must differ. The runner first exercises the candidate's
fresh lifecycle, then installs the previous release into the same disposable
installation, upgrades to the candidate, verifies retained mail and loaded daemon
source, and rolls back the release pointer. Without the previous artifact, this
check is explicitly unqualified; an idempotent repeat install is not reported as
a version upgrade.

This runner does not qualify real agent plugin activation, model wake/resume,
service activation or reboot persistence, Ghostty rendering, interactive tmux,
remote network access, registry publication, or Homebrew's installation path.
Those require their own environment-specific checks. The repository's focused
profile tests cover isolated tmux behavior separately.
