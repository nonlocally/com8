#!/usr/bin/env python3
"""Exercise an actual historical package's installer and migration into COM8.

PREVIOUS must be an installed/extracted package with dependencies. Model client
CLIs are stateful fixtures; their real-host contracts are qualified separately.
Pass --provenance to distinguish original archives from reconstructed packages.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous", type=Path)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--provenance", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    previous, runtime = args.previous.resolve(), args.runtime.resolve()
    old_version = json.loads((previous / "package.json").read_text())["version"]
    node = shutil.which("node")
    if not node or not (previous / "src/cli.mjs").is_file():
        parser.error("Node and a historical Communicate package are required")
    home = Path(tempfile.mkdtemp(prefix="com8-legacy-"))
    data, state = home / "data", home / "state"
    env = {**os.environ, "HOME": str(home), "COMMUNICATE_DATA": str(data), "COMM_STATE": str(state),
           "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
           "COM8_SELF": "legacy-fixture", "COM8_SOCK_DIR": str(home / "sockets"),
           "COM8_SESSIONS_DIR": str(home / "sessions"), "COMM_BUS_PORT": "0",
           "PYTHONDONTWRITEBYTECODE": "1", "PATH": str(home / "bin") + os.pathsep + os.environ["PATH"]}
    for key in ("COM8_SOCK", "COM8_DAEMON_DIR", "CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "COMMUNICATE_HOME"):
        env.pop(key, None)
    (home / "bin").mkdir()
    fixture = r"""#!/usr/bin/env python3
import json,os,pathlib,sys
h=pathlib.Path(os.environ['HOME']); a=sys.argv[1:]; kind=pathlib.Path(sys.argv[0]).name; ident='communicate@communicate'
with (h/'client-calls').open('a') as f:f.write(json.dumps([kind,*a])+'\n')
file=h/(kind+'-client.json'); settings_file=h/'.claude/settings.json'
s=json.loads(file.read_text()) if file.exists() else {'market':None,'installed':False,'version':None,'settings':None,'revision':0}
def save():file.write_text(json.dumps(s))
def settings():return json.loads(settings_file.read_text()) if settings_file.exists() else {}
def write_settings(value):settings_file.parent.mkdir(exist_ok=True);settings_file.write_text(json.dumps(value))
def output(value):print(json.dumps(value),flush=True)
def refresh():
 if not s['market']:raise RuntimeError('marketplace missing')
 root=pathlib.Path(s['market']);manifest=root/('communicate/.claude-plugin/plugin.json' if kind=='claude' else 'plugins/communicate/.codex-plugin/plugin.json')
 s['version']=json.loads(manifest.read_text())['version'];s['installed']=True
 if kind=='claude':
  value=settings();value.setdefault('enabledPlugins',{})[ident]=True;write_settings(value)
 else:s['settings']={**(s['settings'] or {}),'enabled':True};s['revision']+=1
if a==['--version']:print(kind+' 0.156.1-contract-fixture');sys.exit(0)
if a==['app-server'] and kind=='codex':
 for line in sys.stdin:
  q=json.loads(line);s=json.loads(file.read_text()) if file.exists() else s;method=q['method'];value={}
  if method=='initialize':pass
  elif method=='config/read':value={'layers':[{'name':{'type':'user','file':str(pathlib.Path(os.environ['CODEX_HOME'])/'config.toml')},'version':str(s['revision']),'config':{'plugins':{ident:s['settings']} if s['settings'] is not None else {}}}]}
  elif method=='config/value/write':
   p=q['params']
   if p['expectedVersion']!=str(s['revision']) or p['keyPath']!='plugins."'+ident+'"':
    output({'id':q['id'],'error':{'code':-32602,'message':'fixture version/key conflict'}});continue
   s['settings']=p['value'];s['revision']+=1;save();value={'status':'ok','version':str(s['revision'])}
  else:raise RuntimeError('unsupported config method')
  output({'id':q['id'],'result':value})
 sys.exit(0)
