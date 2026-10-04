#!/usr/bin/env python3
"""Installed optional accounts: isolated HOME, fake providers/store/network only."""
import importlib.util
import json
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
spec = importlib.util.spec_from_file_location("profiles", ROOT / "profiles/manage.py")
profiles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profiles)
SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class Accounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="com8-account-boundary-")
        self.home = Path(self.tmp.name) / "home with spaces"
        self.home.mkdir()
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(
            ("ANU_", "COM8_", "CODEX_", "CLAUDE_", "ANTHROPIC_", "OPENAI_", "XDG_"))
            and k not in {"TMUX", "TMUX_PANE", "BASH_ENV", "ENV"}}
        self.env.update(HOME=str(self.home), PATH=str(self.bin) + ":" + SAFE_PATH)
        for name in ["claude", "codex", "curl", "ssh", "tmux", "infisical", "security", "op", "secret-tool"]:
            self.stub(name, 'raise SystemExit("UNQUALIFIED: unexpected integration")')
        self.profile = profiles.Profile(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\nimport json,os,sys\nfrom pathlib import Path\n" + body + "\n")
        path.chmod(0o755)

    def run_tool(self, name, *args, check=True, input=None):
        result = subprocess.run([str(self.bin / name), *args], env=self.env, cwd=self.home,
                                text=True, capture_output=True, input=input, timeout=20)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def install(self, *modules):
        self.profile.install(list(modules))

    def test_module_selection_is_explicit_and_starts_nothing(self):
        self.install("terminal")
        self.assertFalse((self.bin / "com8-account").exists())
        self.assertFalse((self.bin / "com8-box").exists())
        self.install("terminal", "accounts", "box")
        for name in ["com8-account", "com8-account-pane", "com8-account-secrets"]:
            self.assertIn("account", self.run_tool(name, "help").stdout)
        self.run_tool("com8-box", "--help")
        self.assertFalse((self.home / ".local/state/com8/accounts").exists())
        # Watcher invocation stays a no-op until explicitly enabled in local.sh.
        self.run_tool("com8-workstation", "watch")

    def test_file_reply_preserves_correlation_and_literal_bytes_without_tmux(self):
        self.install("accounts")
        correlation = "task-78c2.reply"
        message = 'quoted " $literal `not-a-command`\nUnicode λ\n\n'
        self.run_tool("com8-account-pane", "reply", correlation, "-", input=message)
        replies = self.home / ".local/state/com8/pane/replies"
        self.assertEqual((replies / correlation).read_bytes(), message.encode())
        self.assertEqual((replies / correlation).stat().st_mode & 0o077, 0)
        self.assertEqual(list(replies.iterdir()), [replies / correlation])
        for bad in ["../escape", ".hidden", "nested/id"]:
            self.assertNotEqual(self.run_tool("com8-account-pane", "reply", bad, "bad", check=False).returncode, 0)
        self.assertNotEqual(self.run_tool("com8-account-pane", "reply", correlation,
                                          "-f", "missing input", check=False).returncode, 0)
        self.assertEqual((replies / correlation).read_bytes(), message.encode())
        self.assertEqual(list(replies.iterdir()), [replies / correlation])
        old = replies / "old"
        old.write_text("old fixture")
        os.utime(old, (1, 1))
        self.run_tool("com8-account-pane", "reply", "gc", "--older", "1")
        self.assertFalse(old.exists())
        self.assertTrue((replies / correlation).exists())

    def test_claude_launch_merges_settings_and_hooks_from_installed_path(self):
        self.install("accounts")
        cache = self.home / "cache.json"
        cache.write_text('{"FIXTURE":"fixture-launch-token"}')
        self.env["ANU_ACCOUNT_CACHE"] = str(cache)
        self.stub("claude", "print(json.dumps(sys.argv[1:]))")
        supplied = self.home / "my settings.json"
        original = {"model": "fixture-model", "hooks": {"SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command", "command": "true"}]}]}}
        supplied.write_text(json.dumps(original))
        result = self.run_tool("com8-account", "launch", "--as", "fixture", "--",
                               "--resume", "11111111-1111-4111-8111-111111111111",
                               "--settings", str(supplied), "--", "literal $text")
        args = json.loads(result.stdout)
        merged = json.loads(args[args.index("--settings") + 1])
        self.assertEqual(merged["model"], "fixture-model")
        self.assertEqual(merged["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
        hook = merged["hooks"]["SessionStart"][1]["hooks"][0]["command"]
        self.assertEqual(shlex.split(hook), [str(self.bin / "com8-account"), "hook", "session-start"])
        self.assertEqual(args[-2:], ["--", "literal $text"])
        self.assertEqual(json.loads(supplied.read_text()), original)
        self.assertNotIn(".local/share/anu", hook)
        # Hook can be invoked without the checkout or any live tmux state.
        self.run_tool("com8-account", "hook", "session-start", input="{}")

    def test_contained_launch_never_uses_ambient_box_or_host_fallback(self):
        self.install("accounts")
        result = self.run_tool("com8-account", "launch", "--box", "--", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("executable ANU_ACCOUNT_BOX_BIN", result.stderr)

    def test_unconfigured_sync_host_keeps_sync_and_unregistered_add_local(self):
        self.install("accounts")
        self.stub("fixture-secrets", """
if sys.argv[1] == 'env': print('{"FIXTURE":"fixture-cached-token"}')
elif sys.argv[1] == 'set': assert sys.stdin.read().strip() == 'fixture-new-token'
elif sys.argv[1] != 'mkdir': raise SystemExit('unexpected credential operation')
""")
        self.env["COM8_SECRETS_BIN"] = str(self.bin / "fixture-secrets")
        self.run_tool("com8-account", "sync")
        cache = self.home / ".local/state/com8/accounts/tokens.json"
        self.assertEqual(json.loads(cache.read_text())["FIXTURE"], "fixture-cached-token")
        self.run_tool("com8-account", "add", "--no-register", "fixture", input="fixture-new-token\n")
        self.assertEqual(json.loads(cache.read_text())["FIXTURE"], "fixture-new-token")
        self.assertEqual(cache.stat().st_mode & 0o077, 0)

    def test_codex_hooks_are_runnable_with_spaces_in_installed_home(self):
        self.install("accounts")
        self.stub("codex", "Path(os.environ['CODEX_HOME'], 'auth.json').write_text('{}')")
        self.run_tool("com8-account", "add", "--provider", "codex", "fixture")
        import tomllib
        cfg = self.home / ".local/state/com8/accounts/codex/fixture/config.toml"
        data = tomllib.loads(cfg.read_text())
        for event in ["SessionStart", "UserPromptSubmit"]:
            hook = data["hooks"][event][0]["hooks"][0]["command"]
            self.assertEqual(Path(shlex.split(hook)[0]), (self.bin / "com8-account").resolve())
        self.run_tool("com8-account", "doctor", "--provider", "codex")

    def test_existing_secret_store_cache_and_private_value_transport(self):
        self.install("accounts")
        config = self.home / "secrets.toml"
        state = self.home / "secrets state"
        (state / "root").mkdir(parents=True)
        (state / "root/ua-client-secret:fixture").write_text("fixture-root-credential")
        config.write_text('domain = "https://secrets.example.invalid/api"\nproject_id = "project"\n'
                          'device_id = "fixture"\nua_client_id = "client"\norg_slug = "org"\n'
                          'default_env = "dev"\ncredential_store = "file"\n')
        self.env.update(COM8_SECRETS_CONFIG=str(config), COM8_SECRETS_STATE=str(state), ANU_NOW="1000")
        self.stub("curl", """
args = sys.argv[1:]
assert 'fixture-root-credential' not in ' '.join(args)
log = Path.home() / 'curl.jsonl'
with log.open('a') as f: f.write(json.dumps(args) + '\\n')
if args[-1].endswith('/universal-auth/login'):
    assert json.load(sys.stdin)['clientSecret'] == 'fixture-root-credential'
    print('{"accessToken":"fixture-scoped-token","expiresIn":3600}')
else:
    if '-K' in args: assert Path(args[args.index('-K')+1]).stat().st_mode & 0o077 == 0
    print('{"folders":[]}')
""")
        self.stub("infisical", """
args = sys.argv[1:]
assert 'fixture-root-credential' not in ' '.join(args)
assert 'fixture-value $literal' not in ' '.join(args)
if args[:2] == ['secrets', 'set']:
    p = Path(args[args.index('--file')+1])
    assert p.stat().st_mode & 0o077 == 0
    assert p.read_text() == 'FIXTURE=fixture-value $literal\\n'
elif args[0] == 'export': print('{"FIXTURE":"fixture-value"}')
elif '--plain' in args: print('fixture-value')
""")
        hidden = self.run_tool("com8-account-secrets", "get", "/fixture", "FIXTURE")
        self.assertNotIn("fixture-value", hidden.stdout)
        self.assertEqual(self.run_tool("com8-account-secrets", "get", "/fixture", "FIXTURE", "--show").stdout.strip(), "fixture-value")
        self.run_tool("com8-account-secrets", "set", "/fixture", "FIXTURE", input="fixture-value $literal\n")
        self.assertEqual(json.loads(self.run_tool("com8-account-secrets", "env", "--scope", "/fixture", "--format", "json").stdout), {"FIXTURE": "fixture-value"})
        self.run_tool("com8-account-secrets", "mkdir", "/fixture/sub")
        calls = [json.loads(line) for line in (self.home / "curl.jsonl").read_text().splitlines()]
        self.assertEqual(sum(args[-1].endswith('/universal-auth/login') for args in calls), 1)
        cache = state / "cache/infisical-token.json"
        self.assertEqual(cache.stat().st_mode & 0o077, 0)
        self.assertNotIn("fixture-root-credential", cache.read_text())
        self.assertEqual(list((state / "tmp").iterdir()), [])
        self.env["ANU_NOW"] = "5000"
        self.run_tool("com8-account-secrets", "get", "/fixture", "FIXTURE")
        calls = [json.loads(line) for line in (self.home / "curl.jsonl").read_text().splitlines()]
        self.assertEqual(sum(args[-1].endswith('/universal-auth/login') for args in calls), 2)

    def test_missing_store_config_and_admin_verbs_fail_without_external_calls(self):
        self.install("accounts")
        for args in [("get", "/fixture", "NAME"), ("init",), ("adopt",), ("exec", "true")]:
            result = self.run_tool("com8-account-secrets", *args, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("UNQUALIFIED", result.stderr)


if __name__ == "__main__":
    if not shutil.which("jq"):
        sys.exit("UNQUALIFIED: jq is required")
    unittest.main()
