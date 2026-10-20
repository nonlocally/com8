# Disposable Actions runners

CI requests our own ARM64 runners in the `nonlocally-disposable` group. The public
repository uses the separate `nonlocally-public-disposable` group, restricted to
its trusted `ci.yml` workflow on `main`. No job selects GitHub-hosted compute.
Labels alone never establish that a machine is disposable.

Every job checks its guest proof with the pinned public composite action before
checking out job code, installing dependencies or changing a service. `scripts/disposable_runner.py` retains the existing hosted-runner
admission for standalone harness compatibility, and additionally admits an
attested self-hosted guest. It never rewrites `RUNNER_ENVIRONMENT`.

The provisioner must create a fresh VM and unique runner name for exactly one
job, with no host-directory mounts, no host credentials, and network isolation
enforced outside the guest. Register an ephemeral runner, destroy the guest and
its writable disk after completion or cancellation, and never reuse a guest or
move its labels onto a persistent workstation. A normal runner user or another
temporary `HOME` does not provide these guarantees. The Linux guest needs a real
systemd user manager; the macOS guest needs a supported macOS and Homebrew.
Preinstall Python 3 and the system OpenSSL used by the admission check. Later
dependency installation and service bootstrap occur inside the guest only.

The host-only provisioner signs the exact bytes of `/etc/nonlocally-ci/guest.json`
with RSA/SHA-256 and installs the detached signature as `guest.sig` and its public
PEM as `provisioner-public.pem` beside it. These files and their parent directories
must be root-owned and not group/other writable. The signing key never enters the
guest. Its public PEM's SHA-256 must appear in committed `scripts/ci-provisioners.json`;
an arbitrary key supplied by the guest is refused. The committed public PEM is
included for provisioning and rotation review. Never sign a claim until actual
mount, network and fresh-guest checks pass.

The signed JSON contract is:

```json
{
  "schema_version": 1,
  "kind": "nonlocally-disposable-actions-guest",
  "guest_id": "unique-fresh-guest-identity",
  "runner_name": "the-exact-RUNNER_NAME",
  "platform": "linux",
  "architecture": "arm64",
  "issued_at": 1791820800,
  "expires_at": 1791835200,
  "single_job": true,
  "no_host_mounts": true,
  "network_isolated": true
}
```

`platform` is `linux` or `darwin`; `architecture` is `arm64` or `x64`. The claim
must match the actual runner and platform, be unexpired, and have a lifetime of
at most six hours. Proof admission supplements the harnesses' existing checks
for fresh evidence directories, absent COM8/provider commands and scoped service
ownership. A signed claim does not replace provisioner teardown and isolation.

PR checks use `pull_request_target` so a contributor cannot rewrite the admission
workflow before it runs. Only PRs whose head branch lives in this repository
execute their head commit: pushing such a branch takes write access, whereas
`author_association` is not a usable signal because a member whose organization
membership is private arrives as NONE. Forks and other untrusted PRs execute no
contributor code automatically; the final `qualification` check fails because
their qualification jobs did not run. They are unqualified, never a passing test.
For a reviewed public contribution, a maintainer dispatches the trusted workflow
on `main` with `source_ref` set to the reviewed full commit SHA, using the public
disposable pool. It never uses the private pool or a private-repository token.

PRs keep both Linux Node versions and macOS Node 22; other runs retain the full
Node 20/22 platform matrix. Only superseded PR runs are cancelled. Homebrew and
fresh dependency onboarding run for release qualification, including an explicit
`workflow_dispatch` with `release_qualification: true`. The public workflow runs
from `main` even when a reviewed release tag is supplied as `source_ref`; its
restricted group does not accept a different workflow ref. Private release-branch
and version-tag pushes also request Homebrew qualification.

The final `qualification` check requires successful core, dashboard and actual
systemd acceptance, plus Homebrew/onboarding when requested. Required-check
settings must use that gate after the real guest runs have been verified; a
queued runner, missing attestation or skipped platform does not establish support.
Ephemeral CI artifacts expire after seven days. Release archives and qualification
receipts require separate durable release publication.

Run `python3 -B scripts/test-disposable-runner.py` for refusal and real-signature
fixtures. It neither provisions a VM nor qualifies a service lifecycle. Missing
Linux or macOS infrastructure remains unqualified until an actual job succeeds.
