#!/usr/bin/env python3
"""Install an authenticated bus origin as this user's macOS LaunchAgent.

Run on the hub host after staging a tested release. Settings are a private JSON
file containing BUS_GATEWAY_SHARED_SECRET and BUS_ADMIN_READERS; secret values
never enter process arguments or the plist. Networking is configured separately.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True, help="tested release containing lib/bus_broker.py")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--settings", type=Path, required=True)
    p.add_argument("--port", type=int, default=7433)
    args = p.parse_args()
    if sys.platform != "darwin":
        p.error("this installer is for macOS; use your service manager with bus serve on other systems")
    source, state, settings = (path.expanduser().absolute() for path in (args.source, args.state, args.settings))
    if not (source / "lib/bus_broker.py").is_file():
        p.error("source is missing the tested broker")
    if settings.is_symlink() or settings.stat().st_uid != os.getuid() or settings.stat().st_mode & 0o077:
        p.error("settings must be a private file owned by this user (mode 0600)")
    values = json.loads(settings.read_text())
    if not isinstance(values.get("BUS_GATEWAY_SHARED_SECRET"), str) or len(values["BUS_GATEWAY_SHARED_SECRET"]) < 32:
        p.error("settings must configure an origin authentication secret")
    if values.get("BUS_OPENWEBUI_READERS") not in (None, "0", "1"):
        p.error("BUS_OPENWEBUI_READERS must be the string 0 or 1")
    if values.get("BUS_DEVICE_INVITES") not in (None, "own", "off"):
        p.error("BUS_DEVICE_INVITES must be the string own or off")
    if not 1 <= args.port <= 65535:
        p.error("invalid port")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    runner = state / "run-hub.py"
    runner.write_text("""import json, os, sys
from pathlib import Path
config = json.loads(Path(%r).read_text())
for name in ('BUS_GATEWAY_SHARED_SECRET', 'BUS_ADMIN_READERS', 'BUS_OPENWEBUI_READERS', 'BUS_DEVICE_INVITES'):
    if name in config:
        os.environ[name] = config[name]
for name in ('BUS_READER_USERS', 'BUS_ACCOUNT_LABELS', 'BUS_CHAT_READERS', 'BUS_CHAT_OPENWEBUI_TARGETS'):
    if name in config:
        value = config[name]
        os.environ[name] = json.dumps(value) if isinstance(value, (dict, list)) else value
sys.path.insert(0, %r)
from bus_broker import Broker, serve
serve(Broker(%r), '127.0.0.1', %r)
""" % (str(settings), str(source / "lib"), str(state / "broker"), args.port))
    runner.chmod(0o600)
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    label = "com.communicate.bus-hub"
    plist = agents / (label + ".plist")
    data = {"Label": label, "ProgramArguments": [sys.executable, "-u", str(runner)],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5,
            "WorkingDirectory": str(state), "Umask": 0o077,
            "StandardOutPath": str(state / "hub.log"), "StandardErrorPath": str(state / "hub.err")}
    temp = plist.with_suffix(".plist.new")
    with temp.open("wb") as stream:
        plistlib.dump(data, stream)
    temp.chmod(0o600)
    subprocess.run(["plutil", "-lint", str(temp)], check=True, capture_output=True)
    domain = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
    os.replace(temp, plist)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True)
    print(json.dumps({"service": label, "state": str(state), "port": args.port,
                      "status": "installed; verify authenticated health before publishing"}))


if __name__ == "__main__":
    main()
