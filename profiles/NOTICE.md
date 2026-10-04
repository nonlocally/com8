# Workstation source provenance

Selected shell, Ghostty, tmux, and mesh helpers are adapted from Anu/COM8,
commit `80d3c86`, authored by Aadarsh Agarwal and contributors. Imported at the
owner's direction for the MIT-licensed COM8 consolidation on 2026-09-24.

Donor paths: `config/bash/fns/{tmux,dynlayout,agentlaunch,mesh}`,
`config/tmux/{tmux.conf,tile.sh,session-bar.sh,pane-yank.sh}`, and
`config/ghostty/config`. The source checkout was read only and is not a runtime
dependency. The original helper names and snapshot formats are retained where
useful. Personal service defaults, browser/research/chat applications, global
editor/Git/agent settings, and swarm distribution are not imported.

Adaptations separate package/config/state paths, select shell modules explicitly,
use native agent authentication by default, preserve SSH configuration, and add
ownership-aware configuration installation. See the repository MIT license.

The optional containment adapter (`runtime/box` and `runtime/fns/box`) is adapted
from `config/bash/fns/box` and `config/box/Containerfile` at the same donor commit.
Its selected image excludes notebook/browser applications; package paths,
explicit credential mounts, and a read-only command preview replace source-tree
coupling. The container runtime and image build remain separate opt-in steps.

The optional snapshot module (`runtime/modules/snapshots`) is adapted from the
same Anu commit: `config/bash/bin/anu-snapshot` and
`config/launchd/com.anu.snapshot.plist` (the 03/09/15/21 cadence and the
`snap-`/`nightly-` naming are preserved). Its prune and restore paths were
tightened during the port: a local copy is deleted only after a mounted archive
holds byte-identical verified files, and a corrupt archived copy is never
restored. Personal volume names and paths are not imported; they belong in
private configuration.

Optional account code additionally comes from `config/bash/bin/anu-account`,
selected observer/send/state/notification sections of `plugins/anu/bin/pane`,
and the credential read/cache and get/set/mkdir/env client closure from
`config/bash/bin/anu-secrets`, at the same donor commit. Selected account,
Codex-account, and observer fixtures come from `tests/bash` and `tests/lib`;
fixture accounts/hosts are fictional. Hosting, enrollment/admin operations,
and the wider pane orchestration/presentation commands were excluded. Legacy
state keys and credential-store names remain for compatibility. Host Claude
launch hooks are now supplied per launch; installed paths are quoted, service
defaults removed, and the account module must be selected explicitly.