if a[:3]==['plugin','marketplace','add']:
 root=str(pathlib.Path(a[3]).resolve()) if kind=='codex' else a[3]
 if kind=='codex' and s['market'] and s['market']!=root:sys.exit(3)
 s['market']=root
 if kind=='claude':
  value=settings();value.setdefault('extraKnownMarketplaces',{})['communicate']={'source':{'source':'directory','path':root}};write_settings(value)
elif a[:3]==['plugin','marketplace','remove']:
 s['market']=None
 if kind=='claude':
  value=settings();value.get('extraKnownMarketplaces',{}).pop('communicate',None);write_settings(value)
elif a[:3]==['plugin','marketplace','list']:
 rows=[{'name':'communicate','root':s['market'],'marketplaceSource':{'sourceType':'local','source':s['market']}}] if s['market'] else []
 output({'marketplaces':rows} if kind=='codex' else rows)
elif a[:2] in (['plugin','update'],['plugin','install'],['plugin','add']):refresh()
elif a[:2] in (['plugin','uninstall'],['plugin','remove']):
 s['installed']=False;s['version']=None
 if kind=='claude':
  value=settings();value.get('enabledPlugins',{}).pop(ident,None);write_settings(value)
 else:s['settings']=None;s['revision']+=1
elif a[:2]==['plugin','disable'] and kind=='claude':
 value=settings();value.setdefault('enabledPlugins',{})[ident]=False;write_settings(value)
elif a[:2]==['plugin','list']:
 if kind=='claude':output([{'id':ident,'scope':'user','version':s['version'],'enabled':settings().get('enabledPlugins',{}).get(ident,False)}] if s['installed'] else [])
 else:output({'installed':[{'pluginId':ident,'installed':True,'version':s['version'],'enabled':s['settings'].get('enabled',True) if s['settings'] else True}] if s['installed'] else [],'available':[]})
