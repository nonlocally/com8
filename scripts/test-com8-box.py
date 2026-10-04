#!/usr/bin/env python3
"""Optional box adapter contract tests; a disposable fake container CLI only."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOX = ROOT / "profiles/runtime/box/box.py"


class BoxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-box-test-")
        self.root = Path(self.temp.name).resolve()
        self.home, self.work, self.bin = [self.root / p for p in ["home space", "work space", "bin"]]
        for directory in [self.home, self.work, self.bin]:
            directory.mkdir()
        self.log = self.root / "runtime-calls.jsonl"
        excluded = ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANU_ACCOUNT", "COM8_ACCOUNT"]
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("COM8_BOX_", "ANU_BOX_", "GIT_")) and k not in excluded}
        self.env.update(HOME=str(self.home), XDG_DATA_HOME=str(self.home / ".local/share"),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        COM8_BOX_RUNTIME=str(self.bin / "container"), BOX_FIXTURE_LOG=str(self.log),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        tool = self.bin / "container"
        tool.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
args = sys.argv[1:]
names = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--env"]
with open(os.environ["BOX_FIXTURE_LOG"], "a") as stream:
    stream.write(json.dumps({"argv": args, "passed": {n: os.environ.get(n) for n in names}}) + "\\n")
if args[:2] == ["system", "status"]:
    print("status " + os.environ.get("BOX_FIXTURE_STATUS", "running"))
elif args[:2] == ["image", "inspect"]:
    sys.exit(int(os.environ.get("BOX_FIXTURE_MISSING_IMAGE", "0")))
elif args[:1] == ["run"]:
    sys.exit(int(os.environ.get("BOX_FIXTURE_RUN_EXIT", "0")))
elif args[:1] != ["build"]:
    sys.exit("unexpected runtime operation: " + repr(args))
''')
        tool.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, env=None, cwd=None, entry=BOX, expected=0):
        result = subprocess.run([sys.executable, "-B", str(entry), *args],
                                env={**self.env, **(env or {})}, cwd=cwd or self.work,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.work), *args], env=self.env,
                       check=True, capture_output=True, text=True)

    def repository(self):
        self.git("init", "--quiet", "--template=")
        trailers = ("Co-Authored-By: Claude <noreply@anthropic.com>\n"
                    "Co-authored-by: Codex <codex@openai.com>\n"
                    "Co-authored-by: Com8 <322615700+Com8@users.noreply.github.com>")
        self.git("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit",
                 "--quiet", "--allow-empty", "-m", "Disposable box fixture", "-m", trailers)

    def test_plan_is_read_only_and_plain_workspace_is_exact(self):
        plan = json.loads(self.cli("plan", "echo", "literal $HOME; `id`", "two words").stdout)
        self.assertTrue(plan["read_only"])
        self.assertEqual(plan["command"], ["echo", "literal $HOME; `id`", "two words"])
        self.assertEqual(plan["mounts"][0], {"source": str(self.work), "destination": str(self.work),
                                            "read_only": False, "purpose": "workspace"})
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / ".local").exists())

    def test_repo_subdirectory_and_linked_worktree_mount_git_metadata(self):
        self.repository()
        nested = self.work / "nested"; nested.mkdir()
        plan = json.loads(self.cli("plan", cwd=nested).stdout)
        self.assertEqual(plan["mounts"][0]["source"], str(self.work))
        self.assertFalse(any(m["purpose"] == "shared-git" for m in plan["mounts"]))
        linked = self.root / "linked worktree"
        self.git("worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
        plan = json.loads(self.cli("plan", cwd=linked).stdout)
        mounted = {m["source"] for m in plan["mounts"]}
        self.assertIn(str(linked), mounted)
        self.assertIn(str(self.work / ".git"), mounted)
        self.assertNotIn(str(self.work), mounted)

    def test_stopped_runtime_and_missing_image_never_start_or_build(self):
        self.cli("echo", "test", env={"BOX_FIXTURE_STATUS": "stopped"}, expected=1)
        self.assertEqual([c["argv"] for c in self.calls()], [["system", "status"]])
        self.log.unlink()
        self.cli("echo", "test", env={"BOX_FIXTURE_MISSING_IMAGE": "1"}, expected=1)
        self.assertEqual([c["argv"][:2] for c in self.calls()], [["system", "status"], ["image", "inspect"]])
        self.assertFalse((self.home / ".local").exists())

    def test_doctor_is_read_only_and_missing_tool_fails_without_fallback(self):
        report = json.loads(self.cli("doctor").stdout)
        self.assertTrue(report["read_only"])
        self.assertTrue(report["image_present"])
        self.assertEqual([c["argv"][:2] for c in self.calls()], [["system", "status"], ["image", "inspect"]])
        self.log.unlink()
        self.cli("bash", env={"COM8_BOX_RUNTIME": str(self.bin / "absent")}, expected=1)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / ".local").exists())

    def test_run_arguments_private_home_and_oauth_precedence(self):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "fixture-token", "ANTHROPIC_API_KEY": "fixture-api-key",
               "OPENAI_API_KEY": "fixture-openai-key", "ANU_ACCOUNT": "fixture-account"}
        self.cli("run", "--", "claude", "--print", "literal $HOME; `id`", env=env)
        call = self.calls()[-1]; argv = call["argv"]
        self.assertEqual(argv[-4:], ["com8-agent", "claude", "--print", "literal $HOME; `id`"])
        self.assertIn("--rm", argv)
        self.assertIn("--interactive", argv)
        self.assertNotIn("--tty", argv)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", argv)
        self.assertNotIn("ANTHROPIC_API_KEY", argv)
        self.assertNotIn("fixture-token", " ".join(argv))
        self.assertNotIn("fixture-openai-key", " ".join(argv))
        self.assertEqual(call["passed"]["CLAUDE_CODE_OAUTH_TOKEN"], "fixture-token")
        home = self.home / ".local/share/com8/box/claude"
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.home / ".claude").exists())
        plan = self.cli("plan", env=env).stdout
        self.assertNotIn("fixture-token", plan)
        self.assertNotIn("fixture-openai-key", plan)

    def test_api_key_fallback_exit_code_and_no_git_identity_requirement(self):
        self.cli("npm", "test", env={"ANTHROPIC_API_KEY": "fixture-key", "BOX_FIXTURE_RUN_EXIT": "23"}, expected=23)
        call = self.calls()[-1]
        self.assertEqual(call["passed"]["ANTHROPIC_API_KEY"], "fixture-key")
        self.assertFalse(any(n.startswith("GIT_AUTHOR") for n in call["passed"]))

    def test_explicit_provider_homes_helpers_and_literal_paths(self):
        claude, codex, helpers, replies = [self.root / p for p in ["private claude", "private codex", "pane helpers", "reply data"]]
        for directory in [claude, codex, helpers, replies]:
            directory.mkdir(mode=0o750)
        (helpers / "pane").write_text("#!/bin/sh\nexit 0\n"); (helpers / "pane").chmod(0o755)
        env = {"COM8_BOX_CLAUDE_HOME": str(claude), "COM8_BOX_CODEX_HOME": str(codex),
               "COM8_BOX_PANE_BIN": str(helpers), "COM8_BOX_PANE_DIR": str(replies)}
        self.cli("bash", env=env)
        call = self.calls()[-1]
        self.assertIn(str(helpers) + ":/opt/com8/pane:ro", call["argv"])
        self.assertIn(str(replies) + ":" + str(replies), call["argv"])
        self.assertIn(str(claude) + ":/root/.claude", call["argv"])
        self.assertIn(str(codex) + ":/root/.codex", call["argv"])
        self.assertEqual(call["passed"]["ANU_PANE_DIR"], str(replies))
        self.assertEqual(claude.stat().st_mode & 0o777, 0o750, "explicit private home was changed")
        self.assertFalse((self.home / ".local").exists())

    def test_invalid_explicit_mounts_fail_before_backend_or_writes(self):
        self.cli("plan", env={"COM8_BOX_CLAUDE_HOME": str(self.root / "missing")}, expected=1)
        self.cli("bash", env={"COM8_BOX_PANE_BIN": str(self.bin)}, expected=1)
        bad = self.root / "colon:path"; bad.mkdir()
        self.cli("plan", env={"COM8_BOX_CLAUDE_HOME": str(bad)}, expected=1)
        self.assertEqual(self.calls(), [])

    def test_default_provider_home_symlink_is_not_modified(self):
        state = self.root / "state"; state.mkdir()
        target = self.root / "other home"; target.mkdir(mode=0o755)
        (state / "claude").symlink_to(target)
        self.cli("bash", env={"COM8_BOX_STATE": str(state)}, expected=1)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.calls(), [])

    def test_explicit_build_uses_installed_payload_without_repo_fallback(self):
        installed = self.root / "installed box"
        shutil.copytree(BOX.parent, installed)
        self.cli("build", entry=installed / "box.py")
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv, ["build", "--tag", "com8-agent", "--file", str(installed / "Containerfile"), str(installed)])
        self.assertFalse((self.home / ".local").exists())
        content = (installed / "Containerfile").read_text().lower()
        self.assertNotIn("jupyter", content)
        self.assertNotIn("marimo", content)

    def test_shell_module_load_does_not_call_backend_or_replace_private_launcher(self):
        function = ROOT / "profiles/runtime/fns/box"
        command = '. ' + shlex.quote(str(function)) + '; printf "%s" "$COM8_BOX_LAUNCHER"'
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command],
                                env={**self.env, "COM8_BOX_LAUNCHER": "/private/adapter"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/private/adapter")
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
