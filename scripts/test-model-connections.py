#!/usr/bin/env python3
"""Offline model connection boundaries; no real client, daemon, or service."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import model_connections as models
import com8


class Models(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.config = self.home / "models"
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name in (".codex", ".claude"):
            (self.home / name).mkdir(mode=0o700)
        self.key = self.home / "input-key"
        self.key.write_bytes(b"nlm_fixture-secret-never-in-argv\n")
        self.key.chmod(0o600)
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "COM8_MODEL_CONFIG": str(self.config), "COM8_PYTHON": sys.executable,
            "RECORD": str(self.home / "record.json"),
        }, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def args(self, **kwargs):
        return argparse.Namespace(name="openweb", model="glm", base_url="https://models.example/v1",
            anthropic_base_url="https://models.example", context_window=32768,
            allow_loopback_http=False, key_file=str(self.key), key_stdin=False, dry_run=False, **kwargs)

    def add(self):
        return models.add(self.args())

    def fake_client(self, name, code=0, edit=False):
        script = '''import json,os,pathlib,sys,tomllib
args=sys.argv[1:]
if args == ['--version']:
 print('codex-cli 0.156.1'); raise SystemExit(0)
if NAME == 'codex':
 profile=pathlib.Path(os.environ['CODEX_HOME'])/(args[args.index('--profile')+1]+'.config.toml')
 settings=tomllib.loads(profile.read_text())
else:
 profile=pathlib.Path(args[args.index('--settings')+1])
 settings=json.loads(profile.read_text())
result={'argv':args,'settings':settings,'mode':profile.stat().st_mode & 511,
 'token_present':os.environ.get('COM8_MODEL_API_KEY') == 'nlm_fixture-secret-never-in-argv',
 'old_auth_absent':all(k not in os.environ for k in ['OPENAI_API_KEY','OPENAI_BASE_URL','CLAUDE_CODE_OAUTH_TOKEN','ANU_ACCOUNT','ANU_PROVIDER','ANU_LAUNCH_NONCE','CODEX_THREAD_ID']),
 'plugins_present':(pathlib.Path(os.environ.get('CODEX_HOME',os.environ.get('CLAUDE_CONFIG_DIR')))/'plugin-sentinel').exists()}
pathlib.Path(os.environ['RECORD']).write_text(json.dumps(result))
if EDIT: profile.write_text('user-edit')
raise SystemExit(CODE)
'''.replace('NAME', repr(name)).replace('EDIT', repr(edit)).replace('CODE)', str(code) + ')')
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + script)
        path.chmod(0o755)

    def test_add_private_separate_records_and_list_redaction(self):
        previous = os.umask(0o022)
        try:
            result = self.add()
        finally:
            os.umask(previous)
        self.assertTrue(result['ok'])
        for p in [self.config, self.config / 'openweb']:
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700)
        for p in (self.config / 'openweb').iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertNotIn('nlm_fixture', (self.config / 'openweb/connection.json').read_text())
        self.assertNotIn('nlm_fixture', json.dumps(models.list_connections()))
        self.assertEqual(models.load('openweb')[1], 'nlm_fixture-secret-never-in-argv')

    def test_add_refuses_replacement_and_remove_is_owned(self):
        self.add()
        before = (self.config / 'openweb/credential').read_bytes()
        with self.assertRaises(models.ConnectionError): self.add()
        self.assertEqual((self.config / 'openweb/credential').read_bytes(), before)
        (self.config / 'openweb/unowned').write_text('keep')
        with self.assertRaises(models.ConnectionError): models.remove('openweb')
        (self.config / 'openweb/unowned').unlink()
        models.remove('openweb')
        self.assertTrue(self.key.exists())
        self.assertFalse((self.config / 'openweb').exists())

    def test_changed_secret_or_metadata_is_not_removed_or_used(self):
        self.add()
        (self.config / 'openweb/credential').write_text('changed')
        with self.assertRaises(models.ConnectionError): models.load('openweb')
        with self.assertRaises(models.ConnectionError): models.remove('openweb')
        self.assertEqual((self.config / 'openweb/credential').read_text(), 'changed')

    def test_key_permissions_symlink_and_oversize_refused(self):
        self.key.chmod(0o644)
        with self.assertRaises(models.ConnectionError): self.add()
        self.key.chmod(0o600)
        other = self.home / 'key-link'
        other.symlink_to(self.key)
        args = self.args(); args.key_file = str(other)
        with self.assertRaises(models.ConnectionError): models.add(args)
        self.key.write_bytes(b'x' * 8193)
        with self.assertRaises(models.ConnectionError): self.add()
        self.assertFalse(self.config.exists())

    def test_symlink_root_never_written_and_public_root_never_chmodded(self):
        destination = self.home / 'other'
        destination.mkdir()
        self.config.symlink_to(destination)
        with self.assertRaises(models.ConnectionError): self.add()
        self.assertEqual(list(destination.iterdir()), [])
        self.config.unlink(); self.config.mkdir(mode=0o755)
        with self.assertRaises(models.ConnectionError): self.add()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o755)

    def test_dry_run_does_not_read_key_or_create_config(self):
        args = self.args(); args.dry_run = True; args.key_file = '/missing/key'
        result = models.add(args)
        self.assertFalse(result['credential_read'])
        self.assertFalse(self.config.exists())

    def test_url_requires_https_no_embedded_secret_and_same_origin(self):
        for url in ['http://example.com/v1','https://u:password@example.com/v1','https://example.com/v1?token=secret','https://example.com/v1#secret']:
            args = self.args(); args.base_url = url
            with self.assertRaises(models.ConnectionError): models.add(args)
        args = self.args(); args.anthropic_base_url = 'https://another.example'
        with self.assertRaises(models.ConnectionError): models.add(args)
        self.assertEqual(models.endpoint('http://127.0.0.1:8888/v1', True), 'http://127.0.0.1:8888/v1')
        with self.assertRaises(models.ConnectionError): models.endpoint('http://localhost/v1', True)

    def test_stdin_and_unknown_argument_errors_are_redacted(self):
        command = [str(ROOT/'bin/com8'),'model','add','stdin','--base-url','https://models.example/v1','--model','glm','--key-stdin','--json']
        result = subprocess.run(command, input=b'nlm_stdin-secret', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b'nlm_stdin-secret', result.stdout + result.stderr)
        self.assertEqual(models.load('stdin')[1], 'nlm_stdin-secret')
        result = subprocess.run(command + ['--api-key','nlm_argument-secret'], input=b'', capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'nlm_argument-secret', result.stdout + result.stderr)
        result = subprocess.run([str(ROOT/'bin/com8'),'model','add','large','--base-url','https://models.example/v1','--model','glm','--key-stdin','--json'], input=b's'*8193, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.config/'large').exists())

    def test_codex_launch_preserves_plugin_and_config_and_arguments(self):
        self.add(); self.fake_client('codex')
        home = self.home/'.codex'
        (home/'config.toml').write_text('model = "existing"\n')
        (home/'auth.json').write_text('existing-auth')
        (home/'plugin-sentinel').write_text('keep')
        os.environ.update({k:'old-secret' for k in ['OPENAI_API_KEY','OPENAI_BASE_URL','CLAUDE_CODE_OAUTH_TOKEN','ANU_ACCOUNT','ANU_PROVIDER','ANU_LAUNCH_NONCE','CODEX_THREAD_ID']})
        message = 'literal $HOME `uname` "quote"\nnext line'
        self.assertEqual(models.run('openweb','codex',[message]), 0)
        result=json.loads((self.home/'record.json').read_text())
        self.assertEqual(result['argv'][-1], message)
        self.assertIn('--no-daemon',result['argv'])
        self.assertNotIn('nlm_fixture',json.dumps(result['argv']))
        self.assertTrue(result['token_present'] and result['old_auth_absent'] and result['plugins_present'])
        self.assertEqual(result['settings']['model'],'glm')
        self.assertEqual(result['settings']['model_providers']['com8_connection']['wire_api'],'responses')
        self.assertFalse(result['settings']['model_providers']['com8_connection']['requires_openai_auth'])
        self.assertEqual(result['settings']['model_context_window'],32768)
        self.assertEqual(result['settings']['web_search'],'disabled')
        self.assertFalse(result['settings']['shell_environment_policy']['ignore_default_excludes'])
        self.assertEqual(result['settings']['shell_environment_policy']['set']['COM8_MODEL_API_KEY'],'')
        self.assertNotIn('filters', result['settings']['shell_environment_policy'])
        self.assertNotIn('exclude', result['settings']['shell_environment_policy'])
        self.assertEqual(result['mode'],0o600)
        self.assertEqual((home/'config.toml').read_text(),'model = "existing"\n')
        self.assertEqual((home/'auth.json').read_text(),'existing-auth')
        self.assertEqual(list(home.glob('com8-connection-*.config.toml')),[])

    def test_claude_private_settings_pin_routes_and_preserve_plugin(self):
        self.add(); self.fake_client('claude')
        home=self.home/'.claude'
        (home/'settings.json').write_text('{"model":"existing","enabledPlugins":{"communicate":true}}')
        before=(home/'settings.json').read_bytes()
        (home/'plugin-sentinel').write_text('keep')
        self.assertEqual(models.run('openweb','claude',['hello'],accept_inbound=True),0)
        result=json.loads((self.home/'record.json').read_text())
        self.assertNotIn('nlm_fixture',json.dumps(result['argv']))
        self.assertTrue(result['plugins_present'])
        self.assertFalse(result['token_present'])
        self.assertEqual(result['settings']['env']['ANTHROPIC_BASE_URL'],'https://models.example')
        self.assertEqual(result['settings']['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'],'glm')
        self.assertEqual(result['settings']['env']['ANTHROPIC_API_KEY'],'')
        self.assertEqual(result['settings']['crossSessionInbound'],'accept')
        self.assertEqual((home/'settings.json').read_bytes(),before)
        self.assertEqual(list(home.glob('.com8-connection-*.json')),[])

    def test_claude_overrides_settings_routes_and_alternate_auth_sources(self):
        self.add(); self.fake_client('claude')
        conflicts = {'ANTHROPIC_CUSTOM_HEADERS': 'Authorization: Bearer unrelated-credential',
                     'ANTHROPIC_UNIX_SOCKET': str(self.home/'unrelated-provider.sock'),
                     'ANTHROPIC_SMALL_FAST_MODEL': 'unrelated-small-model',
                     'CLAUDE_CODE_USE_GATEWAY': '1',
                     'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR': '44',
                     'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR': '45',
                     'CLAUDE_CODE_OAUTH_TOKEN': 'unrelated-login',
                     'CCR_OAUTH_TOKEN_FILE': str(self.home/'unrelated-login.json')}
        settings = self.home/'.claude/settings.json'
        settings.write_text(json.dumps({'env': conflicts, 'enabledPlugins': {'communicate': True}}))
        before = settings.read_bytes()
        os.environ.update(conflicts)
        original = models.subprocess.Popen
        captured = {}
        def launch(*args, **kwargs):
            captured.update(kwargs['env'])
            return original(*args, **kwargs)
        with patch.object(models.subprocess, 'Popen', side_effect=launch):
            self.assertEqual(models.run('openweb', 'claude', ['hello']), 0)
        override = json.loads((self.home/'record.json').read_text())['settings']['env']
        for name in conflicts:
            expected = 'glm' if name == 'ANTHROPIC_SMALL_FAST_MODEL' else '0' if name == 'CLAUDE_CODE_USE_GATEWAY' else ''
            self.assertEqual(override[name], expected, name)
            if name != 'ANTHROPIC_SMALL_FAST_MODEL': self.assertNotIn(name, captured)
        self.assertEqual(settings.read_bytes(), before)

    def test_external_edit_retained_and_nonzero_client_exit_propagates(self):
        self.add(); self.fake_client('codex',code=7,edit=True)
        self.assertEqual(models.run('openweb','codex',[]),7)
        paths=list((self.home/'.codex').glob('com8-connection-*.config.toml'))
        self.assertEqual(len(paths),1)
        self.assertEqual(paths[0].read_text(),'user-edit')

    def test_missing_client_or_overrides_create_no_profiles(self):
        self.add()
        with patch.object(models.shutil,'which',return_value=None), self.assertRaises(models.ConnectionError):
            models.run('openweb','codex',[])
        for cli, args in [('codex',['-m','paid-model']),('codex',['--config=model_provider="openai"']),('claude',['--settings','override.json']),('claude',['--fallback-model=x'])]:
            with self.assertRaises(models.ConnectionError): models.run('openweb',cli,args)
        self.assertEqual(list((self.home/'.codex').glob('com8-connection-*')),[])

    def test_launch_uses_one_verified_endpoint_credential_pair(self):
        self.add(); self.fake_client('codex')
        first = models.load('openweb')
        rotated = (dict(first[0], base_url='https://rotated.example/v1'), 'rotated-key')
        # Simulate remove/re-add between reads: the new credential must never
        # be sent to the old endpoint, even though both records are valid.
        with patch.object(models, 'load', side_effect=[first, rotated]):
            self.assertEqual(models.run('openweb', 'codex', []), 0)
        result = json.loads((self.home/'record.json').read_text())
        self.assertEqual(result['settings']['model_providers']['com8_connection']['base_url'],
                         'https://models.example/v1')
        self.assertTrue(result['token_present'])

    def test_incompatible_client_modes_refused_before_launch(self):
        self.add()
        cases = [('claude', [flag]) for flag in
                 ['--bare', '--bg', '--background', '--cloud', '--environment=fixture',
                  '--teleport', '--remote-control', 'attach', 'respawn', 'ultrareview']]
        cases += [('codex', [command]) for command in
                  ['app', 'app-server', 'cloud', 'queue', 'remote-control', 'exec-server']]
        with patch.object(models.subprocess, 'Popen', side_effect=AssertionError('must not launch')):
            for cli, args in cases:
                with self.subTest(cli=cli, args=args), self.assertRaises(models.ConnectionError):
                    models.run('openweb', cli, args)
        self.assertEqual(list((self.home/'.codex').glob('com8-connection-*')), [])
        self.assertEqual(list((self.home/'.claude').glob('.com8-connection-*')), [])

    def test_failed_process_creation_removes_only_owned_launch_file(self):
        self.add(); self.fake_client('claude')
        os.environ['CLAUDE_CODE_SIMPLE'] = '1'
        with patch.object(models.subprocess, 'Popen', side_effect=OSError('fixture')) as launch:
            with self.assertRaises(OSError): models.run('openweb', 'claude', [])
        self.assertNotIn('CLAUDE_CODE_SIMPLE', launch.call_args.kwargs['env'])
        self.assertEqual(list((self.home/'.claude').glob('.com8-connection-*')), [])
        self.assertTrue((self.config/'openweb/credential').is_file())

    def test_spawn_validation_happens_before_daemon_or_claim(self):
        with patch.object(com8,'_call',side_effect=AssertionError('must not contact daemon')):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(com8.cli_call(['spawn','worker','--cli','codex','--model-connection','missing']),1)
                self.assertEqual(com8.cli_call(['spawn','worker','--cli','codex','--model-connection']),1)
                self.assertEqual(com8.cli_call(['spawn','worker','--cli','codex','--model-connection','--json']),1)
        self.add()
        captured=[]
        with patch.object(com8,'_call',side_effect=lambda request,**kw: captured.append(request) or {'ok':True,'name':'worker','seat':'%1'}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(com8.cli_call(['spawn','worker','--cli','claude','--model-connection','openweb']),0)
        self.assertEqual(len(captured),1)
        self.assertTrue(captured[0]['adopt'])
        self.assertIn('--accept-inbound',captured[0]['cmd'])
        self.assertNotIn('nlm_fixture',json.dumps(captured))
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN',captured[0]['cmd'])

    def test_codex_old_version_refused_before_profile_creation(self):
        self.add(); self.fake_client('codex')
        path=self.bin/'codex'
        path.write_text(path.read_text().replace('codex-cli 0.156.1','codex-cli 0.151.0'))
        with self.assertRaisesRegex(models.ConnectionError,'0.156.0'):
            models.run('openweb','codex',[])
        self.assertFalse((self.home/'record.json').exists())
        self.assertEqual(list((self.home/'.codex').glob('com8-connection-*')),[])

    def test_codex_resume_receives_selected_model_and_private_profile(self):
        self.add(); self.fake_client('codex')
        self.assertEqual(models.run('openweb','codex',['resume','fixture-thread']),0)
        result=json.loads((self.home/'record.json').read_text())
        self.assertEqual(result['argv'][-2:],['resume','fixture-thread'])
        self.assertEqual(result['argv'][result['argv'].index('--model')+1],'glm')
        self.assertEqual(result['settings']['model_provider'],'com8_connection')
        self.assertEqual(list((self.home/'.codex').glob('com8-connection-*')),[])

    def test_legacy_communicate_model_dispatch(self):
        self.add()
        result=subprocess.run([str(ROOT/'bin/communicate'),'com8','model','list','--json'],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['connections'][0]['model'],'glm')

    def test_subscription_pane_refused_without_mutation(self):
        self.add()
        os.environ['TMUX_PANE']='%51'
        with patch.object(models.shutil,'which',return_value='/fixture/tmux'), patch.object(models.subprocess,'run',return_value=argparse.Namespace(returncode=0,stdout=b'account-name')) as call:
            with self.assertRaisesRegex(models.ConnectionError,'fresh COM8 seat'): models.run('openweb','codex',[])
        self.assertEqual(call.call_args.args[0][1:3],['show-options','-pqv'])
        self.assertEqual(list((self.home/'.codex').glob('com8-connection-*')),[])

    def test_doctor_catalog_only_and_no_redirect_token_forwarding(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_GET(self):
                received.append((self.path,self.headers.get('Authorization')))
                self.send_response(200); self.end_headers()
                self.wfile.write(b'{"data":[{"id":"glm"}]}')
            def do_POST(self): raise AssertionError('inference must not run')
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            args=self.args(); args.base_url='http://127.0.0.1:%d/v1'%server.server_port
            args.anthropic_base_url=None; args.allow_loopback_http=True
            models.add(args)
            result=models.doctor('openweb')
            self.assertTrue(result['checks']['model_present'])
            self.assertFalse(result['inference_tested'])
            self.assertEqual(received,[('/v1/models','Bearer nlm_fixture-secret-never-in-argv')])
            def redirect(self):
                self.send_response(302); self.send_header('Location','http://127.0.0.1:%d/stolen'%server.server_port); self.end_headers()
            Handler.do_GET=redirect
            with self.assertRaisesRegex(models.ConnectionError,'HTTP 302'): models.doctor('openweb')
            self.assertEqual(len(received),1)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
