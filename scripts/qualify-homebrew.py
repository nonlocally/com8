#!/usr/bin/env python3
"""Qualify COM8's formula in a temporary keg without replacing installed commands.

Run on a qualification host, passing an extracted artifact's archive and the
rendered canonical formula. Only the candidate formula name, local archive URL,
and keg-only status differ. Uses existing dependencies; never upgrades them.
--disposable-ci instead requires a GitHub-hosted runner and absent COM8 commands,
then tests the canonical linked formula with ordinary dependency resolution,
brew test and brew reinstall. Never use that mode on a shared workstation.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def run(argv, *, env=None, timeout=180, quiet=False):
    print("+ " + " ".join(map(str, argv)), flush=True)
    result = subprocess.run(list(map(str, argv)), env=env, text=True, capture_output=True, timeout=timeout)
    if result.stdout and not quiet:
        print(result.stdout, end="", flush=True)
    if result.returncode:
        if result.stderr:
            print(result.stderr, end="", flush=True)
        raise RuntimeError(f"command failed ({result.returncode}): {argv[0]}")
    return result.stdout


def fingerprint(path):
    if path.is_symlink():
        return {"kind": "link", "target": os.readlink(path)}
    if path.is_file():
        return {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": path.stat().st_mode}
    return {"kind": "absent"}


def verify_runtime(runtime):
    manifest = json.loads((runtime / "release.json").read_text())
    for name, digest in manifest["files"].items():
        file = runtime / name
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Homebrew changed or removed an immutable artifact file: " + name)
    return {"source": manifest["source"], "version": manifest["version"], "files": len(manifest["files"])}


def process_state(pid):
    """A PID is gone only when the kernel says ESRCH; never signal it."""
    try:
        os.kill(pid, 0)
    except OSError as error:
        return "gone" if error.errno == errno.ESRCH else "unknown"
    return "live"


def bounded_output(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[:4096]


class OwnedDaemon:
    """Track only this fixture's daemon, including its asynchronous shutdown."""

    def __init__(self, root, entry, env, *, command=subprocess.run, probe=process_state,
                 clock=time.monotonic, sleep=time.sleep, timeout=10):
        self.root, self.entry, self.env = root, entry, env
        self.socket = root / "state/com8/com8.sock"
        self.pidfile = root / "state/com8/daemon.pid"
        self.command, self.probe = command, probe
        self.clock, self.sleep, self.timeout = clock, sleep, timeout
        self.records = []
        self.current = None

    def begin_start(self):
        if self.current and not self.current.get("confirmed"):
            raise RuntimeError("previous isolated daemon shutdown was not confirmed")
        self.current = {"start_attempted": True, "pid": None, "shutdown_checks": []}
        self.records.append(self.current)

    def observe(self):
        record = self.current
        state = {"socket_exists": self.socket.exists(), "pidfile_exists": self.pidfile.exists()}
        if state["pidfile_exists"]:
            try:
                with self.pidfile.open() as handle:
                    pid = int(handle.read(64).strip())
                if not 1 < pid <= 2**31 - 1:
                    raise ValueError("invalid PID")
                if record["pid"] is None:
                    record["pid"] = pid
                elif record["pid"] != pid:
                    record["ownership_error"] = "owned pidfile changed"
            except (OSError, ValueError):
                state["pid_error"] = "owned pidfile unreadable or invalid"
        if state["socket_exists"] or state["pidfile_exists"]:
            record["observed_endpoint"] = True
        if record.get("ownership_error"):
            state["pid_error"] = record["ownership_error"]
        state["process"] = self.probe(record["pid"]) if record["pid"] is not None else "unrecorded"
        return state

    def capture_pid(self):
        state = self.observe()
        self.current["startup_state"] = state
        if self.current["pid"] is None or state.get("pid_error"):
            raise RuntimeError("could not record isolated daemon PID after startup")

    def stop(self, *, required=True):
        if self.current and self.current.get("confirmed"):
            return True  # Never issue a second stop after the acknowledged exit.
        if self.current is None:
            self.current = {"start_attempted": False, "pid": None, "shutdown_checks": []}
            self.records.append(self.current)
        record = self.current
        started = self.clock()
        deadline = started + self.timeout
        state = self.observe()
        # Capture the PID before asking the exact owned endpoint to stop. An
        # unsuccessful command is not itself proof that the daemon survived.
        if (record["pid"] is not None and not state.get("pid_error") and
                state["process"] != "gone" and "stop_command" not in record):
            command = record["stop_command"] = {"returncode": None, "stdout": "", "stderr": ""}
            try:
                result = self.command([str(self.entry), "stop"], env=self.env, text=True,
                                      capture_output=True, timeout=max(0.001, deadline - self.clock()))
                command.update(returncode=result.returncode, stdout=bounded_output(result.stdout),
                               stderr=bounded_output(result.stderr))
            except subprocess.TimeoutExpired as error:
                command.update(error="timeout", stdout=bounded_output(error.stdout), stderr=bounded_output(error.stderr))
            except OSError as error:
                command.update(error=type(error).__name__, errno=error.errno)
        while True:
            state = self.observe()
            absent = not state["socket_exists"] and not state["pidfile_exists"]
            inert = record["pid"] is None and not record["start_attempted"] and not record.get("observed_endpoint")
            confirmed = absent and not state.get("pid_error") and (state["process"] == "gone" or inert)
            if confirmed or self.clock() >= deadline:
                break
            self.sleep(min(0.05, max(0, deadline - self.clock())))
        record["confirmed"] = confirmed
        record["shutdown_checks"].append({"confirmed": confirmed, "final_state": state,
                                           "elapsed_seconds": round(self.clock() - started, 3)})
        if not confirmed and required:
            raise RuntimeError(f"isolated daemon shutdown unconfirmed; retaining {self.root}")
        return confirmed


