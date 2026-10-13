#!/usr/bin/env python3
"""Real isolated daemon startup/shutdown barriers; no operator services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from com8_fixture_lifecycle import lifecycle

ROOT = Path(__file__).resolve().parents[1]


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-life-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.state = self.root / "state/com8"
        self.state.mkdir(parents=True)
        tools = self.root / "tools"
        tools.mkdir()
        wrapper = tools / "python3"
        wrapper.write_text(f'''#!{sys.executable}
import os,sys,time
from pathlib import Path
if len(sys.argv)>2 and sys.argv[1]=={str(ROOT / 'lib/com8.py')!r} and sys.argv[2]=='daemon':
    sys.path.insert(0,{str(ROOT / 'lib')!r})
    import com8
    root=Path(os.environ['FIXTURE_ROOT'])
    def barrier(name):
        if os.environ.get('FIXTURE_BARRIER')!=name:
            return
        (root/('entered-'+name)).touch()
        deadline=time.monotonic()+15
        while not (root/('release-'+name)).exists():
            if time.monotonic()>=deadline:
                raise RuntimeError('fixture barrier timed out: '+name)
            time.sleep(.02)
    load=com8.Com8._load_identities
    def delayed_load(self):
        barrier('load')
        return load(self)
    com8.Com8._load_identities=delayed_load
    shutdown=com8.Com8.shutdown
    def delayed_shutdown(self,*args):
        barrier('stop')
        return shutdown(self,*args)
    com8.Com8.shutdown=delayed_shutdown
    com8.Com8().run()
else:
    os.execv({sys.executable!r},[{sys.executable!r},*sys.argv[1:]])
''')
        wrapper.chmod(0o755)
        self.env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                    "HOME": str(self.root / "home"), "COMM_STATE": str(self.root / "state"),
                    "COM8_SOCK_DIR": str(self.root / "socks"),
                    "COM8_SESSIONS_DIR": str(self.root / "sessions"),
                    "COM8_SELF": "fixture", "COM8_TICK": "0.05",
                    "FIXTURE_ROOT": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("home", "socks", "sessions"):
            (self.root / name).mkdir()
        self.cli = ROOT / "bin/communicate"
        self.children = []

    def tearDown(self):
        for name in ("load", "stop"):
            (self.root / ("release-" + name)).touch()
        try:
            if (self.state / "daemon.pid").exists():
                lifecycle("stop", self.cli, self.state, env=self.env)
        finally:
            for child in self.children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
            self.temp.cleanup()

    def wait_for(self, predicate, detail):
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(detail)
            time.sleep(0.02)

    def spawn(self, command):
        child = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def test_start_cannot_publish_control_or_accept_claims_before_legacy_load(self):
        original = {"oldtimer": {"claimed_at": 1600000000.0, "kind": "local",
                                "home": None, "seat": "%42", "boxed": False}}
        identities = self.state / "identities.json"
        identities.write_text(json.dumps(original))
        self.env["FIXTURE_BARRIER"] = "load"
        start = self.spawn([str(self.cli), "com8", "start"])
        self.wait_for(lambda: (self.root / "entered-load").exists(), "loader not entered")
        self.assertFalse((self.state / "com8.sock").exists(), "control published during restore")
        self.assertIsNone(start.poll(), "start returned before restore completed")
        claim = subprocess.run([str(self.cli), "com8", "claim", "newcomer"],
                               env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(claim.returncode, 0, claim.stdout + claim.stderr)
        self.assertEqual(json.loads(identities.read_text()), original)
        (self.root / "release-load").touch()
        stdout, stderr = start.communicate(timeout=10)
        self.assertEqual(start.returncode, 0, stdout + stderr)
        loaded = json.loads(identities.read_text())["oldtimer"]
        self.assertEqual(loaded["claimed_at"], 1600000000.0)
        self.assertEqual(loaded["seat"], "%42")
        self.assertTrue({"workspace", "place", "surface", "card", "aliases"} <= loaded.keys())
        self.assertTrue((self.root / "socks/com8-oldtimer.sock").is_socket())
        claim = subprocess.run([str(self.cli), "com8", "claim", "newcomer"],
                               env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(claim.returncode, 0, claim.stdout + claim.stderr)
        self.assertEqual(set(json.loads(identities.read_text())), {"oldtimer", "newcomer"})

    def test_observed_stop_waits_for_actual_owned_shutdown(self):
        self.env["FIXTURE_BARRIER"] = "stop"
        lifecycle("start", self.cli, self.state, env=self.env)
        stop = self.spawn([sys.executable, str(ROOT / "scripts/com8_fixture_lifecycle.py"),
                           "stop", str(self.cli), str(self.state)])
        self.wait_for(lambda: (self.root / "entered-stop").exists(), "shutdown not entered")
        # The old fixture's 0.5s sleep would permit its next start here. This
        # barrier holds the real daemon, proving observation rather than luck.
        time.sleep(0.55)
        self.assertIsNone(stop.poll())
        self.assertTrue((self.state / "daemon.lock").is_dir())
        (self.root / "release-stop").touch()
        stdout, stderr = stop.communicate(timeout=5)
        self.assertEqual(stop.returncode, 0, stdout + stderr)
        for name in ("daemon.pid", "daemon.lock", "com8.sock"):
            self.assertFalse((self.state / name).exists(), name)
        lifecycle("start", self.cli, self.state, env=self.env)

    def test_failed_command_keeps_bounded_diagnostic_output(self):
        failing = self.root / "failing-cli"
        failing.write_text("#!/bin/sh\nprintf 'specific startup failure\\n' >&2\nexit 7\n")
        failing.chmod(0o755)
        (self.state / "daemon.log").write_text("x" * 10000 + "daemon failure detail\n")
        with self.assertRaisesRegex(RuntimeError, "start exited 7") as error:
            lifecycle("start", failing, self.state, env=self.env)
        self.assertIn("specific startup failure", str(error.exception))
        self.assertIn("daemon failure detail", str(error.exception))
        self.assertLess(len(str(error.exception)), 4500)

    def test_stuck_owned_shutdown_fails_with_observed_state(self):
        self.env["FIXTURE_BARRIER"] = "stop"
        lifecycle("start", self.cli, self.state, env=self.env)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "did not exit and release state") as error:
            lifecycle("stop", self.cli, self.state, env=self.env, timeout=1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("daemon.lock", str(error.exception))
        self.assertIn("daemon log tail", str(error.exception))
        self.assertTrue((self.root / "entered-stop").exists())
        (self.root / "release-stop").touch()
        self.wait_for(lambda: not (self.state / "daemon.pid").exists(), "owned daemon did not exit")


if __name__ == "__main__":
    unittest.main()
