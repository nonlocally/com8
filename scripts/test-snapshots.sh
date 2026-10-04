#!/usr/bin/env bash
# Optional snapshot/archive module: cadence naming across both schemes, the
# archive gate, the two safety rules (a local copy is deleted only after a
# mounted archive holds byte-identical verified files; a corrupt archived
# snapshot is never restored), unattended runs with a fake snapshotter, and the
# explicit owned schedule against a GLOBAL fake launchd shared by several
# isolated homes (label scoping, loaded-path ownership, inert uninstall,
# failed-reactivation restore, a wrapper shared with the profile). Everything
# runs under temporary homes with fake mount/launchctl/tmux first on PATH and
# the fake manager named explicitly: no real snapshots, drive, tmux server or
# service manager is touched.
set -uo pipefail
# A runner or interactive shell can set XDG/profile roots outside these homes.
# Fixture schedules must never inherit them or source a user's private overlay.
while IFS= read -r name; do
  case "$name" in COM8_*|XDG_*) unset "$name" ;; esac
done < <(compgen -e)
unset TMUX TMUX_PANE BASH_ENV ENV
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOD="$HERE/profiles/runtime/modules/snapshots"
SNAP="$MOD/com8-snapshot"
SCHED="$MOD/schedule.py"
T="$(mktemp -d /tmp/com8-snap.XXXXXX)"
pass=0; fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad() { fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
# Only the main shell cleans up: a subshell that dies (set -u) must not run this trap and take the fixtures with it.
cleanup() { [ "$BASHPID" = "$$" ] || return 0; rm -rf "$T"; }
trap cleanup EXIT

FAKEHOME="$T/home with spaces"; mkdir -p "$FAKEHOME"
export HOME="$FAKEHOME"
FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN"
VOL="$T/vol"; ARCH="$VOL/com8-snapshots"; mkdir -p "$VOL"
LOGF="$T/calls.log"
LAUNCHD="$T/launchd"; mkdir -p "$LAUNCHD"     # the fake domain: one file per loaded label, holding its path
cat > "$FAKEBIN/mount" <<EOF
#!/usr/bin/env bash
# the archive volume is "mounted" only while \$VOL/.mounted exists
[ -e "$VOL/.mounted" ] && printf '/dev/disk9s1 on %s (apfs, local)\n' "$VOL"
[ -e "$T/vol two/.mounted" ] && printf '/dev/disk9s2 on %s (apfs, local)\n' "$T/vol two"
printf '/dev/disk1s1 on / (apfs, local)\n'
EOF
cat > "$FAKEBIN/launchctl" <<EOF
#!/usr/bin/env bash
# A GLOBAL fake launchd domain shared by every HOME in this suite, like the
# real per-user domain: label -> the path it was bootstrapped from.
D="$LAUNCHD"
printf 'launchctl %s\n' "\$*" >> "$LOGF"
case "\$1" in
  bootstrap)
    p="\$3"; l="\$(basename "\$p" .plist)"
    if [ -e "$T/bootstrap-fails" ]; then exit 5; fi
    if [ -e "$T/bootstrap-fails-once" ]; then rm -f "$T/bootstrap-fails-once"; exit 5; fi
    printf '%s\n' "\$p" > "\$D/\$l" ;;
  bootout)
    l="\${2##*/}"; [ -e "$T/bootout-fails" ] && { echo "Boot-out failed: 5: Input/output error" >&2; exit 5; }
    rm -f "\$D/\$l" ;;
  print)
    [ -e "$T/launchctl-broken" ] && { echo "Could not find domain for gui" >&2; exit 125; }
    l="\${2##*/}"; [ -e "\$D/\$l" ] || { echo "Could not find service" >&2; exit 113; }
    printf '%s = {\n\tpath = %s\n\tstate = waiting\n}\n' "\$l" "\$(cat "\$D/\$l")" ;;
