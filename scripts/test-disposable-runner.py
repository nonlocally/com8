#!/usr/bin/env python3
"""Disposable admission fixtures; no VM, service or package manager is used."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import disposable_runner as admission

sys.dont_write_bytecode = True


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="com8-admission-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = time.time()
        self.environment = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted",
                            "RUNNER_NAME": "fixture-single-job", "RUNNER_TEMP": str(self.root)}
        self.claim = {"schema_version": 1, "kind": "nonlocally-disposable-actions-guest",
                      "guest_id": "fixture-fresh-guest-123", "runner_name": "fixture-single-job",
                      "platform": sys.platform, "architecture": "arm64",
                      "issued_at": self.now - 5, "expires_at": self.now + 1800,
                      "single_job": True, "no_host_mounts": True, "network_isolated": True}

    def validate(self):
        return admission.validate_claim(self.claim, self.environment, sys.platform, "arm64", self.now)

    def test_each_missing_isolation_guarantee_is_refused(self):
        self.assertEqual(self.validate()["guest_id"], self.claim["guest_id"])
        for field in ("single_job", "no_host_mounts", "network_isolated"):
            with self.subTest(field=field), patch.dict(self.claim, {field: False}):
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_wrong_identity_platform_and_invalid_or_expired_times_are_refused(self):
        for values in ({"runner_name": "other"}, {"platform": "other"}, {"architecture": "other"},
                       {"guest_id": ""}, {"expires_at": self.now}, {"issued_at": self.now + 60},
                       {"expires_at": self.now + 7 * 3600}, {"issued_at": float("nan")},
                       {"issued_at": True}, {"expires_at": "forever"}):
            with self.subTest(values=values), patch.dict(self.claim, values):
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_opt_in_and_platform_guards_precede_proof_access(self):
        with patch.dict(os.environ, self.environment, clear=True), patch("os.getuid", return_value=501), \
                patch.object(admission, "protected_file") as read:
            for selected, expected in ((False, None), (True, "unsupported")):
                with self.assertRaises(RuntimeError):
                    admission.require_disposable_runner(selected, expected_platform=expected)
            read.assert_not_called()

    def test_missing_proof_cannot_admit_persistent_self_hosted_runner(self):
        with patch.dict(os.environ, self.environment, clear=True), patch("os.getuid", return_value=501), \
                patch.object(admission, "ATTESTATION_ROOT", self.root / "absent"):
            with self.assertRaises(OSError):
                admission.require_disposable_runner(True)

    def test_proof_files_reject_symlink_and_unprivileged_owner(self):
        ordinary = self.root / "ordinary"
        ordinary.write_text("fixture")
        link = self.root / "link"
        link.symlink_to(ordinary)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            admission.protected_file(link)
        if os.getuid() != 0:
            with self.assertRaisesRegex(RuntimeError, "root-owned"):
                admission.protected_file(ordinary)

    def test_real_signature_admits_only_pinned_unmodified_guest_claim(self):
        openssl = "/usr/bin/openssl"
        private = self.root / "private.pem"
        public = self.root / "provisioner-public.pem"
        payload = self.root / "guest.json"
        signature = self.root / "guest.sig"
        trusted = self.root / "trusted.json"
        subprocess.run([openssl, "genrsa", "-out", str(private), "2048"], check=True, capture_output=True)
        subprocess.run([openssl, "rsa", "-in", str(private), "-pubout", "-out", str(public)],
                       check=True, capture_output=True)
        payload.write_text(json.dumps(self.claim))
        subprocess.run([openssl, "dgst", "-sha256", "-sign", str(private), "-out", str(signature), str(payload)],
                       check=True, capture_output=True)
        trusted.write_text(json.dumps({"sha256": [hashlib.sha256(public.read_bytes()).hexdigest()]}))
        # The crypto path is real; only root ownership is substituted for the
        # unprivileged fixture directory, tested separately above.
        with patch.dict(os.environ, self.environment, clear=True), patch("os.getuid", return_value=501), \
                patch.object(admission, "ATTESTATION_ROOT", self.root), \
                patch.object(admission, "TRUSTED_KEYS", trusted), \
                patch.object(admission, "protected_file", side_effect=lambda path: path.read_bytes()), \
                patch("platform.machine", return_value="arm64"):
            self.assertEqual(admission.require_disposable_runner(True)["guest_id"], self.claim["guest_id"])
            payload.write_text(json.dumps({**self.claim, "guest_id": "tampered-guest-12345"}))
            with self.assertRaisesRegex(RuntimeError, "signature"):
                admission.require_disposable_runner(True)
            payload.write_text(json.dumps(self.claim))
            original_signature = signature.read_bytes()
            signature.write_bytes(bytes([original_signature[0] ^ 1]) + original_signature[1:])
            with self.assertRaisesRegex(RuntimeError, "signature"):
                admission.require_disposable_runner(True)
            signature.write_bytes(original_signature)
            trusted.write_text('{"sha256": []}')
            with self.assertRaisesRegex(RuntimeError, "not pinned"):
                admission.require_disposable_runner(True)

    def test_live_harnesses_refuse_before_opening_artifact_or_creating_evidence(self):
        scripts = Path(__file__).parent
        for script in ("qualify-systemd.py", "qualify-onboarding.py", "qualify-homebrew.py"):
            evidence = self.root / script
            arguments = [str(scripts / script), str(self.root / "missing-runtime")]
            if script == "qualify-homebrew.py":
                arguments.append(str(self.root / "missing-formula"))
            result = subprocess.run([sys.executable, "-B", *arguments, "--disposable-ci", "--work", str(evidence)],
                                    env={**os.environ, "GITHUB_ACTIONS": "false"}, capture_output=True, text=True)
            with self.subTest(script=script):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("shared hosts are refused", result.stderr)
                self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
