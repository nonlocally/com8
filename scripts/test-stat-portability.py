#!/usr/bin/env python3
"""Execute shipped socket/probe/cache paths with GNU, BSD and host stat behavior.

Unsupported stat options may write stdout before failing. A fallback must replace
that output, not append to it. All directories and commands here are fixtures;
no SSH, providers, services or shared socket directories are contacted.
"""
import ast
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import com8_adopt


STAT = r'''import os, sys
style = os.environ['STAT_STYLE']
if style == 'native':
    os.execv(os.environ['REAL_STAT'], [os.environ['REAL_STAT'], *sys.argv[1:]])
option, fmt, target = sys.argv[1:]
if option != ('-c' if style == 'gnu' else '-f') or os.environ.get('STAT_FAIL'):
    print('File: fixture filesystem output from rejected stat option')
    sys.exit(1)
st = os.stat(target)
if fmt == '%u': print(os.environ.get('STAT_OWNER', st.st_uid))
elif fmt in ('%a', '%Lp'): print(os.environ.get('STAT_MODE', oct(st.st_mode & 0o777)[2:]))
elif fmt in ('%Y', '%m'): print(int(st.st_mtime))
else: sys.exit(2)
'''


class StatPortability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-stat-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home, self.bin = self.root / "home", self.root / "bin"
        self.home.mkdir(mode=0o700)
        self.bin.mkdir()
        self.socket_dir = self.root / "cc-socks"
        self.socket_dir.mkdir(mode=0o700)
        self.bash = shutil.which("bash")
        self.env = {"PATH": str(self.bin) + os.pathsep + os.defpath,
                    "HOME": str(self.home), "COMM_STATE": str(self.root / "state"),
                    "COMM_HOME": str(ROOT), "REAL_STAT": shutil.which("stat"),
                    "COM8_PROFILE_STATE": str(self.root / "profile-state")}
        (self.bin / "stat").write_text("#!" + sys.executable + "\n" + STAT)
        (self.bin / "stat").chmod(0o755)
        # The probe's login-shell call must not read the account's real profile.
        login = self.bin / "login-shell"
        login.write_text("#!/bin/sh\nprintf 'COM8_SELF_VALUE=fixture-device\\n'\n")
        login.chmod(0o755)
        self.env["SHELL"] = str(login)
        for command in ("ssh", "tailscale", "claude", "codex", "launchctl", "systemctl"):
            target = self.bin / command
            target.write_text("#!/bin/sh\necho 'unexpected external command' >&2\nexit 97\n")
            target.chmod(0o755)

    def run_shell(self, script, style, **env):
        return subprocess.run([self.bash, "-c", script], env={**self.env, "STAT_STYLE": style, **env},
                              cwd=self.home, capture_output=True, text=True, timeout=10)

    def common(self, style, **env):
        return self.run_shell('source "$COMM_HOME/lib/common.sh"; comm_ensure_socket_dir ' +
                              shlex.quote(str(self.socket_dir)), style, **env)

    def test_owned_socket_directory_passes(self):
        for style in ("gnu", "bsd", "native"):
            with self.subTest(style=style):
                result = self.common(style)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_owner_mode_or_failed_stat_is_refused(self):
        for style in ("gnu", "bsd"):
            for env in ({"STAT_OWNER": str(os.getuid() + 1)}, {"STAT_MODE": "755"}, {"STAT_FAIL": "1"}):
                with self.subTest(style=style, env=env):
                    result = self.common(style, **env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("refusing socket dir", result.stderr)

    def test_adoption_probe_distinguishes_owned_and_foreign_socket_directory(self):
        script = com8_adopt.probe_script("fixture@unused.invalid")
        script = script.replace("/tmp/cc-socks", shlex.quote(str(self.socket_dir)))
        for style in ("gnu", "bsd", "native"):
            for foreign in (False, True) if style != "native" else (False,):
                with self.subTest(style=style, foreign=foreign):
                    env = {"STAT_OWNER": str(os.getuid() + 1)} if foreign else {}
                    result = self.run_shell(script, style, **env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(com8_adopt.parse_facts(result.stdout)["cc_collision"], foreign, result.stdout)

    def test_pair_probe_only_steers_foreign_socket_directory(self):
        # Execute the actual remote shell payload without running pair or SSH.
        tree = ast.parse((ROOT / "lib/com8.py").read_text())
        scripts = [node.args[1].value for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "_pair_ssh" and len(node.args) > 1
                   and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                   and "stat " in node.args[1].value and "/tmp/cc-socks" in node.args[1].value]
        self.assertEqual(len(scripts), 1)
        script = scripts[0].replace("/tmp/cc-socks", shlex.quote(str(self.socket_dir)))
        for style in ("gnu", "bsd", "native"):
            for foreign in (False, True) if style != "native" else (False,):
                with self.subTest(style=style, foreign=foreign):
                    env = {"STAT_OWNER": str(os.getuid() + 1)} if foreign else {}
                    result = self.run_shell(script, style, **env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected = str(self.home / ".local/run/cc-socks") + "\n" if foreign else ""
                    self.assertEqual(result.stdout, expected)

    def test_mesh_fresh_cache_does_not_contact_remote(self):
        cache = Path(self.env["COM8_PROFILE_STATE"]) / "mesh/profiles/fixture-device.json"
        cache.parent.mkdir(parents=True)
        content = '{"hostname":"fixture-device","cached":true}\n'
        cache.write_text(content)
        script = 'source "$COMM_HOME/profiles/runtime/fns/mesh"; _mesh_probe fixture-device'
        for style in ("gnu", "bsd", "native"):
            with self.subTest(style=style):
                result = self.run_shell(script, style)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, content)
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