esac
exit 0
EOF
SYSTEMD="$T/systemd"; mkdir -p "$SYSTEMD"     # fake user manager: <unit> holds "enabled=X active=Y", <unit>.path its file
cat > "$FAKEBIN/systemctl" <<EOF
#!/usr/bin/env bash
D="$SYSTEMD"
printf 'systemctl %s\n' "\$*" >> "$LOGF"
[ -e "$T/systemd-broken" ] && { echo "Failed to connect to bus: No medium found" >&2; exit 1; }
[ "\$1" = --user ] && shift
cmd="\$1"; shift
now=0; [ "\${1:-}" = --now ] && { now=1; shift; }
unit="\${1:-}"
ufile() { printf '%s/systemd/user/%s' "\${XDG_CONFIG_HOME:-\$HOME/.config}" "\$1"; }
flag() { sed -n "s/.*\$2=\([01]\).*/\1/p" "\$D/\$1" 2>/dev/null; }
setflags() { printf 'enabled=%s active=%s\n' "\$2" "\$3" > "\$D/\$1"; }
case "\$cmd" in
  daemon-reload)
    for s in "\$D"/*.path; do [ -e "\$s" ] || continue; u="\$(basename "\$s" .path)"
      [ -e "\$(cat "\$s")" ] || rm -f "\$s" "\$D/\$u"; done ;;
  show)
    while [ "\${1:-}" = -p ]; do shift 2; done; unit="\${1:-}"
    if [ -e "\$D/\$unit" ]; then
      e="\$(flag "\$unit" enabled)"; a="\$(flag "\$unit" active)"
      printf 'LoadState=loaded\nFragmentPath=%s\nActiveState=%s\nUnitFileState=%s\n' "\$(cat "\$D/\$unit.path")" "\$([ "\$a" = 1 ] && echo active || echo inactive)" "\$([ "\$e" = 1 ] && echo enabled || echo disabled)"
    else printf 'LoadState=not-found\nFragmentPath=\nActiveState=inactive\nUnitFileState=\n'; fi ;;
  enable)
    if [ -e "$T/systemd-enable-fails-once" ]; then rm -f "$T/systemd-enable-fails-once"; echo "Failed to enable unit" >&2; exit 1; fi
    [ -e "\$(ufile "\$unit")" ] || { echo "Failed to enable unit: Unit file \$unit does not exist." >&2; exit 1; }
    printf '%s\n' "\$(ufile "\$unit")" > "\$D/\$unit.path"
    a="\$(flag "\$unit" active)"; [ "\$now" = 1 ] && a=1; setflags "\$unit" 1 "\${a:-0}" ;;
  disable)
    [ -e "$T/systemd-disable-fails" ] && { echo "Failed to disable unit" >&2; exit 1; }
    a="\$(flag "\$unit" active)"; [ "\$now" = 1 ] && a=0; [ -e "\$D/\$unit" ] && setflags "\$unit" 0 "\${a:-0}" ;;
  start) [ -e "\$D/\$unit" ] || { printf '%s\n' "\$(ufile "\$unit")" > "\$D/\$unit.path"; setflags "\$unit" 0 0; }
         setflags "\$unit" "\$(flag "\$unit" enabled)" 1 ;;
  stop)  [ -e "\$D/\$unit" ] && setflags "\$unit" "\$(flag "\$unit" enabled)" 0 ;;
esac
exit 0
EOF
cat > "$FAKEBIN/tmux" <<'EOF'
#!/usr/bin/env bash
# A detached server has sessions even when older tmux rejects `info` because
# no client is attached. Unattended snapshots must not use that client probe.
[ "$1" = list-sessions ] && exit 0
exit 1
EOF
chmod +x "$FAKEBIN"/*
export PATH="$FAKEBIN:$PATH"

# The CLI against a fixture tree. $1 = sessions dir; archive at $ARCH on $VOL.
_cli() { # <sess_dir> [args...]
  local sess="$1"; shift
  HOME="$FAKEHOME" COM8_SNAPSHOT_DIR="$sess" COM8_SNAPSHOT_LOG="$sess/../snapshots.log" \
    COM8_SNAPSHOT_VOL="$VOL" COM8_SNAPSHOT_ARCHIVE="$ARCH" bash "$SNAP" "$@" 2>&1
}
# A helper from the sourced script, in a subshell (it sets -u/pipefail).
_call() { # <sess_dir> <fn> [args...]
  local sess="$1"; shift
  ( set +u; export HOME="$FAKEHOME" COM8_SNAPSHOT_DIR="$sess" COM8_SNAPSHOT_LOG="$sess/../snapshots.log" \
      COM8_SNAPSHOT_VOL="$VOL" COM8_SNAPSHOT_ARCHIVE="$ARCH"
    source "$SNAP" >/dev/null 2>&1
    "$@" )
}
_names() { while read -r d; do basename "$d"; done | tr '\n' ' '; }
_mkfix() { # <sess_dir> <name> [archived]
  mkdir -p "$1/$2"
  printf 'saved\t%s\nsessions\t1\nwindows\t2\npanes\t3\nagents\t3\n' "$2" > "$1/$2/meta.tsv"
  printf 'window\t%s\tpane\n' "$2" > "$1/$2/layout.tsv"
  [ "${3:-}" = archived ] && printf 'yes\n' > "$1/$2/.archived"
  return 0
}
_mkarch() { # <sess_dir> <name>: a verified archive copy of the local snapshot
  mkdir -p "$ARCH/$2"
  cp "$1/$2"/*.tsv "$ARCH/$2/"
  ( cd "$ARCH/$2" && find . -type f ! -name '.manifest.sha256' -print0 | sort -z | xargs -0 shasum -a 256 > .manifest.sha256 )
}
_mode() { if stat -f %Lp "$1" >/dev/null 2>&1; then stat -f %Lp "$1"; else stat -c %a "$1"; fi; }
export -f _mode
mounted()   { touch "$VOL/.mounted"; }
# The fake snapshotter every section may use.
cat > "$T/fake-fns" <<'EOF'
# Like the real tss: saves under $COM8_PROFILE_STATE/sessions with umask 077,
# and (for the suite) records the mode of the run lock while it exists.
tss() ( umask 077; local d="${COM8_PROFILE_STATE}/sessions/$1"; mkdir -p "$d"
        printf 'saved\tnow\nsessions\t2\nwindows\t3\npanes\t5\nagents\t1\n' > "$d/meta.tsv"; printf 'w\n' > "$d/layout.tsv"
        [ -z "${FAKE_TSS_LOCKMODE:-}" ] || _mode "${COM8_PROFILE_STATE}/sessions/.${1%.partial}.lock" > "$FAKE_TSS_LOCKMODE" 2>/dev/null || true )
EOF
unmounted() { rm -f "$VOL/.mounted"; }

echo "== module files"
[ -x "$SNAP" ] && ok "com8-snapshot is executable" || bad "com8-snapshot executable present at $SNAP"
[ -f "$SCHED" ] && ok "schedule.py present" || bad "schedule.py present"
if bash -n "$SNAP" 2>/dev/null; then ok "com8-snapshot parses"; else bad "com8-snapshot parses"; fi
grep -q 'sys.dont_write_bytecode = True' "$SCHED" && ok "schedule.py never writes bytecode into the payload" || bad "dont_write_bytecode missing"

echo "== ordering is chronological across both naming schemes"
S1="$T/s1/sessions"; mkdir -p "$S1"
_mkfix "$S1" snap-2026-08-21-0900; _mkfix "$S1" nightly-2026-08-22; _mkfix "$S1" snap-2026-08-21-2100; _mkfix "$S1" snap-2026-08-23-0300
got="$(_call "$S1" _snap_dirs "$S1" | _names)"
[ "$got" = "snap-2026-08-21-0900 snap-2026-08-21-2100 nightly-2026-08-22 snap-2026-08-23-0300 " ] && ok "oldest-first interleaves legacy nights by date" || bad "oldest-first ordering (got: $got)"
got="$(_call "$S1" _snap_dirs "$S1" -r | _names)"
[ "$got" = "snap-2026-08-23-0300 nightly-2026-08-22 snap-2026-08-21-2100 snap-2026-08-21-0900 " ] && ok "-r reverses to newest-first" || bad "-r ordering (got: $got)"
S2="$T/s2/sessions"; mkdir -p "$S2"
_mkfix "$S2" nightly-2026-08-22; _mkfix "$S2" snap-2026-08-22-0015; _mkfix "$S2" snap-2026-08-22-0900
got="$(_call "$S2" _snap_dirs "$S2" | _names)"
[ "$got" = "snap-2026-08-22-0015 nightly-2026-08-22 snap-2026-08-22-0900 " ] && ok "a legacy night orders as 03:00 within its day" || bad "legacy night ordering (got: $got)"
S3="$T/s3/sessions"; mkdir -p "$S3"
_mkfix "$S3" snap-2026-08-23-0300; _mkfix "$S3" default; _mkfix "$S3" workspace-2026-07-26; _mkfix "$S3" snap-2026-08-23
got="$(_call "$S3" _snap_dirs "$S3" | _names)"
[ "$got" = "snap-2026-08-23-0300 " ] && ok "named workspaces and malformed names are left alone" || bad "name filtering (got: $got)"

echo "== prune deletes a local copy only after a MOUNTED archive holds verified, byte-identical files"
S4="$T/s4/sessions"; mkdir -p "$S4"; rm -rf "$ARCH"; mkdir -p "$ARCH"
_mkfix "$S4" nightly-2026-08-20 archived;  _mkarch "$S4" nightly-2026-08-20
_mkfix "$S4" snap-2026-08-21-0300 archived; _mkarch "$S4" snap-2026-08-21-0300
_mkfix "$S4" snap-2026-08-21-2100                                   # marked nothing, never archived
_mkfix "$S4" snap-2026-08-22-0300 archived                          # marker but NO archive copy
_mkfix "$S4" snap-2026-08-22-0900 archived; _mkarch "$S4" snap-2026-08-22-0900
printf 'tampered\n' >> "$ARCH/snap-2026-08-22-0900/layout.tsv"      # archive bytes differ from local
_mkfix "$S4" snap-2026-08-22-1500 archived; _mkarch "$S4" snap-2026-08-22-1500
printf 'edited locally\n' >> "$S4/snap-2026-08-22-1500/layout.tsv"   # local bytes differ from archive
_mkfix "$S4" snap-2026-08-23-0300 archived; _mkarch "$S4" snap-2026-08-23-0300
unmounted
out="$(_cli "$S4" prune 1)"
[ -d "$S4/nightly-2026-08-20" ] && [ -d "$S4/snap-2026-08-21-0300" ] && ok "archive not mounted: nothing deleted, whatever the markers say" || bad "unmounted prune deleted a local copy (out: $out)"
printf '%s' "$out" | grep -q "not mounted" && ok "…and the report says the archive is not mounted" || bad "unmounted prune report (out: $out)"
mounted
out="$(_cli "$S4" prune 1)"
[ ! -d "$S4/nightly-2026-08-20" ] && [ ! -d "$S4/snap-2026-08-21-0300" ] && ok "verified-identical archived copies: the two oldest are deleted" || bad "verified copies not pruned (out: $out)"
[ -d "$S4/snap-2026-08-21-2100" ] && ok "never-archived snapshot survives" || bad "never-archived snapshot deleted"
[ -d "$S4/snap-2026-08-22-0300" ] && ok "a .archived marker with no archive copy does not permit deletion" || bad "marker-only snapshot deleted"
[ -d "$S4/snap-2026-08-22-0900" ] && ok "an archive copy whose bytes differ does not permit deletion" || bad "differing-archive snapshot deleted"
[ -d "$S4/snap-2026-08-22-1500" ] && ok "a local copy edited after archiving does not permit deletion" || bad "locally-edited snapshot deleted"
[ -d "$S4/snap-2026-08-23-0300" ] && ok "newest snapshot survives" || bad "newest snapshot deleted"
printf '%s' "$out" | grep -q "removed 2" && ok "report counts the 2 removed" || bad "removed count (out: $out)"
printf '%s' "$out" | grep -q "4 not-yet-verified" && ok "report counts the 4 kept for lack of a verified archive copy" || bad "kept count (out: $out)"
[ -d "$ARCH/nightly-2026-08-20" ] && ok "nothing is ever deleted from the archive" || bad "archive copy deleted"

echo "== restore refuses a corrupt archived snapshot and leaves the destination unchanged"
S5="$T/s5/sessions"; mkdir -p "$S5"; rm -rf "$ARCH"; mkdir -p "$ARCH"
_mkfix "$T/s5/stage" snap-2026-08-24-0300; _mkarch "$T/s5/stage" snap-2026-08-24-0300
_mkfix "$T/s5/stage" snap-2026-08-25-0300; _mkarch "$T/s5/stage" snap-2026-08-25-0300
printf 'bitrot\n' >> "$ARCH/snap-2026-08-25-0300/meta.tsv"
mounted
out="$(_cli "$S5" restore 2026-08-25-0300)"; rc=$?
[ $rc -ne 0 ] && ok "corrupt archive: restore exits nonzero" || bad "corrupt restore exited 0"
printf '%s' "$out" | grep -qi "refus" && ok "…and says it refused" || bad "refusal message (out: $out)"
[ ! -e "$S5/snap-2026-08-25-0300" ] && [ -z "$(ls -A "$S5")" ] && ok "…and wrote nothing into the sessions dir, not even a partial" || bad "corrupt restore left files: $(ls -A "$S5")"
out="$(_cli "$S5" restore 2026-08-24-0300)"; rc=$?
[ $rc -eq 0 ] && [ -f "$S5/snap-2026-08-24-0300/meta.tsv" ] && ok "a verified archive restores" || bad "verified restore (rc=$rc out: $out)"
printf '%s' "$out" | grep -q "tsr snap-2026-08-24-0300" && ok "…naming the tsr command" || bad "tsr hint (out: $out)"
[ -f "$S5/snap-2026-08-24-0300/.archived" ] && ok "…and the restored copy is marked archived (it is verified on the archive)" || bad "restored copy not marked archived"
unmounted
out="$(_cli "$S5" restore 2026-08-24-0300)"
printf '%s' "$out" | grep -q "already present locally" && ok "a local snapshot restores without the archive" || bad "local restore (out: $out)"
out="$(_cli "$S5" restore 2026-08-25-0300)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q "not mounted" && ok "restore from an unmounted archive is refused with a reason" || bad "unmounted restore (rc=$rc out: $out)"

echo "== an archive configuration that could make prune delete the only copy is refused"
S8="$T/s8/sessions"; mkdir -p "$S8"; rm -rf "$ARCH"; mkdir -p "$ARCH"; mounted
_mkfix "$S8" snap-2026-08-26-0300 archived; _mkfix "$S8" snap-2026-08-26-0900 archived; _mkfix "$S8" snap-2026-08-26-1500
_cfg() { # <archive> <sessions> [args...]: the CLI with an explicit archive/sessions pair
  local arch="$1" sess="$2"; shift 2
  HOME="$FAKEHOME" COM8_SNAPSHOT_DIR="$sess" COM8_SNAPSHOT_LOG="$T/s8/snapshots.log" COM8_SNAPSHOT_VOL="$VOL" COM8_SNAPSHOT_ARCHIVE="$arch" bash "$SNAP" "$@" 2>&1
}
out="$(_cfg "$S8" "$S8" prune 0)"
[ -d "$S8/snap-2026-08-26-0300" ] && [ -d "$S8/snap-2026-08-26-0900" ] && printf '%s' "$out" | grep -qi "refus" && ok "archive == sessions dir: prune deletes nothing and says the archive is refused" || bad "same-dir archive (out: $out)"
mkdir -p "$S8/archive"
out="$(_cfg "$S8/archive" "$S8" prune 0)"
[ -d "$S8/snap-2026-08-26-0300" ] && printf '%s' "$out" | grep -qi "refus" && ok "an archive inside the sessions dir is refused" || bad "nested archive (out: $out)"
mkdir -p "$T/elsewhere"
out="$(_cfg "$T/elsewhere" "$S8" prune 0)"
[ -d "$S8/snap-2026-08-26-0300" ] && printf '%s' "$out" | grep -qi "outside" && ok "an archive outside the mounted volume is refused" || bad "archive outside VOL (out: $out)"
out="$(_cfg "$T/elsewhere" "$S8" archive)"
[ ! -e "$S8/snap-2026-08-26-1500/.archived" ] && printf '%s' "$out" | grep -qi "refus" && ok "…and archive mirrors nothing there" || bad "archive outside VOL mirrored (out: $out)"
ln -s "$S8" "$VOL/sessions-link"
out="$(_cfg "$VOL/sessions-link" "$S8" prune 0)"
[ -d "$S8/snap-2026-08-26-0300" ] && printf '%s' "$out" | grep -qi "refus" && ok "a symlink on the volume pointing back at the sessions dir is refused" || bad "symlink overlap (out: $out)"
mkdir -p "$ARCH/sess"; _mkfix "$ARCH/sess" snap-2026-08-27-0300 archived; _mkarch "$ARCH/sess" snap-2026-08-27-0300
out="$(_cfg "$ARCH" "$ARCH/sess" prune 0)"
[ -d "$ARCH/sess/snap-2026-08-27-0300" ] && printf '%s' "$out" | grep -qi "refus" && ok "a sessions dir inside the archive is refused" || bad "sessions inside archive (out: $out)"
out="$(_cfg "$ARCH" "$S8" status)"
printf '%s' "$out" | grep -q "mounted at" && ok "a well-formed configuration reports the volume mounted" || bad "good config status (out: $out)"
unmounted; mkdir -p "$T/vol two"; touch "$T/vol two/.mounted"
out="$(_cfg "$ARCH" "$S8" status)"
printf '%s' "$out" | grep -q "not mounted" && ok "the mount check matches the exact mount path: a longer name is not this volume" || bad "mount prefix match (out: $out)"
rm -f "$T/vol two/.mounted"; mounted

echo "== a local symlink or special entry is never treated as archived"
S9="$T/s9/sessions"; mkdir -p "$S9"; rm -rf "$ARCH"; mkdir -p "$ARCH"; mounted
_mkfix "$S9" snap-2026-08-28-0300; ln -s meta.tsv "$S9/snap-2026-08-28-0300/latest"
out="$(_cli "$S9" archive)"
[ ! -e "$S9/snap-2026-08-28-0300/.archived" ] && printf '%s' "$out" | grep -q "archive FAILED" && ok "a snapshot holding a symlink is not marked archived" || bad "symlink snapshot archived (out: $out)"
out="$(_cli "$S9" prune 0)"
[ -d "$S9/snap-2026-08-28-0300" ] && ok "…and prune keeps it" || bad "symlink snapshot pruned"

echo "== an archive copy with extra, special or traversing entries is refused"
S10="$T/s10/sessions"; mkdir -p "$S10"; rm -rf "$ARCH"; mkdir -p "$ARCH"; mounted
_mkfix "$T/s10/stage" snap-2026-08-29-0300; _mkarch "$T/s10/stage" snap-2026-08-29-0300; printf 'extra\n' > "$ARCH/snap-2026-08-29-0300/extra.txt"
out="$(_cli "$S10" restore 2026-08-29-0300)"; rc=$?
[ $rc -ne 0 ] && [ -z "$(ls -A "$S10")" ] && ok "an archive copy with a file its manifest does not list is refused, destination untouched" || bad "extra file restore (rc=$rc out: $out; left: $(ls -A "$S10"))"
_mkfix "$T/s10/stage" snap-2026-08-29-0900; _mkarch "$T/s10/stage" snap-2026-08-29-0900
printf '%s  ./../../escape.txt\n' "$(shasum -a 256 "$T/s10/stage/snap-2026-08-29-0900/meta.tsv" | cut -c1-64)" >> "$ARCH/snap-2026-08-29-0900/.manifest.sha256"
out="$(_cli "$S10" restore 2026-08-29-0900)"; rc=$?
[ $rc -ne 0 ] && [ -z "$(ls -A "$S10")" ] && [ ! -e "$T/s10/escape.txt" ] && ok "a manifest that traverses out of the snapshot is refused" || bad "traversing manifest (rc=$rc out: $out)"
_mkfix "$T/s10/stage" snap-2026-08-29-1500; _mkarch "$T/s10/stage" snap-2026-08-29-1500; ln -s /etc/hosts "$ARCH/snap-2026-08-29-1500/hosts"
out="$(_cli "$S10" restore 2026-08-29-1500)"; rc=$?
[ $rc -ne 0 ] && [ -z "$(ls -A "$S10")" ] && ok "an archive copy holding a symlink is refused" || bad "symlink in archive (rc=$rc out: $out)"
_mkfix "$S10" snap-2026-08-30-0300 archived; _mkarch "$S10" snap-2026-08-30-0300; printf 'extra\n' > "$ARCH/snap-2026-08-30-0300/extra.txt"
_mkfix "$S10" snap-2026-08-30-0900
out="$(_cli "$S10" prune 1)"
[ -d "$S10/snap-2026-08-30-0300" ] && ok "prune keeps a local copy whose archive copy is not an exact inventory" || bad "prune deleted despite extra archive file (out: $out)"
out="$(_cli "$S10" verify)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q "CORRUPT" && ok "verify reports those copies corrupt" || bad "verify (rc=$rc out: $out)"
_mkfix "$T/s10/stage" snap-2026-08-31-0300; mkdir -p "$ARCH/snap-2026-08-31-0300"; cp "$T/s10/stage/snap-2026-08-31-0300"/*.tsv "$ARCH/snap-2026-08-31-0300/"
( cd "$ARCH/snap-2026-08-31-0300" && { shasum -a 256 ./meta.tsv; shasum -a 256 -b ./layout.tsv; } > .manifest.sha256 )
out="$(_cli "$S10" restore 2026-08-31-0300)"; rc=$?
[ $rc -eq 0 ] && [ -f "$S10/snap-2026-08-31-0300/layout.tsv" ] && ok "existing text and binary-mode shasum manifests keep verifying" || bad "gnu manifest formats (rc=$rc out: $out)"

echo "== new state, logs, locks, manifests and archive copies are private under a permissive umask; nothing existing is re-moded"
FRESH="$T/fresh-home"; mkdir -p "$FRESH"; rm -rf "$ARCH"; mounted     # the script, not the suite, creates the archive dir
STATE="$FRESH/.local/state/com8/workstation"
( umask 022; HOME="$FRESH" COM8_SNAPSHOT_FNS="$T/fake-fns" FAKE_TSS_LOCKMODE="$T/lockmode" COM8_SNAPSHOT_VOL="$VOL" COM8_SNAPSHOT_ARCHIVE="$ARCH" bash "$SNAP" run snap-2026-09-02-0300 > "$T/fresh-run.out" 2>&1 )
[ "$(_mode "$STATE")" = 700 ] && [ "$(_mode "$STATE/sessions")" = 700 ] && ok "fresh state parents and the sessions dir are 0700 under umask 022" || bad "state dirs: $(_mode "$STATE" 2>/dev/null) $(_mode "$STATE/sessions" 2>/dev/null)"
[ "$(_mode "$STATE/snapshots.log")" = 600 ] && ok "the new log is 0600" || bad "log mode: $(_mode "$STATE/snapshots.log" 2>/dev/null)"
[ "$(cat "$T/lockmode" 2>/dev/null)" = 700 ] && ok "the run lock is 0700 while it exists" || bad "lock mode: $(cat "$T/lockmode" 2>/dev/null)"
SNAPD="$STATE/sessions/snap-2026-09-02-0300"
[ "$(_mode "$SNAPD")" = 700 ] && [ "$(_mode "$SNAPD/.manifest.sha256")" = 600 ] && [ "$(_mode "$SNAPD/.archived")" = 600 ] && ok "the snapshot stays 0700 and its manifest and marker are 0600" || bad "snapshot modes: $(_mode "$SNAPD" 2>/dev/null) $(_mode "$SNAPD/.manifest.sha256" 2>/dev/null) $(_mode "$SNAPD/.archived" 2>/dev/null) (run: $(tr '\n' '|' < "$T/fresh-run.out"))"
[ "$(_mode "$ARCH")" = 700 ] && [ "$(_mode "$ARCH/snap-2026-09-02-0300")" = 700 ] && [ "$(_mode "$ARCH/snap-2026-09-02-0300/.manifest.sha256")" = 600 ] && ok "the new archive dir is 0700 and the archived copy keeps its private modes" || bad "archive modes: $(_mode "$ARCH" 2>/dev/null) $(_mode "$ARCH/snap-2026-09-02-0300" 2>/dev/null)"
rm -rf "$SNAPD"
( umask 022; HOME="$FRESH" COM8_SNAPSHOT_VOL="$VOL" COM8_SNAPSHOT_ARCHIVE="$ARCH" bash "$SNAP" restore 2026-09-02-0300 >/dev/null 2>&1 )
[ "$(_mode "$SNAPD")" = 700 ] && [ "$(_mode "$SNAPD/meta.tsv")" = 600 ] && [ "$(_mode "$SNAPD/.archived")" = 600 ] && ok "a restored snapshot keeps the archive copy's private modes" || bad "restored modes: $(_mode "$SNAPD" 2>/dev/null) $(_mode "$SNAPD/meta.tsv" 2>/dev/null) $(_mode "$SNAPD/.archived" 2>/dev/null)"
chmod 755 "$ARCH/snap-2026-09-02-0300"; rm -rf "$SNAPD"
( umask 022; HOME="$FRESH" COM8_SNAPSHOT_VOL="$VOL" COM8_SNAPSHOT_ARCHIVE="$ARCH" bash "$SNAP" restore 2026-09-02-0300 >/dev/null 2>&1 )
[ "$(_mode "$SNAPD")" = 755 ] && ok "…and an archive copy that was never private is restored as it is, not claimed private" || bad "imported mode not retained: $(_mode "$SNAPD" 2>/dev/null)"
chmod 700 "$ARCH/snap-2026-09-02-0300"
PRE="$T/pre-home"; mkdir -p "$PRE/.local/state/com8/workstation/sessions"; chmod 755 "$PRE/.local/state/com8/workstation" "$PRE/.local/state/com8/workstation/sessions"
printf 'earlier\n' > "$PRE/.local/state/com8/workstation/snapshots.log"; chmod 644 "$PRE/.local/state/com8/workstation/snapshots.log"
( umask 022; HOME="$PRE" COM8_SNAPSHOT_FNS="$T/fake-fns" COM8_SNAPSHOT_VOL="$VOL" COM8_SNAPSHOT_ARCHIVE="$ARCH" bash "$SNAP" run snap-2026-09-02-0900 >/dev/null 2>&1 )
[ "$(_mode "$PRE/.local/state/com8/workstation/sessions")" = 755 ] && [ "$(_mode "$PRE/.local/state/com8/workstation/snapshots.log")" = 644 ] && ok "pre-existing directories and a pre-existing log keep their modes (never re-moded)" || bad "existing data re-moded: $(_mode "$PRE/.local/state/com8/workstation/sessions") $(_mode "$PRE/.local/state/com8/workstation/snapshots.log")"
got="$( umask 022; export HOME="$FRESH" COM8_SNAPSHOT_LOG="$T/umask-probe.log"; source "$SNAP" >/dev/null 2>&1; _logline OK probe >/dev/null; umask )"
[ "$got" = 0022 ] && ok "sourcing the module and logging leaves the invoking shell's umask at 0022" || bad "invoking umask changed to $got"
[ "$(_mode "$T/umask-probe.log")" = 600 ] && ok "…while the log it created is still 0600" || bad "probe log mode: $(_mode "$T/umask-probe.log" 2>/dev/null)"

echo "== restore resolves every token form"
S6="$T/s6/sessions"; mkdir -p "$S6"
_mkfix "$S6" nightly-2026-08-22; _mkfix "$S6" snap-2026-08-23-0300; _mkfix "$S6" snap-2026-08-23-2100
[ "$(_call "$S6" _resolve_snap "$S6" snap-2026-08-23-0300)" = "snap-2026-08-23-0300" ] && ok "full current-scheme name passes through" || bad "full name"
[ "$(_call "$S6" _resolve_snap "$S6" 2026-08-23-0300)" = "snap-2026-08-23-0300" ] && ok "YYYY-MM-DD-HHMM gains the snap- prefix" || bad "date-time token"
[ "$(_call "$S6" _resolve_snap "$S6" 2026-08-22)" = "nightly-2026-08-22" ] && ok "a bare date finds the day's single legacy night" || bad "bare date legacy"
out="$(_call "$S6" _resolve_snap "$S6" 2026-08-23 2>&1)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q ambiguous && ok "a bare date with several snapshots is refused as ambiguous" || bad "ambiguous date (rc=$rc out: $out)"
_call "$S6" _resolve_snap "$S6" ../etc >/dev/null 2>&1 && bad "a path token is accepted" || ok "a non-date, non-name token is rejected"

echo "== list, status and retention"
out="$(_cli "$S4" list)"
[ "$(printf '%s\n' "$out" | head -1 | awk '{print $2}')" = "snap-2026-08-23-0300" ] && ok "list is newest-first" || bad "list order (out: $out)"
printf '%s' "$out" | grep -q '^\* snap-2026-08-23-0300' && ok "list marks archived snapshots" || bad "archived mark (out: $out)"
out="$(_cli "$S4" status)"
printf '%s' "$out" | grep -q "every 6h at 03:00, 09:00, 15:00, 21:00" && ok "status states the cadence" || bad "cadence text (out: $out)"
printf '%s' "$out" | grep -q "keep newest 56 snapshots" && ok "KEEP defaults to 56 (14 days at 4/day)" || bad "keep default (out: $out)"
out="$(COM8_SNAPSHOT_KEEP=8 _cli "$S4" status)"
printf '%s' "$out" | grep -q "keep newest 8 snapshots" && ok "COM8_SNAPSHOT_KEEP overrides" || bad "keep override (out: $out)"
printf '%s' "$out" | grep -Eq '^label: +(com\.communicate\.com8\.snapshots\.[0-9a-f]{8}|communicate-com8-snapshots-[0-9a-f]{8}\.timer)$' && ok "status names the scoped native COM8 schedule label" || bad "label (out: $out)"
printf '%s' "$out" | grep -q "loaded:    no" && ok "status reports the (scoped) job not loaded" || bad "loaded line (out: $out)"
out="$(_cli "$S4" bogus)"; rc=$?
[ $rc -eq 2 ] && ok "unknown command exits 2" || bad "unknown command rc=$rc"

echo "== an unattended run: fake snapshotter, no tmux server needed"
S7="$T/s7/sessions"; mkdir -p "$S7"
out="$(COM8_SNAPSHOT_FNS="$T/fake-fns" _cli "$S7" run snap-2026-09-01-0300)"
[ -f "$S7/snap-2026-09-01-0300/meta.tsv" ] && ok "run takes the named snapshot" || bad "run snapshot (out: $out)"
[ ! -e "$S7/snap-2026-09-01-0300.partial" ] && ok "…promoted atomically from its .partial" || bad "partial left behind"
grep -q 'OK     sessions=2 windows=3 panes=5 agents=1  -> snap-2026-09-01-0300' "$T/s7/snapshots.log" && ok "…and logged one aligned OK line" || bad "log line (log: $(cat "$T/s7/snapshots.log" 2>/dev/null))"
out="$(COM8_SNAPSHOT_FNS="$T/fake-fns" COM8_SNAPSHOT_VOL= COM8_SNAPSHOT_ARCHIVE= HOME="$FAKEHOME" COM8_SNAPSHOT_DIR="$S7" COM8_SNAPSHOT_LOG="$T/s7/snapshots.log" bash "$SNAP" run snap-2026-09-01-0900 2>&1)"
printf '%s' "$out" | grep -q "no archive volume configured" && ok "with no archive configured the run says so and keeps everything" || bad "no-archive message (out: $out)"
[ -d "$S7/snap-2026-09-01-0300" ] && ok "…nothing pruned without an archive" || bad "pruned without archive"
out="$(COM8_SNAPSHOT_FNS="$T/fake-fns" _cli "$S7" run 'bad name/../x')"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q "unsafe" && ok "an unsafe snapshot name is refused" || bad "unsafe name (rc=$rc out: $out)"
name="snap-$(date '+%Y-%m-%d-%H%M')"
printf '%s' "$name" | grep -Eq '^snap-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}$' && ok "the default name matches snap-YYYY-MM-DD-HHMM" || bad "default name shape"

echo "== the explicit owned schedule: one GLOBAL fake launchd shared by every home, named as an explicit fixture"
rm -f "$LOGF"
ACCOUNT_HOME="$(python3 -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
HOME_A="$T/home-a"; HOME_B="$T/home-b"; HOME_C="$T/home-c"; HOME_E="$T/home-e"; HOME_F="$T/home-f"; HOME_G="$T/home-g"; HOME_H="$T/home-h"; HOME_L="$T/home-l"
mkdir -p "$HOME_A" "$HOME_B" "$HOME_C" "$HOME_E" "$HOME_F" "$HOME_G" "$HOME_H" "$HOME_L"
sched() { # <home> <verb> [args...]
  local h="$1"; shift
  python3 "$SCHED" "$@" --home "$h" --runtime "$HERE/profiles/runtime" --platform darwin --manager "$FAKEBIN/launchctl" 2>&1
}
jq_() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }
label_of() { sched "$1" status | jq_ 'print(d["label"])'; }
plist_of() { printf '%s/Library/LaunchAgents/%s.plist' "$1" "$(label_of "$1")"; }
calls() { grep -c . "$LOGF" 2>/dev/null || echo 0; }

echo "-- labels: default only for the actual account home with standard roots; stable scoped labels elsewhere"
out="$(env -u XDG_CONFIG_HOME -u XDG_STATE_HOME python3 "$SCHED" preview --home "$ACCOUNT_HOME" --runtime "$HERE/profiles/runtime" --platform darwin 2>&1)"
printf '%s' "$out" | jq_ 'assert d["read_only"]; assert d["label"] == "com.communicate.com8.snapshots", d["label"]; assert d["scope"] == "default"' 2>/dev/null && ok "the account home with standard roots keeps the compatible default label (read-only preview)" || bad "default label for the account home (out: $out)"
LA="$(sched "$HOME_A" preview | jq_ 'print(d["label"])')"
case "$LA" in com.communicate.com8.snapshots.*) ok "an isolated home gets a scoped label ($LA)";; *) bad "scoped label (got: $LA)";; esac
[ "$(sched "$HOME_A" preview | jq_ 'print(d["label"])')" = "$LA" ] && ok "…which is stable across runs" || bad "label not stable"
LB="$(sched "$HOME_B" preview | jq_ 'print(d["label"])')"
[ "$LB" != "$LA" ] && ok "…and different for a different home" || bad "two homes share a label"
out="$(XDG_STATE_HOME="$T/xdg-state" python3 "$SCHED" preview --home "$ACCOUNT_HOME" --runtime "$HERE/profiles/runtime" --platform darwin 2>&1)"
printf '%s' "$out" | jq_ 'assert d["scope"] == "scoped" and d["label"] != "com.communicate.com8.snapshots"' 2>/dev/null && ok "a nonstandard state root scopes even the account home" || bad "XDG scope (out: $out)"
[ ! -s "$LOGF" ] && ok "preview called no service manager" || bad "preview called the manager"
out="$(HOME="$HOME_A" python3 "$SCHED" preview --runtime "$HERE/profiles/runtime" --platform darwin 2>&1)"
printf '%s' "$out" | jq_ "assert d['label'] == '$LA' and d['scope'] == 'scoped'" 2>/dev/null && ok "with no --home the selection follows \$HOME (the wrapper's case) and is scoped there" || bad "\$HOME default (out: $out)"
out="$(env -u HOME -u XDG_CONFIG_HOME -u XDG_STATE_HOME python3 "$SCHED" preview --runtime "$HERE/profiles/runtime" --platform darwin 2>&1)"
printf '%s' "$out" | jq_ 'assert d["label"] == "com.communicate.com8.snapshots" and d["scope"] == "default"' 2>/dev/null && ok "with no HOME at all the account home is selected (read-only preview)" || bad "no-HOME default (out: $out)"

echo "-- install/uninstall across two homes leave each other alone"
out="$(sched "$HOME_A" install)"; rc=$?
PA="$(plist_of "$HOME_A")"
[ $rc -eq 0 ] && [ -f "$PA" ] && [ "$(cat "$LAUNCHD/$LA")" = "$PA" ] && ok "A installs and the manager holds A's label from A's file" || bad "install A (rc=$rc out: $out)"
hours="$(python3 -c 'import plistlib,sys; print(" ".join(str(e["Hour"]) for e in plistlib.load(open(sys.argv[1], "rb"))["StartCalendarInterval"]))' "$PA" 2>/dev/null)"
[ "$hours" = "3 9 15 21" ] && ok "…StartCalendarInterval fires at 03/09/15/21" || bad "plist hours (got: $hours)"
grep -q 'KeepAlive\|RunAtLoad\|StartInterval' "$PA" && bad "plist is a daemon or drifting timer" || ok "…one-shot job: no KeepAlive/RunAtLoad/StartInterval"
[ "$(_mode "$HOME_A/Library/Logs")" = 700 ] && [ "$(_mode "$HOME_A/Library/Logs/com8-snapshot.out.log")" = 600 ] && [ "$(_mode "$HOME_A/Library/Logs/com8-snapshot.err.log")" = 600 ] && ok "…a fresh launchd log dir is 0700 with its two log files pre-created 0600" || bad "log dir/file modes: $(_mode "$HOME_A/Library/Logs" 2>/dev/null) $(_mode "$HOME_A/Library/Logs/com8-snapshot.out.log" 2>/dev/null)"
WA="$HOME_A/.local/bin/com8-snapshot"
[ -x "$WA" ] && grep -q "$WA" "$PA" && ok "…the job runs A's owned wrapper" || bad "wrapper/ProgramArguments"
python3 -c "import json,sys; e=json.load(open(sys.argv[1]))['entries']; assert e[sys.argv[2]]['owner']=='snapshots-schedule' and e[sys.argv[3]]['owner']=='snapshots-schedule'" "$HOME_A/.local/state/com8/profiles/ownership.json" "$PA" "$WA" 2>/dev/null && ok "…both files are ledger entries tagged as schedule-owned" || bad "ledger tags"
out="$(sched "$HOME_B" install)"; rc=$?
[ $rc -eq 0 ] && [ -e "$LAUNCHD/$LB" ] && [ -e "$LAUNCHD/$LA" ] && ok "B installs beside A in the same domain" || bad "install B (rc=$rc out: $out)"
n="$(calls)"; out="$(sched "$HOME_A" install)"; rc=$?
[ $rc -eq 0 ] && printf '%s' "$out" | jq_ 'assert d["action"] == "unchanged" and d["restarted"] is False' 2>/dev/null && [ "$(calls)" -le $((n + 1)) ] && ok "an identical, owned, loaded schedule is not rewritten or restarted (one status query only)" || bad "idempotent A (rc=$rc calls: $n -> $(calls) out: $out)"
out="$(sched "$HOME_B" uninstall)"; rc=$?
[ $rc -eq 0 ] && [ ! -e "$LAUNCHD/$LB" ] && [ -e "$LAUNCHD/$LA" ] && [ -f "$PA" ] && ok "uninstalling B unloads only B; A stays loaded from its file" || bad "uninstall B (rc=$rc out: $out)"

echo "-- an uninstall with no schedule-owned entries is inert"
n="$(calls)"; out="$(sched "$HOME_C" uninstall)"; rc=$?
[ $rc -eq 0 ] && printf '%s' "$out" | jq_ 'assert d["inert"] is True and d["removed"] == []' 2>/dev/null && [ "$(calls)" -eq "$n" ] && ok "no owned entries: ok, inert, and the manager was never called" || bad "inert uninstall (rc=$rc calls $n -> $(calls) out: $out)"

echo "-- a job loaded from a path that is not ours is never touched"
printf '%s\n' "/elsewhere/$LA.plist" > "$LAUNCHD/$LA"
n="$(calls)"; out="$(sched "$HOME_A" status)"
printf '%s' "$out" | jq_ 'assert d["loaded"] is True and d["ours"] is False and d["loaded_path"].startswith("/elsewhere/")' 2>/dev/null && ok "status reports the label loaded from a foreign path" || bad "foreign status (out: $out)"
before="$(cat "$PA")"; out="$(sched "$HOME_A" uninstall)"; rc=$?
[ $rc -ne 0 ] && [ "$(cat "$PA")" = "$before" ] && ! grep -q 'bootout' <(tail -n +$((n+1)) "$LOGF") && ok "uninstall refuses: no bootout, files kept" || bad "foreign uninstall (rc=$rc out: $out)"
cp -R "$HERE/profiles/runtime" "$T/runtime2"
out="$(python3 "$SCHED" install --home "$HOME_A" --runtime "$T/runtime2" --platform darwin --manager "$FAKEBIN/launchctl" 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ "$(cat "$PA")" = "$before" ] && ! grep -q 'bootout\|bootstrap' <(tail -n +$((n+1)) "$LOGF") && ok "an update refuses too: nothing written, nothing loaded" || bad "foreign update (rc=$rc out: $out)"
printf '%s\n' "$PA" > "$LAUNCHD/$LA"

echo "-- a failed reactivation restores the previous files AND the previous loaded job"
out="$(sched "$HOME_E" install)"; PE="$(plist_of "$HOME_E")"; LE="$(label_of "$HOME_E")"; WE="$HOME_E/.local/bin/com8-snapshot"
[ -e "$LAUNCHD/$LE" ] && ok "E installed and loaded" || bad "install E (out: $out)"
v1="$(cat "$WE")"; touch "$T/bootstrap-fails-once"; n="$(calls)"
out="$(python3 "$SCHED" install --home "$HOME_E" --runtime "$T/runtime2" --platform darwin --manager "$FAKEBIN/launchctl" 2>&1)"; rc=$?
[ $rc -ne 0 ] && ok "the update whose bootstrap fails exits nonzero" || bad "failed update rc=$rc"
[ "$(cat "$WE")" = "$v1" ] && ok "…the wrapper is back to its previous bytes" || bad "wrapper not restored"
python3 -c "import hashlib,json,sys; e=json.load(open(sys.argv[1]))['entries'][sys.argv[2]]; assert e['installed_hash']==hashlib.sha256(open(sys.argv[2],'rb').read()).hexdigest()" "$HOME_E/.local/state/com8/profiles/ownership.json" "$WE" 2>/dev/null && ok "…and the ledger hash matches the restored file" || bad "ledger hash after restore"
[ "$(cat "$LAUNCHD/$LE" 2>/dev/null)" = "$PE" ] && ok "…and the previous job is loaded again from our file" || bad "previous activation not restored"
grep -c 'bootstrap' <(tail -n +$((n+1)) "$LOGF") | grep -q '^2$' && ok "…one failed bootstrap, one restoring bootstrap" || bad "bootstrap sequence: $(tail -n +$((n+1)) "$LOGF" | tr '\n' ';')"
printf '%s' "$out" | grep -qi 'previous' && ok "…and the error says the previous schedule was put back" || bad "error text (out: $out)"

echo "-- a wrapper the profile installed is shared: used, never retagged, never removed by the schedule"
mkdir -p "$HOME_F/.local/bin" "$HOME_F/.local/state/com8/profiles"
WF="$HOME_F/.local/bin/com8-snapshot"
sched "$HOME_F" preview | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(f["content"] for f in d["files"] if f["path"].endswith("/com8-snapshot")), end="")' > "$WF"; chmod 755 "$WF"
python3 - "$HOME_F/.local/state/com8/profiles/ownership.json" "$WF" <<'PY'
import hashlib, json, sys
p, w = sys.argv[1:3]
json.dump({"schema": 1, "entries": {w: {"kind": "file", "original": {"kind": "absent"},
          "installed_hash": hashlib.sha256(open(w, "rb").read()).hexdigest(), "block": None}}}, open(p, "w"), indent=2)
PY
out="$(sched "$HOME_F" install)"; rc=$?
[ $rc -eq 0 ] && printf '%s' "$out" | jq_ 'assert any(f["action"] == "shared" and f["path"].endswith("/com8-snapshot") for f in d["files"])' 2>/dev/null && ok "install uses the profile-owned wrapper as shared" || bad "shared wrapper install (rc=$rc out: $out)"
python3 -c "import json,sys; e=json.load(open(sys.argv[1]))['entries'][sys.argv[2]]; assert 'owner' not in e" "$HOME_F/.local/state/com8/profiles/ownership.json" "$WF" 2>/dev/null && ok "…without retagging its ledger entry" || bad "wrapper entry retagged"
out="$(sched "$HOME_F" uninstall)"; rc=$?
PF="$(plist_of "$HOME_F")"
[ $rc -eq 0 ] && [ ! -e "$PF" ] && [ -x "$WF" ] && ok "uninstall removes the schedule's plist and keeps the profile's wrapper" || bad "shared wrapper uninstall (rc=$rc out: $out)"
python3 -c "import json,sys; e=json.load(open(sys.argv[1]))['entries']; assert sys.argv[2] in e and 'owner' not in e[sys.argv[2]]" "$HOME_F/.local/state/com8/profiles/ownership.json" "$WF" 2>/dev/null && ok "…with its ledger metadata intact" || bad "wrapper ledger metadata lost"

echo "-- ownership rules and platform boundaries"
printf 'user edit\n' >> "$WA"
out="$(sched "$HOME_A" uninstall)"; rc=$?
[ $rc -ne 0 ] && [ -e "$WA" ] && [ -e "$LAUNCHD/$LA" ] && ok "uninstall refuses an edited owned file and unloads nothing" || bad "edited-file refusal (rc=$rc out: $out)"
python3 - "$WA" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); p.write_text(p.read_text().replace("user edit\n", ""))
PY
out="$(sched "$HOME_A" uninstall)"; rc=$?
[ $rc -eq 0 ] && [ ! -e "$PA" ] && [ ! -e "$WA" ] && [ ! -e "$LAUNCHD/$LA" ] && ok "uninstall A unloads and removes both owned files" || bad "uninstall A (rc=$rc out: $out)"
mkdir -p "$(dirname "$PA")"; printf 'someone else\n' > "$PA"
out="$(sched "$HOME_A" install)"; rc=$?
[ $rc -ne 0 ] && [ "$(cat "$PA")" = "someone else" ] && ok "install refuses to replace an unowned existing LaunchAgent" || bad "unowned plist refusal (rc=$rc out: $out)"
rm -f "$PA"
touch "$T/bootstrap-fails"; n="$(calls)"
out="$(sched "$HOME_G" install)"; rc=$?
PG="$HOME_G/Library/LaunchAgents/$(sched "$HOME_G" preview | jq_ 'print(d["label"])').plist"
[ $rc -ne 0 ] && [ ! -e "$PG" ] && [ ! -e "$HOME_G/.local/bin/com8-snapshot" ] && ok "a first install whose load fails leaves no files behind" || bad "failed first load left files (rc=$rc out: $out)"
python3 -c "import json,sys,os; p=sys.argv[1]; d=json.load(open(p)) if os.path.exists(p) else {'entries':{}}; assert not d['entries']" "$HOME_G/.local/state/com8/profiles/ownership.json" 2>/dev/null && ok "…and no ledger entries" || bad "failed load left ledger entries"
rm -f "$T/bootstrap-fails"
NON_NATIVE="$(python3 -c 'import sys; print("darwin" if sys.platform == "linux" else "linux")')"
out="$(python3 "$SCHED" install --home "$HOME_G" --runtime "$HERE/profiles/runtime" --platform "$NON_NATIVE" 2>&1)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q -- '--manager' && ok "a non-native --platform cannot mutate without an explicit --manager fixture" || bad "non-native mutation (rc=$rc out: $out)"
out="$(python3 "$SCHED" preview --home "$HOME_G" --runtime "$HERE/profiles/runtime" --platform linux 2>&1)"
printf '%s' "$out" | grep -q 'OnCalendar=\*-\*-\* 03,09,15,21:00:00' && printf '%s' "$out" | grep -q 'Persistent=true' && ok "…while rendering the Linux persistent 03/09/15/21 timer is fine" || bad "linux timer rendering (out: $out)"

echo "== the rendered environment carries the selected HOME and supplied XDG roots, nothing else ambient"
out="$(XDG_STATE_HOME="$T/xdg-a" SECRET_TOKEN=hunter2 sched "$HOME_A" preview)"
printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin); plist = next(f["content"] for f in d["files"] if f["path"].endswith(".plist"))
assert "<key>XDG_STATE_HOME</key>" in plist and sys.argv[1] in plist, "XDG_STATE_HOME not baked"
assert "<key>HOME</key>" in plist and sys.argv[2] in plist, "HOME not baked"
assert "SECRET_TOKEN" not in plist and "hunter2" not in plist, "ambient env leaked"
assert d["label"] != sys.argv[3], "XDG state root did not scope the label"' "$T/xdg-a" "$HOME_A" "$LA" 2>/dev/null && ok "macOS: plist bakes HOME and a supplied XDG_STATE_HOME, no ambient variable, and scopes the label" || bad "plist env rendering (out: $out)"
out="$(XDG_CONFIG_HOME="$T/xdg-cfg" HOME="$HOME_L" python3 "$SCHED" preview --home "$HOME_L" --runtime "$HERE/profiles/runtime" --platform linux 2>&1)"
printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin); svc = next(f["content"] for f in d["files"] if f["path"].endswith(".service"))
assert "Environment=\"HOME=%s\"" % sys.argv[1] in svc, svc
assert "Environment=\"XDG_CONFIG_HOME=%s\"" % sys.argv[2] in svc, svc
assert all(f["path"].startswith(sys.argv[2] + "/systemd/user/") for f in d["files"] if f["path"].endswith((".service", ".timer"))), "units not under XDG_CONFIG_HOME"' "$HOME_L" "$T/xdg-cfg" 2>/dev/null && ok "Linux: the service bakes a quoted HOME and XDG_CONFIG_HOME, and the units live under that root" || bad "service env rendering (out: $out)"
out="$(XDG_STATE_HOME="relative/state" sched "$HOME_A" preview)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q "absolute" && ok "a relative XDG root is refused rather than baked" || bad "relative XDG accepted (rc=$rc out: $out)"
out="$(HOME="$HOME_A" bash "$SNAP" schedule preview --runtime "$HERE/profiles/runtime" --platform darwin 2>&1)"
printf '%s' "$out" | jq_ "assert d['label'] == '$LA'" 2>/dev/null && ok "the com8-snapshot schedule dispatch keeps the invocation HOME's scope" || bad "dispatch scope (out: $out)"

echo "== an unload the manager refuses keeps every file and entry, and a retry finishes the job"
out="$(sched "$HOME_H" install)"; LH="$(label_of "$HOME_H")"; PH="$(plist_of "$HOME_H")"; WH="$HOME_H/.local/bin/com8-snapshot"
[ -e "$LAUNCHD/$LH" ] && ok "H installed and loaded" || bad "install H (out: $out)"
touch "$T/bootout-fails"; n="$(calls)"
out="$(sched "$HOME_H" uninstall)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | jq_ 'assert d["ok"] is False and d["unloaded"] is False' 2>/dev/null && ok "a refused bootout makes uninstall answer ok:false, unloaded:false" || bad "refused bootout result (rc=$rc out: $out)"
[ -f "$PH" ] && [ -x "$WH" ] && ok "…every file is still there" || bad "files removed despite the loaded job"
python3 -c "import json,sys; e=json.load(open(sys.argv[1]))['entries']; assert e[sys.argv[2]]['owner']=='snapshots-schedule' and e[sys.argv[3]]['owner']=='snapshots-schedule'" "$HOME_H/.local/state/com8/profiles/ownership.json" "$PH" "$WH" 2>/dev/null && ok "…and the ledger still owns them" || bad "ledger entries dropped despite the loaded job"
[ -e "$LAUNCHD/$LH" ] && ok "…while the job is still loaded (as the manager says)" || bad "manager state inconsistent"
rm -f "$T/bootout-fails"
out="$(sched "$HOME_H" uninstall)"; rc=$?
[ $rc -eq 0 ] && [ ! -e "$LAUNCHD/$LH" ] && [ ! -e "$PH" ] && [ ! -e "$WH" ] && ok "the retry unloads and removes both files" || bad "retry (rc=$rc out: $out)"

echo "== a service manager that errors is unknown, never absent, before anything destructive"
out="$(sched "$HOME_A" install)"; [ -e "$LAUNCHD/$LA" ] && ok "A reinstalled for the ambiguity cases" || bad "reinstall A (out: $out)"
touch "$T/launchctl-broken"; n="$(calls)"
out="$(sched "$HOME_A" status)"
printf '%s' "$out" | jq_ 'assert d["loaded"] is None' 2>/dev/null && ok "status reports loaded: null when the manager errors" || bad "broken status (out: $out)"
out="$(sched "$HOME_A" uninstall)"; rc=$?
[ $rc -ne 0 ] && [ -f "$PA" ] && ! grep -q 'bootout' <(tail -n +$((n+1)) "$LOGF") && ok "uninstall refuses on an unknown manager state: no bootout, files kept" || bad "broken uninstall (rc=$rc out: $out)"
out="$(python3 "$SCHED" install --home "$HOME_A" --runtime "$T/runtime2" --platform darwin --manager "$FAKEBIN/launchctl" 2>&1)"; rc=$?
[ $rc -ne 0 ] && ! grep -q 'bootout\|bootstrap' <(tail -n +$((n+1)) "$LOGF") && ok "an update refuses too" || bad "broken update (rc=$rc out: $out)"
rm -f "$T/launchctl-broken"
out="$(sched "$HOME_A" uninstall)"; rc=$?
[ $rc -eq 0 ] && [ ! -e "$LAUNCHD/$LA" ] && ok "A uninstalls once the manager answers again" || bad "uninstall A after recovery (rc=$rc out: $out)"

echo "== Linux: a failed reactivation restores enablement and activity separately (fake systemd fixture)"
lsched() { HOME="$HOME_L" python3 "$SCHED" "$@" --home "$HOME_L" --runtime "$HERE/profiles/runtime" --platform linux --manager "$FAKEBIN/systemctl" 2>&1; }
UL="$(lsched preview | jq_ 'print(d["unit"])').timer"
lstate() { cat "$SYSTEMD/$UL" 2>/dev/null | tr '\n' ' '; }
out="$(lsched install)"; rc=$?
[ $rc -eq 0 ] && [ "$(lstate)" = "enabled=1 active=1 " ] && ok "Linux install enables and starts the timer" || bad "linux install (rc=$rc state: $(lstate) out: $out)"
TL="$HOME_L/.config/systemd/user/$UL"; SL="${TL%.timer}.service"; v1="$(cat "$HOME_L/.local/bin/com8-snapshot")"
grep -q "Environment=\"HOME=$HOME_L\"" "$SL" && ok "…the installed service carries the selected HOME" || bad "service HOME missing: $(grep Environment "$SL" | tr '\n' ' ')"
printf 'enabled=1 active=0\n' > "$SYSTEMD/$UL"; touch "$T/systemd-enable-fails-once"
out="$(HOME="$HOME_L" python3 "$SCHED" install --home "$HOME_L" --runtime "$T/runtime2" --platform linux --manager "$FAKEBIN/systemctl" 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ "$(cat "$HOME_L/.local/bin/com8-snapshot")" = "$v1" ] && ok "a failed Linux update restores the previous files" || bad "linux failed update (rc=$rc out: $out)"
[ "$(lstate)" = "enabled=1 active=0 " ] && ok "…and an enabled-but-inactive timer is enabled and inactive again, not started" || bad "enabled-inactive collapsed to: $(lstate)"
printf 'enabled=0 active=1\n' > "$SYSTEMD/$UL"; touch "$T/systemd-enable-fails-once"
out="$(HOME="$HOME_L" python3 "$SCHED" install --home "$HOME_L" --runtime "$T/runtime2" --platform linux --manager "$FAKEBIN/systemctl" 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ "$(lstate)" = "enabled=0 active=1 " ] && ok "…and a disabled-but-active timer is disabled and active again, not enabled" || bad "disabled-active collapsed to: $(lstate) (rc=$rc out: $out)"
printf 'enabled=1 active=1\n' > "$SYSTEMD/$UL"; touch "$T/systemd-disable-fails"
out="$(lsched uninstall)"; rc=$?
[ $rc -ne 0 ] && [ -f "$TL" ] && [ -f "$SL" ] && [ "$(lstate)" = "enabled=1 active=1 " ] && ok "a refused disable keeps the units and the timer's state" || bad "linux refused disable (rc=$rc out: $out)"
rm -f "$T/systemd-disable-fails"
out="$(lsched uninstall)"; rc=$?
[ $rc -eq 0 ] && [ ! -f "$TL" ] && [ ! -f "$SL" ] && [ ! -e "$SYSTEMD/$UL" ] && ok "the retry disables, stops and removes both units" || bad "linux retry (rc=$rc out: $out)"
touch "$T/systemd-broken"
out="$(lsched status)"
printf '%s' "$out" | jq_ 'assert d["loaded"] is None' 2>/dev/null && ok "a Linux manager that cannot be reached reads as unknown" || bad "linux broken status (out: $out)"
rm -f "$T/systemd-broken"

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
