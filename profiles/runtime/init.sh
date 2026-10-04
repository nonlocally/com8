# COM8 optional profile. No daemons, network calls, prompts, or package installs.
# COM8_PROFILE_RUNTIME and COM8_PROFILE_MODULES come from the managed active.sh.
[ -n "${BASH_VERSION:-}" ] || return 0
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  printf 'COM8 workstation needs Bash 4+; use a newer bash for this profile.\n' >&2
  return 0
fi
export COM8_PROFILE_CONFIG="${COM8_PROFILE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/com8/profiles}"
export COM8_PROFILE_STATE="${COM8_PROFILE_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/com8/workstation}"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
# Account behavior is selected explicitly; native authentication is still the
# default without this module. A watcher requires COM8_PROFILE_WATCH=1 as well.
case " ${COM8_PROFILE_MODULES:-} " in
  *" accounts "*)
    export COM8_ACCOUNT_LAUNCHER="${COM8_ACCOUNT_LAUNCHER:-$HOME/.local/bin/com8-account}"
    export COM8_PANE_WATCHER="${COM8_PANE_WATCHER:-$HOME/.local/bin/com8-account-pane}"
    export ANU_ACCOUNT_BIN="${ANU_ACCOUNT_BIN:-$HOME/.local/bin/com8-account}"
    export ANU_PANE_BIN="${ANU_PANE_BIN:-$HOME/.local/bin/com8-account-pane}"
    ;;
esac
case " ${COM8_PROFILE_MODULES:-} " in
  *" box "*) . "$COM8_PROFILE_RUNTIME/fns/box" ;;
esac
# Private configuration, never replaced by profile installation or upgrade.
[ ! -f "$COM8_PROFILE_CONFIG/local.sh" ] || . "$COM8_PROFILE_CONFIG/local.sh"
case " ${COM8_PROFILE_MODULES:-} " in
  *" terminal "*)
    for _com8_module in tmux agentlaunch; do
      . "$COM8_PROFILE_RUNTIME/fns/$_com8_module"
    done
    unset _com8_module
    cx() { com8-agent cx "$@"; }
    cxx() { com8-agent cxx "$@"; }
    cxc() { com8-agent cxc "$@"; }
    cdx() { com8-agent cdx "$@"; }
    cdxx() { com8-agent cdxx "$@"; }
    cdxxs() { com8-agent cdxxs "$@"; }
    tile() { if [ "$#" -eq 0 ]; then com8-workstation tile status; else com8-workstation tile "$@"; fi; }
    ;;
esac
case " ${COM8_PROFILE_MODULES:-} " in
  *" snapshots "*) . "$COM8_PROFILE_RUNTIME/fns/snapshots" ;;
esac
case " ${COM8_PROFILE_MODULES:-} " in
  *" mesh "*) . "$COM8_PROFILE_RUNTIME/fns/mesh" ;;
esac