def finish_fixture_cleanup(report, root, cleanup_errors):
    """Keep the fixture and original error whenever cleanup is unconfirmed."""
    if not cleanup_errors:
        try:
            shutil.rmtree(root)
        except OSError as error:
            cleanup_errors.append("fixture removal: " + type(error).__name__)
    report["temporary_root"] = str(root)
    report["fixture_retained"] = root.exists()
    report["cleanup_errors"] = cleanup_errors
    if cleanup_errors:
        report["ok"] = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("formula", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--disposable-ci", action="store_true",
                        help="Allow dependency changes on a disposable GitHub-hosted runner only")
    args = parser.parse_args()
    if args.disposable_ci and not (os.environ.get("GITHUB_ACTIONS") == "true" and
                                  os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted" and
                                  Path(os.environ.get("RUNNER_TEMP", "/nonexistent")).is_dir()):
        parser.error("--disposable-ci requires a disposable GitHub-hosted runner; shared hosts are refused")
    archive = args.archive.resolve(strict=True)
    args.work.mkdir(parents=True, exist_ok=True)
    report = {"archive": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "mode": ("canonical linked formula on disposable GitHub-hosted runner" if args.disposable_ci
                       else "renamed keg-only candidate; canonical command names inside keg"), "checks": [], "ok": False}
    brew = shutil.which("brew")
    if not brew:
        parser.error("Homebrew is required on the qualification host")
    brew_env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ANALYTICS": "1",
                "HOMEBREW_NO_INSTALL_CLEANUP": "1", "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
                "HOMEBREW_NO_INSTALL_UPGRADE": "1", "HOMEBREW_NO_AUTOREMOVE": "1",
                "HOMEBREW_NO_ENV_HINTS": "1", "HOMEBREW_NO_ASK": "1"}
    prefix = Path(run([brew, "--prefix"], env=brew_env, quiet=True).strip())
    protected = [prefix / "bin/com8", prefix / "bin/communicate"]
    before = {str(p): fingerprint(p) for p in protected}
    report["protected_before"] = before
    if args.disposable_ci and any(value["kind"] != "absent" for value in before.values()):
        raise RuntimeError("disposable qualification requires absent com8/communicate commands; refusing replacement")
    # Refuse to install/upgrade dependencies: this is qualification on an
    # existing workstation. The formula still exercises their real opt paths.
    dependencies = {}
    for dependency in ("node", "python@3.14", "bash"):
        listed = subprocess.run([brew, "list", "--versions", dependency], env=brew_env, text=True, capture_output=True, timeout=60)
        versions = listed.stdout.strip()
        if not versions and not args.disposable_ci:
            raise RuntimeError(f"preinstall {dependency} before qualifying this host")
        dependencies[dependency] = versions
    report["dependencies_before"] = dependencies
    formula_name = "com8" if args.disposable_ci else "com8-qualification-" + report["sha256"][:10]
    classname = "".join(part.capitalize() for part in formula_name.split("-"))
    formula = args.formula.read_text()
    if "class Com8 < Formula" not in formula or '  license "MIT"' not in formula:
        raise RuntimeError("expected the rendered canonical COM8 formula")
    formula = formula.replace("class Com8 < Formula", f"class {classname} < Formula", 1)
    formula = re.sub(r'^  url ".*"$', '  url "' + archive.as_uri() + '"', formula, count=1, flags=re.M)
    formula = re.sub(r'^  sha256 ".*"$', '  sha256 "' + report["sha256"] + '"', formula, count=1, flags=re.M)
    if not args.disposable_ci:
        formula = formula.replace('  license "MIT"', '  license "MIT"\n  keg_only "temporary isolated COM8 qualification"', 1)
    formula_path = args.work / f"{formula_name}.rb"
    formula_path.write_text(formula)
    if (prefix / "opt" / formula_name).exists() or (prefix / "Cellar" / formula_name).exists():
        raise RuntimeError("candidate keg already exists; inspect it before rerunning qualification")
    tap = "com8-qualification/candidate-" + report["sha256"][:10]
    tapped = run([brew, "tap"], env=brew_env, quiet=True).splitlines()
    if tap in tapped:
        raise RuntimeError("candidate tap already exists; inspect it before rerunning qualification")
    qualified_name = tap + "/" + formula_name
    report["tap"] = tap
    installed = False
    tap_created = False
    temporary = Path(tempfile.mkdtemp(prefix="com8-brew-"))
    env = {**os.environ, "HOME": str(temporary), "COMMUNICATE_DATA": str(temporary / "data"),
           "COMM_STATE": str(temporary / "state"), "COM8_SOCK_DIR": str(temporary / "socks"),
           "COM8_SESSIONS_DIR": str(temporary / "sessions"), "COM8_SELF": "brew-fixture",
           "CODEX_HOME": str(temporary / "codex"), "CLAUDE_CONFIG_DIR": str(temporary / "claude")}
    for key in ("COM8_SOCK", "COM8_DAEMON_DIR", "CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "COMMUNICATE_HOME"):
        env.pop(key, None)
    stable = temporary / "data/bin/com8"
    daemon = OwnedDaemon(temporary, stable, env)
    report["daemon_lifecycles"] = daemon.records
    try:
        # Current Homebrew rejects arbitrary formula files. A private local tap
        # exercises normal formula loading without a source clone or publication.
        run([brew, "tap-new", "--no-git", tap], env=brew_env)
        tap_created = True
        tap_root = Path(run([brew, "--repository", tap], env=brew_env, quiet=True).strip())
        (tap_root / "Formula").mkdir(exist_ok=True)
        shutil.copyfile(formula_path, tap_root / "Formula" / formula_path.name)
        install_args = [brew, "install", "--formula", *([] if args.disposable_ci else ["--ignore-dependencies"]), qualified_name]
        run(install_args, env=brew_env, timeout=1800 if args.disposable_ci else 180)
        installed = True
        keg = prefix / "opt" / formula_name
        entry = keg / "bin/com8"
        if args.disposable_ci:
            entry = prefix / "bin/com8"
            assert entry.is_symlink() and entry.resolve() == (keg / "bin/com8").resolve(), "canonical com8 command was not linked"
            assert (prefix / "bin/communicate").is_symlink(), "compatibility command was not linked"
        report["installed_manifest"] = verify_runtime(keg / "libexec")
        tested = subprocess.run([brew, "test", qualified_name], env=brew_env, text=True, capture_output=True, timeout=180)
        print(tested.stdout + tested.stderr, end="", flush=True)
        if tested.returncode:
            # brew test insists on current metadata versions even when the
            # declared runtime dependencies are installed and work. Do not
            # upgrade an existing workstation to get past this separate gate.
            if args.disposable_ci or "is missing test dependencies:" not in tested.stdout + tested.stderr:
                raise RuntimeError("Homebrew formula test failed")
            report["formula_test"] = "unqualified: installed dependency versions do not satisfy brew test's current metadata"
        else:
            report["formula_test"] = "pass"
        minimal = {**env, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        version = run([entry, "version"], env=minimal)
        assert "COM8 " + report["installed_manifest"]["version"] in version
        assert "bus" in run([entry, "--help"], env=minimal)
        run([entry, "setup", "--no-clients", "--dry-run"], env=minimal)
        assert not (temporary / "data").exists(), "formula install/test unexpectedly ran setup"
        report["checks"].append("formula install and equivalent version/help/dry-run checks with minimal PATH")
        run([entry, "setup", "--no-clients"], env=env)
        daemon.begin_start()
        run([stable, "start"], env=env)
        daemon.capture_pid()
        run([stable, "claim", "brew-fixture"], env=env)
        run([stable, "send", "brew-fixture", "--from", "qualification", "--", "brew-reinstall-sentinel"], env=env)
        stored = run([stable, "inbox", "brew-fixture"], env=env)
        assert "brew-reinstall-sentinel" in stored
        daemon.stop()
        report["checks"].append("explicit isolated setup and durable claim/send/inbox")
        # Older host dependencies may not meet current tap metadata. Reinstall
        # exposes no --ignore-dependencies flag, so forbid changes to every
        # currently installed formula as a fail-closed dependency guard.
        names = run([brew, "list", "--formula"], env=brew_env, quiet=True).splitlines()
        reinstall_env = brew_env if args.disposable_ci else {**brew_env, "HOMEBREW_FORBIDDEN_FORMULAE": " ".join(name for name in names if name != formula_name)}
        result = subprocess.run([brew, "reinstall", "--formula", qualified_name], env=reinstall_env, text=True, capture_output=True, timeout=1800 if args.disposable_ci else 180)
        print(result.stdout + result.stderr, end="", flush=True)
        if result.returncode:
            if args.disposable_ci or "forbidden" not in (result.stdout + result.stderr).lower():
                raise RuntimeError("Homebrew reinstall failed")
            report["brew_reinstall"] = "unqualified: current metadata requires dependency changes; guarded reinstall refused"
            run([brew, "uninstall", "--formula", qualified_name], env=brew_env)
            installed = False
            run([brew, "install", "--formula", "--ignore-dependencies", qualified_name], env=brew_env)
            installed = True
            report["reinstall_method"] = "explicit uninstall then install with unchanged dependencies"
        else:
            report["brew_reinstall"] = "pass"
            report["reinstall_method"] = "native brew reinstall" if args.disposable_ci else "brew reinstall with dependency-change guard"
        report["reinstalled_manifest"] = verify_runtime(keg / "libexec")
        if args.disposable_ci:
            run([brew, "test", qualified_name], env=brew_env)
            report["reinstalled_formula_test"] = "pass"
        run([keg / "bin/communicate", "version"], env=minimal)
        daemon.begin_start()
        run([stable, "start"], env=env)
        daemon.capture_pid()
        assert "brew-reinstall-sentinel" in run([stable, "inbox", "brew-fixture"], env=env)
        daemon.stop()
        report["checks"].append("formula reinstall preserves stable installation and mail")
        run([brew, "uninstall", "--formula", qualified_name], env=brew_env)
        installed = False
        assert not (prefix / "opt" / formula_name).exists()
        daemon.begin_start()
        run([stable, "start"], env=env)
        daemon.capture_pid()
        assert "brew-reinstall-sentinel" in run([stable, "inbox", "brew-fixture"], env=env)
        daemon.stop()
        report["checks"].append("formula uninstall preserves explicit installation and mail")
        after = {str(p): fingerprint(p) for p in protected}
        assert before == after, "pre-existing executable changed"
        report["protected_after"] = after
        report["checks"].append("pre-existing brew executables unchanged")
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        cleanup_errors = []
        if not daemon.stop(required=False):
            cleanup_errors.append("isolated daemon shutdown unconfirmed")
        # A failed install can still have created a keg. Its name was absent
        # before this run, so this cleanup never owns a pre-existing formula.
        if installed or (prefix / "Cellar" / formula_name).exists():
            try:
                run([brew, "uninstall", "--formula", qualified_name], env=brew_env)
            except (RuntimeError, subprocess.TimeoutExpired):
                cleanup_errors.append("candidate keg removal")
        if tap_created:
            try:
                run([brew, "untap", tap], env=brew_env)
            except (RuntimeError, subprocess.TimeoutExpired):
                cleanup_errors.append("candidate tap removal")
        report["protected_after"] = {str(p): fingerprint(p) for p in protected}
        report["dependencies_after"] = {name: subprocess.run([brew, "list", "--versions", name], env=brew_env,
                                                             text=True, capture_output=True, timeout=60).stdout.strip()
                                        for name in dependencies}
        if report["protected_after"] != before:
            cleanup_errors.append("pre-existing executable changed")
        if not args.disposable_ci and report["dependencies_after"] != dependencies:
            cleanup_errors.append("dependency versions changed")
        finish_fixture_cleanup(report, temporary, cleanup_errors)
        (args.work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if cleanup_errors and "error" not in report:
            raise RuntimeError("qualification cleanup requires attention: " + ", ".join(cleanup_errors))
    print("PASS: Homebrew install, wrappers, reinstall and uninstall with preserved state; brew test: " + report["formula_test"], flush=True)


if __name__ == "__main__":
    main()
