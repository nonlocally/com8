# Optional contained execution

This adapter retains the useful `box` behavior from Anu: execute a command in a
disposable Apple/container Linux VM, preserve worktree edits, and optionally
share a private provider home and pane reply directory. It is separate from
ordinary tmux panes. Selecting the profile installs helpers; it does not install
a container runtime, start services, download an image, or build one.

```bash
com8 profile preview --box
com8 profile install --box
com8-box plan npm test
com8-box doctor
```

Install and start Apple/container separately on a supported Mac. Once it is
running, `com8-box build` explicitly builds the bundled image; this step uses the
network to fetch its base image, development tools, and Claude Code. The image
contains Bash, Git, Node, Python, common build tools, and Claude Code. It excludes
notebooks, scientific packages, browser services, and Jupyter/Marimo. The build
uses the upstream image/package versions available then; choose a previously
built image through `COM8_BOX_IMAGE` when you need to reuse exactly that image.

```bash
com8-box build
com8-box npm test
com8-box claude                 # /login persists in the separate box home
com8-box run -- bash
```

In a Bash shell with the box profile loaded, `box` is an aliasing function for
the selected launcher. With the terminal and box profiles selected, `cxc`
uses this launcher for contained Claude execution. A private
`COM8_BOX_LAUNCHER` still takes precedence. The account module may call the same
launcher as `claude [arguments]`; its existing restriction on boxed Codex
account launches remains. Other commands/providers need the corresponding
software in the selected image; this adapter does not install it at run time.

`plan` prints the exact command, mounts, and environment variable **names**
without creating directories or invoking the runtime. `doctor` only inspects
runtime and image availability. Neither proves provider authentication or VM
isolation. A stopped runtime or missing image makes `run` fail with an explicit
next step. There is no uncontained fallback.

## Configuration and persistence

Put private settings in `~/.config/com8/profiles/local.sh`. Do not copy provider
credentials into the package or its Containerfile.

| Setting | Default / meaning |
| --- | --- |
| `COM8_BOX_RUNTIME` | `container`; executable path/name for Apple/container |
| `COM8_BOX_IMAGE` | `com8-agent` |
| `COM8_BOX_CPUS` / `COM8_BOX_MEMORY` | `2` / `4G` |
| `COM8_BOX_STATE` | `${XDG_DATA_HOME:-$HOME/.local/share}/com8/box` |
| `COM8_BOX_CLAUDE_HOME` | Separate private `claude` directory under box state |
| `COM8_BOX_CODEX_HOME` | Unset; explicitly mount an existing Codex home if needed |
| `COM8_BOX_PANE_BIN` | Unset; existing directory containing a usable `pane` helper |
| `COM8_BOX_PANE_DIR` | Unset; existing shared pane state/reply directory |

The old `ANU_BOX_IMAGE`, `ANU_BOX_CPUS`, and `ANU_BOX_MEMORY` names are fallback
aliases. There is no `ANU_PATH` or source-checkout fallback. Paths must be absolute
and contain no colon/newline. Explicit provider homes must already exist and
remain user-owned data; COM8 does not change their contents or permissions.
The default contained Claude home is created with mode `0700` only when a run
passes runtime/image checks. It survives disposable VMs and profile uninstall.
Host `~/.claude`, `~/.codex`, SSH keys, Git signing keys, and application data are
not automatically mounted.

For an already reviewed private setup:

```bash
export COM8_BOX_CLAUDE_HOME="$HOME/private/contained-claude"
export COM8_BOX_PANE_BIN="$HOME/private/pane-tools"
export COM8_BOX_PANE_DIR="$HOME/private/pane-state"
```

Pane settings are a pair: the helper directory mounts read-only at
`/opt/com8/pane`, while the selected reply directory mounts read/write at its
real path. `ANU_PANE_DIR` is passed for existing file-based `pane reply` helpers.
The helper must work in Linux with the image's tools and its supplied files.
This does **not** mount a host tmux socket or make host pane-control verbs work
inside a VM. The reply directory is shared among trusted workers, as before;
it is not a per-agent authorization boundary.

## What crosses the boundary

The current Git worktree is mounted read/write at its real path. Outside a Git
repository, that is the current directory. Linked worktrees additionally mount
the shared Git metadata directory read/write, so `.git` pointers and commits
work without exposing the main checkout's source files. This still permits
changes to shared Git objects, configuration, refs, and hooks. The plan lists
every mount so this scope can be reviewed before a run.

`CLAUDE_CODE_OAUTH_TOKEN`, or otherwise `ANTHROPIC_API_KEY`, passes by name-only
environment flags. `OPENAI_API_KEY` and the configured account label also pass
when present. Secret values do not appear in the adapter's argv or plan output.
Git author/committer identity comes from the current Git configuration; commits
inside the default image are unsigned. The guest can read supplied credentials
and modify writable mounts. Network access follows the selected runtime's
normal behavior; this adapter does not create a network policy.

## Validation and provenance

`python3 -B scripts/test-com8-box.py` tests the actual adapter with a disposable
fake runtime and real temporary Git repositories: mount selection, linked
worktrees, read-only preview, absent/stopped runtime behavior, explicit image
building from a copied payload, literal arguments, private state, secret flag
handling, exit propagation, and shell loading without runtime calls.

Actual Apple/container image building, VM startup, provider authentication,
virtiofs permissions, and in-guest pane replies require backend qualification.
The fake-runtime tests make no claim about those behaviors. No actual runtime
is touched by this suite.

Adapted at the owner's direction from Anu commit `80d3c86`,
`config/bash/fns/box` and `config/box/Containerfile`, authored by Aadarsh Agarwal
and contributors. The source repository remains read only and is not a runtime
dependency. See COM8's MIT license.
