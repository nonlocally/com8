# Installation

With Homebrew installed, start here:

```sh
brew install nonlocally/tap/com8
com8 setup
com8 doctor
```

Setup lets you choose Claude Code, Codex, and optional tools, and review any
missing software before installing it. Sign into your selected provider, then
open a fresh session and ask your agent to create peers, send messages, or
coordinate work. See the [Quickstart](QUICKSTART.md) for examples and
[Homebrew](#homebrew) below for upgrades and removal.

You can also install from the [COM8 0.5.0 release archive](https://github.com/nonlocally/com8/releases/tag/v0.5.0).

## Requirements

| Needed for | Requirement |
|---|---|
| Launch the archive | macOS or Linux; Node.js 20 or later; Bash |
| COM8 runtime | Python 3.9 or later; guided setup can install it when missing |
| Terminal seats and spawned executions | tmux; included in guided dependency checks when either client is selected, even without a terminal profile |
| Agent sessions | Claude Code CLI and/or Codex CLI; guided setup offers missing clients, with login separately selected; see [Clients](#clients) |
| Other devices | SSH; Tailscale optional |
| Optional profiles | Bash 4+ and fzf; tmux for terminal, jq and SSH for mesh; see [PROFILES.md](PROFILES.md) |

Guided setup can install missing dependencies and clients for selected features.
It can also join a shared bus using your invitation; this is an explicit choice.
Provider sign-in remains with the provider, and installation alone grants no hosted access.
Node.js must already be available to start the archive's installer; the COM8
Homebrew formula supplies Node and the core runtime dependencies.

## Get the archive

The release provides `com8-VERSION.tar.gz` and its matching `.sha256` file.
Download both, then verify the archive before extracting it:

```sh
curl -fLO https://github.com/nonlocally/com8/releases/download/v0.5.0/com8-0.5.0.tar.gz
curl -fLO https://github.com/nonlocally/com8/releases/download/v0.5.0/com8-0.5.0.tar.gz.sha256
shasum -a 256 -c com8-0.5.0.tar.gz.sha256
tar -xzf com8-0.5.0.tar.gz
```

The archive contains the CLI, the MCP server, the plugin, the daemon, and its
production Node dependencies. COM8 itself needs no Git checkout, npm account, or
registry access to install from the archive. Installing missing third-party
tools does need network access to their installation sources. An archive copied
to a machine over SSH installs exactly like a downloaded one.

## Run setup

For Homebrew, run `com8 setup`. From an extracted archive, use:

```sh
./com8-0.5.0/bin/com8 setup
```

With no flags in an interactive terminal, `setup` guides you through the clients,
terminal/mesh tools, optional Ghostty, service and bus choices. It shows the selected
package and configuration actions before asking you to apply them. Use
`--guided` to request that flow explicitly. Declining or reaching end-of-input
at confirmation does not authorize installation.

For automation or a selection you already know, use explicit flags:

```sh
# Preview only: no downloads, login, package installation or configuration writes.
./com8-0.5.0/bin/com8 setup --install-missing --claude --codex --terminal --mesh --dry-run

# Apply that selection; omit either client or profile you do not want.
./com8-0.5.0/bin/com8 setup --install-missing --claude --codex --terminal --mesh --yes

# CLI-only configuration, including on a server:
./com8-0.5.0/bin/com8 setup --no-clients --no-service
```

Add `--ghostty` on macOS to select the application, the configured Nerd Font,
and the terminal profile. It is never inferred from a server or terminal selection. `--terminal` and
`--mesh` select the corresponding owned profiles. Without `--install-missing`,
these selections require their dependencies to be present and report any missing
ones. New noninteractive selections require `--yes` and an explicit client choice
(`--claude`, `--codex`, or `--no-clients`). `--guided --dry-run` does not prompt;
it plans only the explicitly selected features and core requirements.
Selecting a client includes tmux for agent seats; it does not select terminal
configuration. `--no-clients` without terminal/Ghostty selection can omit tmux
for messaging-only use.

Existing explicit commands such as `setup --claude`, `setup --no-clients`, and
`profile install` retain their configuration-only behavior. Bare setup without a
terminal also retains its prior behavior; it does not silently authorize package
downloads. `update` remains the explicit release activation command.

| Flag | Effect |
|---|---|
| `--guided` | Choose features interactively and review the installation plan. |
| `--install-missing` | Permit installation of missing dependencies for the selected features after confirmation, or with `--yes`. |
| `--claude`, `--codex` | Select clients for plugin registration. In configuration-only setup, neither flag means attempt both existing CLIs. |
| `--no-clients` | Install the CLI only; register clients later with `com8 setup --claude` or `--codex`. |
| `--terminal`, `--mesh` | Select owned profiles and check their required tools. |
| `--ghostty` | Explicitly select Ghostty, its configured font, and the terminal profile on macOS. |
| `--model` | v0.5: configure a named model connection with a hidden key prompt. Requires a terminal; automation uses `com8 model add --key-file` or `--key-stdin`. |
| `--bus=local` | Select local bus operation without starting a broker; keep saved remote connections. |
| `--bus=HTTPS_ORIGIN` | Select a shared hub this installation has already joined. Does not enroll a new device. |
| `--bus-invite-file=/absolute/path` | Join using a private invitation file owned by you. Cannot be combined with `--bus`. |
| `--yes` | Confirm the explicit installation selection; does not authorize provider login. |
| `--login-claude`, `--login-codex` | Request the selected provider's interactive login separately; skip it if already logged in. Requires a terminal. |
| `--service` | Also install the per-user daemon service (launchd or systemd `--user`). |
| `--no-service` | Leave an existing managed service untouched during an update. |
| `--service-inherit=NAME` | Add an allowed variable to the service environment, such as `CLAUDE_CONFIG_DIR` or `CODEX_HOME`. Repeat for each variable. |
| `--dry-run` | Print the selected setup/dependency plan and change nothing. Use `com8 profile preview` for the exact profile paths and conflicts. |

### Choosing a model

In v0.5, guided setup can add a model API connection, such as GLM, to power
Claude Code or Codex directly. This is optional and does not change ordinary
client defaults or subscription login. The service's canonical HTTPS `/v1`
address, model ID and scoped key are needed; setup never guesses a public
endpoint or uses a bus credential for inference. Use `com8 setup --model` to
configure it later. The key prompt is hidden; automated configuration uses a
private file or stdin through `com8 model add`.

Setup saves the connection without making a model request. `com8 model doctor`
checks catalog access separately; an actual coding task establishes inference
and tool use. See [model connections](MODELS.md) for the nonlocally endpoint
and client requirements. Model connections require COM8 0.5 or later.

### Choosing a bus

Setup keeps your existing connection unless you explicitly choose another. For
local work, no hosted account is needed. For a shared hub, get an invitation
from its administrator, enter it at the hidden prompt, and confirm the displayed
HTTPS destination. You can finish installation and connect later with
`com8 setup --guided`.

Automated setup accepts `--bus-invite-file=/absolute/path` alongside an explicit
client selection and `--yes`. The file must be a regular, non-symlink file owned
by your account with no permissions for other accounts, for example mode `0600`.
The invitation travels to the bus command through standard input and is never
printed. Dry-run does not read its contents or contact the hub. Setup does not
delete your invitation file; remove it yourself when it is no longer needed.

If joining fails after core installation, setup reports the connection failure
and keeps the installed core available. Correct the invitation or connection and
retry. Agent publication is a separate action, performed by your agent when you
ask it to join a bus. See [connecting agents](HOSTED.md) for general and private
bus workflows.

### Installing missing tools

On macOS, guided setup uses Homebrew and includes Homebrew bootstrap in the
reviewed plan if it is needed. Selected clients require tmux for agent seats.
Terminal selection additionally requires Bash 4+ and fzf;
mesh adds jq and SSH. Selected Claude Code and Codex use their Homebrew casks;
Ghostty and the font are installed only when selected.

Package availability follows [Homebrew's platform requirements](https://docs.brew.sh/Installation).
The fresh-package CI check uses macOS 15 with Apple Silicon;
the core runtime matrix also uses macOS 14 with existing dependencies.
If Homebrew cannot supply a dependency, setup reports its error and stops;
it does not silently opt into an unsupported source build.

On Linux, system-tool recipes use `apt-get` where available. Provider clients use
their official native installers. Missing Ghostty on Linux requires manual
installation; the plan reports it before applying changes. A system without a
supported recipe receives a manual prerequisite instruction. COM8 does not add
a new package manager or a substitute container backend for that system.

Existing provider executables are not implicitly upgraded. If an installed
client lacks COM8's required capabilities, setup reports that failure so you can
choose how to update it. Installing software and signing into it are separate:
`--yes` alone never starts login. Login uses the provider's own interactive CLI;
COM8 does not collect your password or create a hosted account.

Third-party package installation is not part of COM8's configuration rollback.
If a later step fails, installed packages remain available; COM8 does not remove
them during a retry, rollback or uninstall. Core setup and profile installation
have their own ownership and recovery boundaries, and report any recovery needed.

### Installed configuration

Core setup verifies the archive against its manifest; copies the
release to `~/.local/share/communicate/<version>-<hash>/`, which is never modified
again; points the stable link `~/.local/share/communicate/current` at it; creates
`~/.local/share/communicate/bin/com8` and `bin/communicate`; registers the plugin
with the clients you named through their own CLIs; optionally installs the
service. If activation fails, setup attempts to restore the previous state and
reports any recovery still needed.

Then:

```sh
export PATH="$HOME/.local/share/communicate/bin:$PATH"   # add to ~/.bashrc, ~/.zshrc, or equivalent
com8 doctor
```

`doctor` reports the executable in use, the installed release and its source
commit, the running daemon and whether it runs the installed release, client
registration and the cached plugin version, the managed service, and optional
dependencies. It also reports the selected bus, enrollment and reachability,
including saved connections that are offline. It never starts a daemon and never
prints credentials. A bus worker heartbeat does not prove a model is running.

## Clients

After `setup --claude`, restart Claude Code. After `setup --codex`, start a new
Codex thread. A running session keeps the plugin it loaded at start.

The plugin is `communicate@communicate` from the marketplace `communicate`, served
from the installed release. Setup records what was registered before it ran
(installed, enabled, marketplace source, and for Codex the plugin's user
configuration) and restores that installation and its settings on uninstall. If you edit those client
settings after setup, later setup, rollback, or uninstall refuse to replace them
until you reconcile the change; they are never silently overwritten. Client
plugin registration is verified with Claude Code's `claude plugin` commands and
Codex CLI 0.156.1's `codex plugin` commands.

Codex queued delivery requires `codex queue`. Plugin setup also requires the
client's versioned configuration API so existing settings can be restored safely;
setup checks these capabilities before replacing a registration. Installation and
restoration are verified with Codex 0.156.1; queued input is also verified with
0.153.4. These are separate from full model and platform qualification, recorded
in [PROVIDER-QUALIFICATION.md](PROVIDER-QUALIFICATION.md).

Avoid concurrent edits to client configuration while setup, rollback, or removal
is running. Changes already present are checked before replacement; client CLI
commands do not provide a transaction spanning all registration changes.

## The daemon

`com8 start` runs an unmanaged daemon for the current login. `com8 setup --service`
installs a managed one:

- macOS: `~/Library/LaunchAgents/com.communicate.com8.plist`
- Linux: `~/.config/systemd/user/communicate-com8.service`

The service runs the installed release with an explicit environment: your home,
the state directory, the device name, and a PATH containing the selected Python
and standard executable directories. Use `--service-inherit=PATH` to pass your
current PATH explicitly. An installation in
an isolated home gets a scoped service name, so a qualification install can
never replace your real service. A previous definition under the same name is
backed up before replacement. Only one daemon owns a state root at a time.

## Update, roll back, uninstall

If you installed optional profiles, remove their configuration separately with
`com8 profile uninstall` before removing COM8 itself.

```sh
/path/to/com8-0.3.1/bin/com8 update     # from the newly verified archive
com8 rollback --dry-run
com8 rollback
com8 uninstall --claude                 # remove only this client's registration
com8 uninstall                          # remove owned integrations, service, executable links
com8 uninstall --purge                  # also delete retained release payloads
```

Rules that hold throughout:

- A release directory is never changed in place; `update` stages the new one
  and switches `current`. `rollback` switches back to the retained previous
  release and re-registers clients on it.
- Setup only ever replaces what it still owns. A client registration, executable
  link, or service file that someone else changed is kept, and setup says so.
- Uninstall preserves identities, saved messages, credentials, and configuration
  under `~/.local/state/communicate/`. `--purge` removes release payloads only,
  and refuses while any owned integration remains.
- An interrupted run leaves `~/.local/share/communicate/install.lock` with the
  owner's PID and start time. `doctor` shows it. After confirming that process is
  gone, rename that exact directory out of the way and retry; COM8 never removes
  another process's lock.

Upgrading from Communicate 0.1.x or 0.2.x keeps the original payload. Use the
new release's `bin/com8` for lifecycle commands; the old package has no `com8`
entry point.

## Private configuration

Nothing personal ships in the package. Where your settings go:

| Purpose | Location |
|---|---|
| Installed releases, `current`, `bin/` | `~/.local/share/communicate/` (`COMMUNICATE_DATA`) |
| Identities, saved messages, daemon and bus state, device links | `~/.local/state/communicate/` (`COMM_STATE`) |
| Bus broker selection, enrollment, device credential | `~/.local/state/communicate/bus/` |
| Optional profile files, `local.sh`, tmux/Ghostty overrides, mesh hosts | `~/.config/com8/profiles/` |
| Python for the daemon | `COM8_PYTHON=/path/to/python3` |

Keep invitations, device credentials, and hosts out of source control and
public logs. A bus invitation grants membership on one bus; it never grants
shell access or seat control on any device.

## Optional profiles

```sh
com8 profile preview --terminal --mesh    # read-only: every file, action, and conflict
com8 profile install --terminal --mesh
com8 profile status
com8 profile uninstall
```

Modules: `--terminal`, `--mesh`, `--snapshots`, `--box`, `--accounts`. Guided setup
offers only terminal/mesh and optional Ghostty; the other modules are explicit
later choices. Terminal configuration retains the `cx`/`cxx` and `cdx`/`cdxx`
agent shortcuts as commands callable from zsh or Bash. It uses Bash internally,
leaves the interactive shell choice unchanged, and adds only a managed PATH
include to `.zshrc` alongside its existing Bash startup includes.
Profile installation starts no service, runs no package manager, and reloads
no tmux server. It records every file it writes with a backup and
removes only what is still exactly what it wrote. See [PROFILES.md](PROFILES.md),
[SNAPSHOTS.md](SNAPSHOTS.md), and [CONTAINED-EXECUTION.md](CONTAINED-EXECUTION.md).

## Troubleshooting

- **`com8: command not found`** — add `~/.local/share/communicate/bin` to PATH, or
  call the installed `~/.local/share/communicate/bin/com8` directly.
- **The session does not see COM8** — restart Claude Code or start a new Codex
  thread; `com8 doctor` shows the registration and cached version. A cached
  plugin proves installation, not that a running client loaded it.
- **`daemon release parity: different`** in `doctor` — the running daemon was
  started from another source (an unmanaged `com8 start`, or an older service).
  `com8 setup --service` restarts it on the installed release.
- **`MCP dependencies are missing`** — the installed payload was damaged.
  `uninstall --purge` from the retained archive, then `setup` again; state is
  preserved.
- **Setup refuses a file or registration** — it is no longer what COM8 wrote.
  The message names it; move it aside or reconcile the change, then retry.

## Homebrew

```sh
brew install nonlocally/tap/com8
com8 setup
com8 doctor
```

`brew install nonlocally/tap/com8` installs the same archive under Homebrew's prefix and
puts `com8` and `communicate` on PATH; you still run `com8 setup` to register
clients and, optionally, the service. Formula installation never configures your
machine by itself.

Homebrew upgrades make a new release available; activate it explicitly:

```sh
brew upgrade nonlocally/tap/com8
"$(brew --prefix nonlocally/tap/com8)/bin/com8" update
com8 doctor
```

Calling the formula's full path ensures that `update` uses the new installer
even if an older COM8 launcher comes first on PATH. Runtime commands continue
using the release selected by setup or rollback until you activate another one.

If you selected profiles, run `com8 profile uninstall` first. Then remove COM8's
owned client registrations and service before removing the formula:

```sh
"$(brew --prefix nonlocally/tap/com8)/bin/com8" uninstall
brew uninstall nonlocally/tap/com8
```

Use `uninstall --purge` in the first command if you also want to remove retained
release copies. Identities, saved messages and credentials remain preserved.
Homebrew alone does not remove COM8's per-user integrations or state.

## Qualification

How a release is built, tested, and qualified before publication is described in
[RELEASING.md](RELEASING.md), with the installed-artifact, client-restoration,
and provider harnesses linked from [index.md](index.md).
