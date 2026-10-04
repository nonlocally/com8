#!/usr/bin/env python3
"""Validate an extracted artifact's installed profile with the real Ghostty parser.

Opens no windows and changes only a disposable home. Rendering/font availability
are not qualified. Missing Ghostty is unqualified, not a successful check.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("installed_gate", Path(__file__).with_name("qualify-installed.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--ghostty", default=shutil.which("ghostty") or "/Applications/Ghostty.app/Contents/MacOS/ghostty")
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    info = gate.artifact(runtime)
    if not Path(args.ghostty).is_file() or not os.access(args.ghostty, os.X_OK):
        print(json.dumps({"status": "unqualified", "reason": "Ghostty executable unavailable"}))
        return 2
    report = {"status": "fail", "source": info["source"], "version": info["version"],
              "rendering": "not tested", "font_availability": "not tested"}
    try:
        with tempfile.TemporaryDirectory(prefix="com8-ghostty-") as directory:
            home = Path(directory) / "home with spaces"
            home.mkdir(mode=0o700)
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(("COM8_", "GHOSTTY_")) or key in ("BASH_ENV", "ENV", "TMUX", "TMUX_PANE"):
                    env.pop(key)
            env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                       XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local/state"),
                       PYTHONDONTWRITEBYTECODE="1")

            def run(argv):
                return subprocess.run(argv, env=env, cwd=home, text=True, capture_output=True, timeout=30)

            install = run([sys.executable, str(runtime / "vendor/profiles/manage.py"),
                           "install", "--terminal", "--home", str(home)])
            gate.require(install.returncode == 0, "isolated profile installation failed")
            config = home / ".config/ghostty/config"
            valid = run([args.ghostty, "+validate-config", "--config-file=" + str(config)])
            gate.require(valid.returncode == 0 and not valid.stdout.strip() and not valid.stderr.strip(),
                         "Ghostty parser rejected the installed configuration")
            # show-config loads the isolated default path; unlike validate-config,
            # Ghostty 1.3.1 does not accept its --config-file flag here.
            shown = run([args.ghostty, "+show-config", "--changes-only=false"])
            expected = ("font-family = JetBrainsMono Nerd Font", "font-size = 13",
                        "background = #121212", "window-padding-x = 14")
            gate.require(shown.returncode == 0 and all(v in shown.stdout for v in expected),
                         "Ghostty did not load the installed profile from the isolated home")
            config.write_text(config.read_text() + "com8-deliberate-invalid-option = true\n")
            bad = run([args.ghostty, "+validate-config", "--config-file=" + str(config)])
            gate.require(bad.returncode != 0 and "com8-deliberate-invalid-option" in bad.stdout + bad.stderr,
                         "Ghostty negative control was not rejected")
            version = run([args.ghostty, "+version"])
            gate.artifact(runtime)
            report.update(status="pass", ghostty_version=version.stdout.splitlines()[0],
                          installed_include=True, expected_settings=True, negative_control=True,
                          artifact_unchanged=True, cleanup="temporary profile removed")
    except Exception as error:
        report["error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "unqualified", "error": str(error) if isinstance(error, RuntimeError) else type(error).__name__}))
        raise SystemExit(2)
