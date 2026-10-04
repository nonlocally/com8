# Copy selected settings into ~/.config/com8/profiles/local.sh.
# This file is never installed over user configuration. Shell code here is trusted.
# Native provider authentication is the default. An optional account adapter is a
# single executable that accepts: launch --provider claude|codex [--box] -- ARGS.
# Select --accounts / --box to use COM8's packaged adapters. The overrides below
# are only for an existing private adapter; they are not required for the modules.
# export COM8_ACCOUNT_LAUNCHER="$HOME/.local/libexec/private-account"
# export COM8_BOX_LAUNCHER="$HOME/.local/bin/box"
# export COM8_PANE_WATCHER="$HOME/.local/libexec/private-pane"
# export COM8_PROFILE_WATCH=1
# Keep existing snapshot data in place during a separately reviewed migration:
# export COM8_PROFILE_STATE="$HOME/.local/state/com8/workstation"
# User-owned tmux and Ghostty overrides may be placed at:
# ~/.config/com8/profiles/local.tmux.conf
# ~/.config/com8/profiles/local.ghostty.conf
