# Release qualification

Test the actual artifact, installation and advertised behavior together. Existing
native communication, buses, durable COM8 and selected terminal helpers are the
baseline implementation.

## Build

Commit source and keep root/package/plugin release versions consistent:

```sh
python3 scripts/build-release.py
python3 scripts/render-homebrew-formula.py VERSION SHA256 /path/to/tap/Formula/com8.rb
```

The builder captures the current commit and uses an owned detached temporary Git
worktree, with checkout hooks disabled. Vendoring runs there; production npm
dependencies are installed only in the temporary archive stage. The development
checkout's `vendor/` and `node_modules/` are never rebuilt or replaced. The owned
worktree and staging directories are removed after success or a build exception;
unrelated worktrees are never pruned.

The committed lockfile supplies production dependencies and notices. Archive
metadata is normalized; the manifest records the actual source commit,
dependency integrity and file hashes. `--allow-dirty` is development-only: it
freezes tracked files and nonignored untracked files, including deletions,
rejects concurrent changes detected during capture, and records a source snapshot
digest and dirty markers in both manifests. Ignored dependencies/vendor output
are not source inputs. Edits after capture cannot change the packaged snapshot.
Publish
only the immutable archive whose checksum was actually tested. Formula installation
must not configure the user's machine automatically.

Run `python3 -B scripts/test-build-release.py --real-builds` from a clean commit
to verify two actual npm-backed builds have identical checksums, unchanged source
dependency/vendor fingerprints, and no temporary worktree registrations left
behind. Without `--real-builds`, it runs isolated fixture tests for source capture,
checkout-hook suppression, failure cleanup, and development provenance.

## Qualification

1. Run affected native/bus/durable/seat suites and integration tests.
2. Validate manifests, skills and CLI/MCP from the packed runtime.
3. Exercise fresh/repeat setup, upgrade, failed activation, rollback, selective
   uninstall and preserved-state reinstall in isolated environments.
4. Qualify optional terminal/mesh independently from core install.
5. On designated devices, disable conflicting legacy integration reversibly and
   install the artifact as a user would, without source-checkout fallback.
6. Open fresh supported clients and prove creation, exact registration, discovery,
   messaging, replies and dashboard/inbox operation with bounded test identities.
7. Prove cross-device enrollment/communication using the installed payloads.
8. Record platform/runtime versions and pass/fail/skipped results. Missing
   prerequisites do not establish support. Terminal delivery does not prove
   desktop wake; queue acceptance does not prove model consumption.

Use isolated fixtures on the developer's working Mac. Do not disable its existing
integrations for qualification; actual legacy-disabled installation testing belongs
on designated test devices.

CI runs the retained core gate on Linux and macOS with Node 20 and 22, then extracts
the built archive and runs `scripts/qualify-installed.py` against it. Its JSON
report is retained independently from the archive. Any explicitly unqualified
upgrade check remains open; a successful process exit does not turn omitted live
provider or previous-release checks into passes. See
[Provider and client qualification](PROVIDER-QUALIFICATION.md) for the opt-in
artifact harness, fresh installed-plugin acceptance, and two-device proof.

For a release changing hosted onboarding, also run the coordinated
[hosted qualification](HOSTED-QUALIFICATION.md) against the actual public HTTPS
deployment. It distinguishes general publication from private membership and
verifies replies from actual installed clients; isolated broker fixtures alone
do not establish deployed compatibility.

The independent [systemd qualification](SYSTEMD-QUALIFICATION.md) runs the Linux
service lifecycle against a real user manager on a disposable Ubuntu runner.
Fixture service tests alone do not establish that manager-level acceptance.

Also run the opt-in [client restoration matrix](CLIENT-RESTORATION-QUALIFICATION.md)
against the extracted release. It uses actual client registry commands in isolated
homes to verify that setup and removal restore prior enabled, disabled and custom
policy states. It makes no model requests; installed-provider acceptance remains
a separate check.

The dashboard job uses Playwright 1.63.0 with Chromium against the built archive's
actual assets. Run the same gate locally with
`uv run --with playwright==1.63.0 python scripts/qualify-browser.py /path/to/runtime --evidence /private/new-ui-evidence`
after installing its Chromium binary. It covers graph/conductor and human-inbox
behavior using isolated API fixtures; it does not establish real model delivery.

## Publication

Audit all retained Git refs/history, current source and archive. Verify historical
credential status and confidential material. Preserve a private backup before any
history treatment. Do not publish while a gate remains unresolved.

Include MIT and third-party notices. Publish the tested commit/tag, archive,
checksums, release notes and support matrix, then update the tap. Verify anonymous
download/install. npm publication is optional; never advertise an unpublished scope.
Public distribution does not grant access to private hosted buses.

0.6.0 is the first release whose tag assets (`com8-<version>.tar.gz` and its
`.sha256`), tap formula (`Formula/com8.rb`, class `Com8`) and release notes
carry the COM8 name. Its release notes must include the upgrade paragraph from
[docs/INSTALL.md](INSTALL.md) and the list of renamed MCP tools (`com8_*`). The
hosted bus protocol is unchanged, so hosted devices need not re-enroll.

This repository's kernel reports a semantic version (`COM8_VERSION` `"0.6.0"`),
while the private development repository's kernel uses a date-based value; the
private branch must adopt the public scheme before any cross-build `pair`.
