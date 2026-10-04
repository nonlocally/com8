# Optional workspace snapshots and archive

`com8-snapshot` takes a snapshot of the whole tmux workspace with the profile's
`tss`, mirrors it to an archive volume, prunes the local window, and logs one
line per run. It is an optional module of the workstation profile: nothing in
core setup or in `com8 profile install` takes snapshots or installs a schedule.

```sh
com8-snapshot run              # snapshot now (snap-YYYY-MM-DD-HHMM) + archive + prune
com8-snapshot list             # local snapshots, newest first (* = archived)
com8-snapshot restore 2026-09-24-0300   # a verified copy back from the archive
com8-snapshot status           # schedule, archive state, counts, recent log
com8-snapshot schedule preview # the owned 03/09/15/21 schedule, before installing it
```

The module lives at `profiles/runtime/modules/snapshots/` (`com8-snapshot` and
`schedule.py`), so an installed profile payload carries it. `com8 profile
install --snapshots` installs the stable wrapper `~/.local/bin/com8-snapshot`
and nothing else: no job, no schedule. `com8-snapshot schedule install` writes
that same wrapper when the profile did not, and treats a profile-installed one
as shared.

## Format, naming, retention

Snapshots are the `tss` directories (`meta.tsv`, layouts, resume commands) under
the sessions directory, named `snap-YYYY-MM-DD-HHMM`. Snapshots from the earlier
nightly cadence are named `nightly-YYYY-MM-DD`; every command finds, orders,
archives, prunes, verifies, lists and restores both schemes. Ordering uses the
embedded timestamp, never the bare name. The local window keeps the newest 56
snapshots (14 days at four a day). Restore with `tsr NAME` (`com8-workstation
shell tsr NAME` outside a profile shell).

## Archive safety

Each archived snapshot carries a `.manifest.sha256` written when it was
mirrored, and the local copy is marked `.archived` only after the mirrored
files verify against it. Two rules are stricter than the Anu original:

- **Prune deletes a local copy only when the archive volume is mounted right
  now and holds a copy that verifies against its manifest, whose manifest the
  local files also satisfy, with exactly the same file set.** A `.archived`
  marker alone, an unmounted or absent volume, a differing archive copy, or a
  local copy edited after archiving all keep the snapshot. Nothing is ever
  deleted from the archive.
- **Restore refuses an archived copy that fails its checksum** (or has no
  manifest). Nothing is written to the sessions directory; a good copy is
  restored through a temporary directory, verified, marked archived, and only
  then given its final name.
- **A manifest is an exact inventory.** Verification requires every listed
  path to be a safe relative path inside the snapshot, every listed file to
  match, the regular files present (marker and manifest aside) to be exactly
  the listing, and no symlink or special entry anywhere in the copy. A local
  snapshot holding a symlink is never marked archived and never pruned; an
  archive copy with an extra, special or traversing entry is refused for
  restore and reported corrupt by `verify`. Existing text-mode and binary-mode
  shasum manifests keep verifying.
- **The archive configuration itself is checked** before anything is mirrored,
  pruned, restored or verified: the volume must be exactly a mount point (the
  whole mount-point field, never a prefix or a pattern), the archive directory
  must resolve inside that volume, and the archive and the sessions directory
  must be disjoint — neither the same directory nor an ancestor or descendant
  of the other, with symlinks resolved through every existing ancestor. A
  configuration that fails is refused with the reason and deletes nothing.

Without a configured archive volume every snapshot stays local and the log
says so; nothing is pruned.

## Privacy of new data

Everything the module creates is private regardless of the caller's umask:
state parents, the sessions directory, the log, the run lock, manifests,
`.archived` markers and the archive directory are created under a scoped
`umask 077` (a subshell, so a shell that sources the module keeps its own
umask), and the schedule creates its launchd log directory 0700 with the two
log files pre-created 0600 when absent. Nothing that already exists is
re-moded: a pre-existing log, sessions directory or snapshot keeps its mode,
and `rsync -a` keeps the source's modes on every archived or restored copy,
so a copy that was never private is restored as it is rather than claimed
private. Reviewing the permissions of data that predates this module is a
separate step.

## Configuration

Private settings belong in `~/.config/com8/profiles/local.sh` (trusted shell,
loaded by every profile tool and by the scheduled run):

```sh
# where tss saves; snapshots are <state>/sessions, the log <state>/snapshots.log
export COM8_PROFILE_STATE="$HOME/.local/state/com8/workstation"
# the permanent archive: a mounted volume and a directory on it
export COM8_SNAPSHOT_VOL="/Volumes/YOUR-ARCHIVE"
export COM8_SNAPSHOT_ARCHIVE="$COM8_SNAPSHOT_VOL/com8-snapshots"
# optional overrides
# export COM8_SNAPSHOT_DIR="$COM8_PROFILE_STATE/sessions"
# export COM8_SNAPSHOT_LOG="$COM8_PROFILE_STATE/snapshots.log"
# export COM8_SNAPSHOT_KEEP=56
```

An existing Anu installation keeps its data in place by pointing
`COM8_PROFILE_STATE` at the directory that already holds `sessions/` and
`snapshots.log`, and `COM8_SNAPSHOT_ARCHIVE` at the existing archive
directory. Public defaults name no volume, host, or account.

## The schedule: explicit, owned, reversible

