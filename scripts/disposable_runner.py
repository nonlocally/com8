"""Fail-closed admission for tests that change a disposable CI guest.

Self-hosted labels and environment variables are not proof of isolation. A
provisioner outside the guest signs its identity and isolation guarantees; its
public key must be pinned in this checkout before qualification is admitted.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import time


ATTESTATION_ROOT = Path("/etc/nonlocally-ci")
TRUSTED_KEYS = Path(__file__).with_name("ci-provisioners.json")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def protected_file(path):
    """Accept only root-owned files beneath non-writable root-owned parents."""
    require(not path.is_symlink(), "disposable proof must not be a symlink")
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            "disposable proof must be a root-owned, non-writable regular file")
    for parent in [path.parent.resolve(), *path.parent.resolve().parents]:
        info = parent.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "disposable proof parent must be root-owned and non-writable")
    return path.read_bytes()


def validate_claim(claim, environment, current_platform, current_arch, now):
    require(isinstance(claim, dict) and claim.get("schema_version") == 1 and
            claim.get("kind") == "nonlocally-disposable-actions-guest", "unsupported disposable proof")
    require(isinstance(claim.get("guest_id"), str) and len(claim["guest_id"]) >= 16,
            "disposable proof lacks a unique guest identity")
    require(environment.get("RUNNER_NAME") and claim.get("runner_name") == environment["RUNNER_NAME"],
            "disposable proof belongs to another runner")
    require(claim.get("platform") == current_platform and claim.get("architecture") == current_arch,
            "disposable proof belongs to another platform")
    require(all(claim.get(field) is True for field in
                ("single_job", "no_host_mounts", "network_isolated")),
            "provisioner has not attested one-job, mount and network isolation")
    issued, expires = claim.get("issued_at"), claim.get("expires_at")
    require(all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in (issued, expires)), "disposable proof has invalid timestamps")
    require(issued <= now + 30 and issued < expires and now < expires and expires - issued <= 6 * 3600,
            "disposable proof is expired, future-dated or too long-lived")
    return {"environment": "self-hosted", "guest_id": claim["guest_id"], "runner_name": claim["runner_name"]}


def require_disposable_runner(selected, *, expected_platform=None):
    require(selected and os.getuid() != 0 and os.environ.get("GITHUB_ACTIONS") == "true",
            "requires --disposable-ci on a non-root disposable Actions runner; shared hosts are refused")
    require(expected_platform is None or sys.platform == expected_platform, "unsupported qualification platform")
    require(Path(os.environ.get("RUNNER_TEMP", "/nonexistent")).is_dir(), "RUNNER_TEMP is unavailable")
    environment = os.environ.get("RUNNER_ENVIRONMENT")
    if environment == "github-hosted":
        return {"environment": environment}
    require(environment == "self-hosted", "unknown runner environment; shared hosts are refused")
    marker, signature, key = (ATTESTATION_ROOT / name for name in
                              ("guest.json", "guest.sig", "provisioner-public.pem"))
    payload = protected_file(marker)
    protected_file(signature)
    public_key = protected_file(key)
    trusted = json.loads(TRUSTED_KEYS.read_text())["sha256"]
    require(hashlib.sha256(public_key).hexdigest() in trusted, "disposable provisioner key is not pinned")
    # Signature verification is read-only. No artifact, home, package manager or
    # service manager is touched before admission succeeds.
    openssl = Path("/usr/bin/openssl")
    require(openssl.is_file(), "system OpenSSL is required to verify disposable proof")
    checked = subprocess.run([str(openssl), "dgst", "-sha256", "-verify", str(key),
                              "-signature", str(signature), str(marker)],
                             text=True, capture_output=True, timeout=10)
    require(checked.returncode == 0, "disposable provisioner signature is invalid")
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine())
    return validate_claim(json.loads(payload), os.environ, sys.platform, architecture, time.time())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-ci", action="store_true")
    parser.add_argument("--expected-platform", choices=("linux", "darwin"))
    args = parser.parse_args()
    try:
        print(json.dumps(require_disposable_runner(args.disposable_ci, expected_platform=args.expected_platform)))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit("UNQUALIFIED: " + str(error))
