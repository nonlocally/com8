# Actual Linux service qualification

`scripts/qualify-systemd.py` tests an extracted release against a real systemd
user manager. It runs only with `--disposable-ci` on a non-root GitHub-hosted
Linux runner. Shared workstations, Minis, self-hosted runners and ordinary local
invocations are refused before artifact reads, file creation or process calls.
The independent `systemd` CI job builds its own release and uploads `report.json`.
A local syntax check or refusal check does not qualify the Linux service.

The test keeps the runner account's real `HOME` and systemd's default
`~/.config/systemd/user` search path. Fresh short `/tmp` data/state roots derive
one unique COM8 service name. Socket, session and provider directories are
explicitly inherited private fixture paths; no provider is installed or invoked.
Existing COM8 unit files and manager state, plus Claude/Codex config files, are
fingerprinted before setup and compared afterwards.

The acceptance sequence checks:

- CLI-only `setup --no-clients --service` starts the installed source commit,
  with the same PID reported by the daemon and actual user manager.
- Claim/send/inbox stores a literal message in a durable identity.
- Repeated identical setup keeps the PID, start time, unit bytes and release.
- An explicit manager restart changes the PID while retaining identity and mail.
- The first uninstall invocation unloads the unit and stops its process while
  preserving identity and mail. Cleanup retries cannot turn a failure into a pass.
- The input artifact and pre-existing COM8 units/client config stay unchanged.

If the runner has no user manager, `--start-user-manager` permits the documented
`loginctl enable-linger USER` and `systemctl start user@UID.service` commands via
noninteractive sudo. The harness restores lingering if it enabled it. It leaves
the runner user manager itself running until disposal; it never stops unrelated
user services. Bootstrap is refused when pre-existing COM8 user units could be
activated by starting the manager. COM8 cleanup uses the installer's ownership checks for the single
scoped unit. A rejected cleanup preserves evidence and fails the gate.

Example inside the disposable CI runner, after extracting the built archive:

```sh
python3 scripts/qualify-systemd.py "$RUNNER_TEMP/com8-systemd-artifact/com8-$(cat VERSION)" \
  --work "$RUNNER_TEMP/com8-systemd-evidence" --disposable-ci --start-user-manager
```

The JSON report records commands and exit codes, checks actually reached,
runtime provenance, manager PIDs and cleanup failures. It contains no supplied
credentials. The report and work directory use private permissions. This gate
does not qualify model clients, cross-device routing or snapshot schedules.