```sh
com8-snapshot schedule preview     # JSON: files, actions, the load command; writes nothing
com8-snapshot schedule install     # write + load; takes no snapshot itself
com8-snapshot schedule status      # loaded? which files are owned, edited, or missing
com8-snapshot schedule uninstall   # unload, then remove only unedited owned files
```

On macOS this is a one-shot LaunchAgent `com.communicate.com8.snapshots`
(`StartCalendarInterval` at 03:00, 09:00, 15:00 and 21:00, no `KeepAlive`, a
missed firing runs once on the next wake; logs in `~/Library/Logs`). On Linux it
is the user timer `communicate-com8-snapshots.timer` (`OnCalendar=*-*-*
03,09,15,21:00:00`, `Persistent=true`). Both run the owned wrapper
`~/.local/bin/com8-snapshot`.

Ownership boundaries:

- That compatible label is used only for the actual account home (from the
  password database, not `$HOME`) with standard config and state roots. Any
  other home, or an `XDG_CONFIG_HOME`/`XDG_STATE_HOME` override, gets a
  stable scoped label (`com.communicate.com8.snapshots.<tag>`), so an isolated
  or qualification installation can never reach the account's real job.
- Before anything is loaded or unloaded, the service manager is asked which
  file it loaded the label from. A job of our label loaded from another file
  is a collision: install, update and uninstall refuse and change nothing. A
  manager that cannot be run, or answers with an error or an unreadable
  reply, is *unknown*, never "absent": every mutation refuses on unknown.
- Uninstall confirms the job is really gone (macOS: absent; Linux: disabled
  and inactive) after asking the manager to unload it and *before* it
  deletes a single file or ledger entry. Until that confirmation every file
  and entry stays and the answer is not ok, so a retry can finish the job.
- An uninstall that finds no schedule-owned ledger entries is inert and never
  calls the service manager.
- An update that fails to activate restores the previous files and the
  previous activation: on macOS the previous job is loaded again; on Linux
  enablement and activity are restored separately to what they were (an
  enabled-but-inactive or disabled-but-active timer stays that way). An
  identical owned schedule that is already loaded is neither rewritten nor
  restarted.
- The job's environment is rendered, not inherited: the selected `HOME`, the
  supplied `XDG_CONFIG_HOME` and `XDG_STATE_HOME` (the same roots that scope
  the label, validated as absolute paths, so the run snapshots the selected
  state), a fixed `PATH`, `TMUX_TMPDIR`, and the UTF-8 locale. Nothing else
  from the ambient environment is baked in. The `com8-snapshot schedule`
  dispatch passes the invocation `HOME` explicitly, so an isolated wrapper
  stays scoped.
- `--platform` renders either platform for `preview`, but `install` and
  `uninstall` only drive the host's own manager unless an explicit
  `--manager PATH` fixture is given (that is how the tests run a fake
  launchd). On Linux, ownership of the unit files, enablement and activity are
  reported separately, and only this timer is enabled or disabled.
- A wrapper the profile installer owns is used as shared: never rewritten,
  retagged or removed here. Only a wrapper this schedule created is removed.

The files are owned through the profile ledger
(`~/.local/state/com8/profiles/ownership.json`) using the installer's own
conflict rules: an existing unowned file is never replaced, an owned file that
was edited is never removed, and `com8 profile status` lists them.

`com8 profile uninstall` handles an installed schedule itself. When the ledger
holds schedule-owned entries it first checks its own plan for conflicts, then
delegates to `com8-snapshot schedule uninstall` outside its lock; that step
verifies the job is loaded from the schedule's own file, unloads only that
job, and removes only unedited schedule-owned files. The profile uninstall
continues only when the schedule confirms the job unloaded and no
schedule-owned entries remain, reloads the ledger, and removes the rest. If the
schedule refuses (a foreign loaded path, an edited owned file, an unavailable
service manager), the profile uninstall stops with every file preserved. The
service manager is invoked only by the schedule's install and uninstall,
whether run directly or through the profile uninstall. `--home PATH --runtime
PATH --platform` select an isolated home, a runtime payload, and a rendering
for qualification; `--home` defaults to `$HOME`.

## Qualification

`bash scripts/test-snapshots.sh` runs under temporary homes with a fake
`mount`, `tmux` and a fake snapshotter first on PATH and one global fake
launchd named as an explicit `--manager` fixture: ordering across both naming
schemes, token resolution, both safety rules against a fixture archive
(unmounted volume, marker without a copy, differing archive bytes, locally
edited copy, corrupt restore), refused archive configurations (same or nested
directories, off-volume, symlink overlap, a longer volume name), symlink and
inventory rules, the unattended run, the retention default, and the schedule
across two isolated homes: scoped and default labels, install, identical-repeat
without restart, uninstall of one home leaving the other loaded, an inert
uninstall with nothing owned, a foreign loaded path refused, a failed
reactivation restoring files and the previous job, a shared profile-owned
wrapper retained, edited-file and unowned-file refusals, a non-native
platform refused for mutation, a refused unload that keeps every file and
entry until a retry succeeds, a manager that errors reading as unknown, the
rendered environment, a fake systemd fixture whose enabled/active flags
are independent (a failed Linux update restores each separately; a refused
disable keeps the units), and a fresh home under `umask 022` whose new state
parents, sessions directory, log, run lock, manifest, markers and archive
directory come out private while pre-existing modes and an imported
non-private archive copy are left as they are. It never touches real snapshots, a real volume, a
tmux server, or a service manager. Real launchd firing, a real external
volume, and real systemd remain environment-specific checks.