else:raise RuntimeError('unexpected fixture command: '+repr(a))
save()
"""
    for name in ("claude", "codex"):
        tool = home / "bin" / name; tool.write_text(fixture); tool.chmod(0o755)
    for name in ("launchctl", "systemctl", "ssh", "gh", "tailscale"):
        tool = home / "bin" / name; tool.write_text("#!/bin/sh\necho 'external tool forbidden in legacy fixture' >&2\nexit 93\n"); tool.chmod(0o755)
    settings = home / ".claude/settings.json"; settings.parent.mkdir()
    settings.write_text(json.dumps({"unrelated": "preserved", "enabledPlugins": {"other@other": True}}))
    report = {"previous_version": old_version, "previous_provenance": args.provenance,
              "runtime_source": json.loads((runtime / "release.json").read_text())["source"],
              "client_scope": "stateful CLI/config-API contract fixtures; no model or real client discovery",
              "checks": [], "ok": False}
    old_cli, new_cli = previous / "src/cli.mjs", runtime / "src/com8.mjs"
    def run(entry, *arguments, success=True):
        result = subprocess.run([node, str(entry), *arguments], env=env, text=True, capture_output=True, timeout=60)
        if (result.returncode == 0) != success:
            raise RuntimeError(f"{arguments}: {result.stdout}\n{result.stderr}")
        return result.stdout + result.stderr
    def client(kind, *arguments):
        result = subprocess.run([str(home / "bin" / kind), *arguments], env=env,
                                text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"client fixture {kind} {arguments}: {result.stderr}")
        return result.stdout
    def snapshot():
        value = json.loads(settings.read_text())
        source = value.get("extraKnownMarketplaces", {}).get("communicate", {}).get("source", {})
        if source.get("path"):
            source["path"] = str(Path(source["path"]).resolve())
        result = {"claude_settings": {key: val for key, val in value.items()
                  if not (key in {"extraKnownMarketplaces", "enabledPlugins"} and val == {})}}
        for kind in ("claude", "codex"):
            registered = json.loads((home / (kind + "-client.json")).read_text())
            result[kind] = {key: registered[key] for key in ("installed", "version", "settings")}
            result[kind]["market"] = str(Path(registered["market"]).resolve()) if registered["market"] else None
            result[kind]["visible"] = json.loads(client(kind, "plugin", "list", "--json"))
        return result
    def hashes(root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
    try:
        run(old_cli, "setup", "--claude", "--codex")
        old_target = (data / "current").resolve()
        assert old_target.name == old_version and not (old_target / "vendor/release.json").exists()
        # 0.2.0 enabled Claude through settings; represent an existing working
        # installation by completing its documented client activation step.
        client("claude", "plugin", "marketplace", "add", str(data / "current/vendor/plugins"))
        client("claude", "plugin", "install", "communicate@communicate", "--scope", "user")
        original_clients = snapshot()
        assert all(original_clients[kind]["installed"] for kind in ("claude", "codex"))
        run(old_cli, "bus", "create", "migration-fixture")
        assert "migration-fixture" in run(old_cli, "bus", "list", "--json")
        # This is real broker state produced by the historical code. Setup must
        # preserve its credential/database bytes while the broker is stopped.
        run(old_cli, "bus", "stop")
        state_before, old_payload = hashes(state), hashes(old_target)
        report["checks"].append("historical installer plus real local broker persistence")
        run(new_cli, "setup", "--claude", "--codex")
        new_target = (data / "current").resolve()
        assert new_target != old_target
        ledger_path = data / "install.json"
        first_clients = json.loads(ledger_path.read_text())["clients"]
        run(new_cli, "setup", "--claude", "--codex")
        assert json.loads(ledger_path.read_text())["clients"] == first_clients, "repeat setup changed original ownership"
        for name, digest in state_before.items():
            assert hashlib.sha256((state / name).read_bytes()).hexdigest() == digest, name
        assert "migration-fixture" in run(runtime / "src/cli.mjs", "bus", "list", "--json")
        run(runtime / "src/cli.mjs", "bus", "stop")
        assert json.loads(settings.read_text())["unrelated"] == "preserved"
        report["checks"].append("migration preserves historical broker data, credentials and unrelated client settings")
        ledger_bytes = ledger_path.read_text()
        guarded = json.loads(ledger_bytes); guarded["service"] = {"fixture": "do not invoke"}
        ledger_path.write_text(json.dumps(guarded))
        before_calls = (home / "client-calls").read_text()
        refused = run(new_cli, "rollback", success=False)
        assert "No pointer or client changes" in refused
        assert (data / "current").resolve() == new_target and (home / "client-calls").read_text() == before_calls
        ledger_path.write_text(ledger_bytes)
        run(new_cli, "rollback")
        assert (data / "current").resolve() == old_target
        assert old_version in run(data / "current/src/cli.mjs", "version")
        for kind in ("claude", "codex"):
            assert json.loads((home / (kind + '-client.json')).read_text())["version"].split('+')[0] == old_version
        run(new_cli, "doctor")
        for name, digest in old_payload.items():
            assert hashlib.sha256((old_target / name).read_bytes()).hexdigest() == digest, name
        report["checks"].append("legacy rollback refreshes both clients without changing old payload; unsupported service rollback refuses before mutation")
        run(new_cli, "rollback")
        assert (data / "current").resolve() == new_target
        assert json.loads(ledger_path.read_text())["clients"]["codex"]["original"] == first_clients["codex"]["original"]
        for key in ("previousMarket", "previousInstalled", "previousPluginEnabled", "previousEnabled"):
            assert json.loads(ledger_path.read_text())["clients"]["claude"].get(key) == first_clients["claude"].get(key)
        state_before_uninstall = hashes(state)
        run(new_cli, "uninstall")
        assert snapshot() == original_clients, "uninstall did not restore the working original client versions/settings"
        assert hashes(state) == state_before_uninstall, "uninstall changed broker state"
        assert hashes(old_target) == old_payload, "historical payload changed"
        report["checks"].append("repeat setup and both rollbacks preserve first originals; uninstall restores working historical clients and exact retained state")
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        subprocess.run([node, str(old_cli), "bus", "stop"], env=env, capture_output=True, timeout=20)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        args.report.chmod(0o600)
        shutil.rmtree(home)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
