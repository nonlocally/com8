#!/usr/bin/env python3
"""Exercise the shipped dashboard assets with isolated browser API fixtures.

Requires Playwright Chromium and the repository's existing browser test scripts.
The application assets come only from the checksum-verified extracted artifact.
No provider, hosted bus, browser profile, or existing client state is used.
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
HERE = Path(__file__).resolve().parent
SUITES = ("flow", "spectral", "conductor", "chat", "self-service")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not importlib.util.find_spec("playwright"):
        parser.error("Playwright is unavailable; install the documented test dependency first")
    spec = importlib.util.spec_from_file_location("com8_artifact_check", HERE / "qualify-provider.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    runtime = args.runtime.resolve(strict=True)
    manifest = gate.artifact(runtime)
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = args.evidence.resolve()
    report = {"status": "pass", "source": manifest["source"], "version": manifest["version"],
              "runtime": str(runtime), "scope": "artifact assets; isolated API fixtures; headless Chromium",
              "real_agent_dispatch": False, "checks": []}
    with tempfile.TemporaryDirectory(prefix="com8-artifact-ui-") as directory:
        root = Path(directory)
        (root / "scripts").mkdir()
        (root / "lib").symlink_to(runtime / "vendor/lib", target_is_directory=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("COMM_CHAT_SCREENSHOT", None)
        env.pop("COMM_SELF_SERVICE_SCREENSHOT", None)
        for suite in SUITES:
            name = f"test-bus-{suite}-browser.py"
            shutil.copyfile(HERE / name, root / "scripts" / name)
            row = {"suite": suite, "status": "fail"}
            try:
                result = subprocess.run([sys.executable, str(root / "scripts" / name)],
                                        env=env, cwd=root, text=True, capture_output=True, timeout=120)
                with gate.private_file(evidence / (suite + ".log")) as out:
                    out.write(result.stdout + result.stderr)
                row.update(status="pass" if result.returncode == 0 else "fail", exit_code=result.returncode)
            except subprocess.TimeoutExpired:
                row["error"] = "browser fixture exceeded 120 seconds"
            report["checks"].append(row)
            if row["status"] != "pass":
                report["status"] = "fail"
    gate.artifact(runtime)
    with gate.private_file(evidence / "report.json") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
