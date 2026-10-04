#!/usr/bin/env bash
# Small profile dispatcher; terminal messaging remains the existing COM8 seat API.
op="${1:-help}"; [ $# -eq 0 ] || shift
case "$op" in
  shell)
    fn="${1:-}"; [ $# -eq 0 ] || shift
    case "$fn" in
      t|tn|tk|tl|tp|tj|tw|twp|to|tws|twg|tss|tsr|_t_cycle|al|alw|mesh)
        declare -F "$fn" >/dev/null || { echo "COM8 profile module for $fn is not enabled" >&2; exit 1; }
        "$fn" "$@" ;;
      *) echo 'com8-workstation: unknown shell helper' >&2; exit 2 ;;
    esac ;;
  agent) . "$COM8_PROFILE_RUNTIME/agent.sh" "$@" ;;
  tile|bar|yank)
    case "$op" in tile) script=tile.sh;; bar) script=session-bar.sh;; yank) script=pane-yank.sh;; esac
    bash "$COM8_PROFILE_RUNTIME/tmux/$script" "$@" ;;
  copy)
    if command -v pbcopy >/dev/null; then pbcopy
    elif command -v wl-copy >/dev/null; then wl-copy
    elif command -v xclip >/dev/null; then xclip -selection clipboard
    else echo 'COM8 clipboard needs pbcopy, wl-copy, or xclip' >&2; exit 1; fi ;;
  watch)
    # Private compatibility only. No observer is started for generic installs.
    [ "${COM8_PROFILE_WATCH:-0}" = 1 ] || exit 0
    [ -n "${COM8_PANE_WATCHER:-}" ] && [ -x "$COM8_PANE_WATCHER" ] || {
      echo 'COM8_PROFILE_WATCH needs an executable COM8_PANE_WATCHER' >&2; exit 1;
    }
    exec "$COM8_PANE_WATCHER" watchd "$@" ;;
  *) printf 'com8-workstation {shell HELPER|agent ALIAS|tile|bar|yank|copy|watch}\n' ;;
esac
