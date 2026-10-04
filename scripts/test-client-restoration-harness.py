#!/usr/bin/env python3
"""No clients or models: verify qualifier child cleanup and comparison semantics."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location("restoration", Path(__file__).with_name("qualify-client-restoration.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class QualifierSafety(unittest.TestCase):
    def test_only_known_empty_root_parent_tables_are_ignored(self):
        self.assertEqual(module.normalized({"plugins": {}, "enabledPlugins": {}, "permissions": {"allow": []},
                                            "explicit_empty": {}, "other": []}),
                         {"permissions": {"allow": []}, "explicit_empty": {}, "other": []})
        self.assertNotEqual(module.normalized({"permissions": {"allow": []}}), module.normalized({"permissions": {}}))
        self.assertNotEqual(module.normalized({"plugins": {"fixture": {}}}), module.normalized({"plugins": {}}))

    def test_owned_children_stop_on_parent_success_and_timeout(self):
        child = """import signal,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    with open(sys.argv[1], 'a') as out: out.write('heartbeat\\n')
    time.sleep(0.01)
"""
        parent = """import pathlib,subprocess,sys,time
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)
if sys.argv[3] == 'timeout': time.sleep(30)
"""
        for mode in ("success", "timeout"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="com8-qualifier-owned-") as root:
                heartbeat = Path(root) / "heartbeat"
                args = [sys.executable, "-c", parent, child, str(heartbeat), mode]
                if mode == "timeout":
                    with self.assertRaises(subprocess.TimeoutExpired):
                        module.run_owned(args, env=os.environ.copy(), cwd=root, timeout=0.25)
                else:
                    result = module.run_owned(args, env=os.environ.copy(), cwd=root, timeout=5)
                    self.assertEqual(result.returncode, 0)
                before = heartbeat.read_bytes()
                time.sleep(0.1)
                self.assertEqual(heartbeat.read_bytes(), before, "owned child survived qualifier command cleanup")


if __name__ == "__main__":
    unittest.main()
