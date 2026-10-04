#!/usr/bin/env python3
"""Legacy persistence entry points must refuse before touching any service."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LegacyServiceGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-legacy-service-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "private home"
        self.fake = self.root / "tools"
        self.fake.mkdir()
        self.calls = self.root / "calls"
        for name in ["launchctl", "systemctl", "mkdir", "rm", "chmod", "mv", "ln", "sleep"]:
            script = self.fake / name
            script.write_text('#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$GUARD_CALLS"\nexit 77\n')
            script.chmod(0o755)
        py = self.fake / "python3"
        py.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$GUARD_CALLS"\nprintf "kernel dispatch fixture\\n"\n')
        py.chmod(0o755)
        for relative in ["Library/LaunchAgents/com.communicate.com8.plist",
                         "Library/LaunchAgents/private.override.plist",
                         ".config/systemd/user/communicate-com8.service",
                         "state/com8/control.token", "data/install.json"]:
            file = self.home / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("existing owner bytes: " + relative)
            file.chmod(0o600)
        self.env = {"HOME": str(self.home), "PATH": str(self.fake) + os.pathsep + os.environ.get("PATH", ""),
                    "COMM_STATE": str(self.home / "state"), "COMMUNICATE_DATA": str(self.home / "data"),
                    "GUARD_CALLS": str(self.calls), "COM8_LABEL": "private.override",
                    "COM8_SELF": "isolated-qualification", "LC_ALL": "C"}

    def snapshot(self):
        return {str(file.relative_to(self.home)): (stat.S_IMODE(file.lstat().st_mode),
                file.lstat().st_mtime_ns, file.read_bytes() if file.is_file() else None)
                for file in self.home.rglob("*")}

    def run_cli(self, prefix, args):
        return subprocess.run([*prefix, *args], env=self.env, text=True,
                              capture_output=True, timeout=10)

    def test_all_legacy_mutating_routes_refuse_without_side_effects(self):
        prefixes = [[str(ROOT / "bin/com8"), "daemon"],
                    [str(ROOT / "bin/communicate"), "com8"]]
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required to exercise public package entry points")
        prefixes += [[node, str(ROOT / "packages/communicate/src/com8.mjs"), "daemon"],
                     [node, str(ROOT / "packages/communicate/src/cli.mjs"), "com8"]]
        # Older wrappers may source the adapter and invoke its functions or
        # dispatcher directly. They must hit the same refusal without common.sh.
        prefixes += [["/bin/bash", "-c", 'source "$1/lib/com8.sh"; shift; com8_cmd "$@"', "guard", str(ROOT)],
                     ["/bin/bash", "-c", 'source "$1/lib/com8.sh"; shift; fn="com8_$1"; shift; "$fn" "$@"', "guard", str(ROOT)]]
        self.env["COMM_HOME"] = str(ROOT)
        before = self.snapshot()
        for prefix in prefixes:
            for verb in ["install", "uninstall"]:
                for extra in [[], ["--dry-run"], ["--yes", "--label=other.service"]]:
                    with self.subTest(entry=prefix[-1], verb=verb, extra=extra):
                        result = self.run_cli(prefix, [verb, *extra])
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("legacy daemon " + verb + " is disabled", result.stderr)
                        self.assertIn("com8 setup --no-clients --service" if verb == "install"
                                      else "also detaches owned client integrations", result.stderr)
                        self.assertFalse(self.calls.exists(), "legacy persistence invoked a mutation or kernel helper")
                        self.assertEqual(self.snapshot(), before, "existing configuration/state was modified")

    def test_status_and_pair_keep_existing_kernel_dispatch(self):
        for args in [["status"], ["pair", "fixture-device", "--no-install"]]:
            with self.subTest(args=args):
                self.calls.unlink(missing_ok=True)
                result = self.run_cli([str(ROOT / "bin/com8"), "daemon"], args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls.read_text().splitlines(),
                                 [str(self.fake / "mkdir") + " -p " + str(self.home / "state"),
                                  str(ROOT / "lib/com8.py") + " call " + " ".join(args)])

    def test_refusal_does_not_create_missing_runtime_state(self):
        shutil.rmtree(self.home / "state")
        before = self.snapshot()
        for verb in ["install", "uninstall"]:
            result = self.run_cli([str(ROOT / "bin/communicate"), "com8"], [verb])
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.calls.exists())
            self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
