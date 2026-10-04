#!/usr/bin/env python3
"""Qualify an extracted COM8 release with a real disposable-runner user manager.

Refuses shared hosts before opening the artifact or creating files. The runner's
real HOME is required for systemd's default unit search; only fresh data/state,
socket/session roots and one uniquely scoped service belong to this test.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def fingerprint(path):
    if path.is_symlink():
        return {"link": os.readlink(path)}
    if path.is_file():
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": path.stat().st_mode & 0o777}
    return None


class Proof:
    def __init__(self, runtime, work, bootstrap):
        self.runtime, self.work, self.bootstrap = runtime, work, bootstrap
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        require(Path(os.environ["HOME"]).resolve() == self.home, "HOME must be the runner account's real home")
        self.work.mkdir(parents=True, mode=0o700)
        os.chmod(self.work, 0o700)
        self.base = Path(tempfile.mkdtemp(prefix="com8-sd-", dir="/tmp"))
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("COM8_", "COMM_", "COMMUNICATE_", "XDG_", "DBUS_", "CLAUDE_", "CODEX_"))}
        self.env.update(HOME=str(self.home), COMMUNICATE_DATA=str(self.base / "data"),
                        COMM_STATE=str(self.base / "state"), COM8_SELF="systemd-proof",
                        COM8_SOCK_DIR=str(self.base / "socks"), COM8_SESSIONS_DIR=str(self.base / "sessions"),
                        COM8_TMUX_SOCKET=str(self.base / "tmux.sock"),
                        CLAUDE_CONFIG_DIR=str(self.base / "claude"), CODEX_HOME=str(self.base / "codex"),
                        XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}",
                        DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{os.getuid()}/bus",
                        PYTHONDONTWRITEBYTECODE="1")
        self.report = {"ok": False, "mode": "real systemd user manager on disposable GitHub-hosted runner",
                       "checks": [], "commands": [], "temporary": str(self.base), "home": str(self.home)}
        self.label = self.unit = None
        self.linger_changed = False
        self.protected = None
        self.setup_attempted = False

    def run(self, *argv, check=True, timeout=60):
        result = subprocess.run([str(a) for a in argv], env=self.env, cwd=self.base,
                                text=True, capture_output=True, timeout=timeout)
        self.report["commands"].append({"argv": list(map(str, argv)), "returncode": result.returncode})
        require(not check or result.returncode == 0,
                f"command failed ({result.returncode}): {argv[:3]}: {(result.stdout + result.stderr)[-3000:]}")
        return result

    def manager(self, *args, **kwargs):
        return self.run("systemctl", "--user", *args, **kwargs)

    def state(self, label):
        result = self.manager("show", label, "--property=LoadState", "--property=FragmentPath",
                              "--property=ActiveState", "--property=UnitFileState", "--property=MainPID",
                              "--property=ExecMainStartTimestampMonotonic", check=False)
        rows = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        require("LoadState" in rows, "user manager did not return a unit state: " + result.stderr[-1000:])
        return rows

    def protected_state(self):
        paths = self.home / ".config/systemd/user"
        files = {str(p.relative_to(paths)): fingerprint(p) for p in paths.rglob("*")
                 if "com8" in p.name and p != self.unit and (p.is_file() or p.is_symlink())} if paths.exists() else {}
        units = set()
        for command in ("list-units", "list-unit-files"):
            # Some systemd releases exit 1 for an unmatched unit-file pattern.
            # Read the inventory successfully, then select COM8 names locally;
            # a manager failure must still fail this protection check.
            result = self.manager(command, "--all", "--no-legend", "--plain")
            units.update(line.split()[0] for line in result.stdout.splitlines()
                         if line.split() and "com8" in line.split()[0])
        units.discard(self.label)
        return {"files": files, "units": {name: self.state(name) for name in sorted(units)},
                "client_configs": {str(p): fingerprint(p) for p in
                                   [self.home / ".claude/settings.json", self.home / ".codex/config.toml"]}}

    def connect_manager(self):
        ready = self.manager("show-environment", check=False)
        if ready.returncode:
            require(self.bootstrap, "user manager unavailable; --start-user-manager is required on this disposable runner")
            # Starting a user's default target can activate already-enabled
            # units. Only bootstrap a fresh runner with no existing COM8 units.
            for root in (self.home / ".config/systemd/user", self.home / ".local/share/systemd/user",
                         Path("/etc/systemd/user"), Path("/usr/lib/systemd/user")):
                require(not root.exists() or not any("com8" in p.name for p in root.rglob("*")),
                        "manager bootstrap refused: pre-existing COM8 unit files require an already-running manager")
            account = pwd.getpwuid(os.getuid()).pw_name
            linger = self.run("loginctl", "show-user", account, "--property=Linger", "--value", check=False).stdout.strip()
            if linger != "yes":
                self.run("sudo", "-n", "loginctl", "enable-linger", account)
                self.linger_changed = True
            self.run("sudo", "-n", "systemctl", "start", f"user@{os.getuid()}.service")
            for _ in range(50):
                ready = self.manager("show-environment", check=False)
                if ready.returncode == 0:
                    break
                time.sleep(0.1)
            require(ready.returncode == 0, "runner user manager did not become available")
            self.report["manager_bootstrapped"] = True
        values = dict(line.split("=", 1) for line in ready.stdout.splitlines() if "=" in line)
        require(not values.get("XDG_CONFIG_HOME") or Path(values["XDG_CONFIG_HOME"]).resolve() == self.home / ".config",
                "existing manager uses a nonstandard config root; refusing mismatched unit search")
        self.report["systemd_version"] = self.run("systemctl", "--version").stdout.splitlines()[0]

    def cli(self, *args, **kwargs):
        return self.run(self.runtime / "bin/com8", *args, **kwargs)

    def status(self):
        result = self.cli("status", "--json", check=False)
        return json.loads(result.stdout) if result.returncode == 0 else None

    def await_runtime(self, previous_pid=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = self.status()
            if status and status.get("ok") and status["self"]["pid"] != previous_pid:
                actual = self.state(self.label)
                if actual.get("ActiveState") == "active" and int(actual.get("MainPID", "0")) == status["self"]["pid"]:
                    return status, actual
            time.sleep(0.1)
        raise RuntimeError("managed daemon did not become ready with the expected manager PID")

    def check(self, description):
        self.report["checks"].append(description)
        print("PASS: " + description, flush=True)

    def execute(self):
        spec = importlib.util.spec_from_file_location("artifact_qualification", Path(__file__).with_name("qualify-installed.py"))
        artifact = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(artifact)
        self.report["artifact"] = artifact.artifact(self.runtime)
        artifact_before = artifact.files(self.runtime)
        self.connect_manager()
        # Import only installed lifecycle code to derive its public scope; no
        # service is written or started by this definition call.
        program = ('import {pathToFileURL} from "node:url";'
                   'const m=await import(pathToFileURL(process.argv[1]));'
                   'console.log(JSON.stringify(m.serviceDefinition("linux",process.argv[2],"systemd-proof")));')
        definition = json.loads(self.run("node", "--input-type=module", "-e", program,
                                        self.runtime / "src/lifecycle.mjs", shutil.which("python3")).stdout)
        self.label, self.unit = definition["label"], Path(definition["path"])
        require(self.label.startswith("communicate-com8-") and self.label.endswith(".service"), "qualification needs a scoped unit")
        require(self.unit == self.home / ".config/systemd/user" / self.label, "unexpected user unit search path")
        require(not self.unit.exists() and not self.unit.is_symlink() and self.state(self.label)["LoadState"] == "not-found",
                "qualification unit already exists; refusing to adopt it")
        self.report.update(unit=self.label, unit_path=str(self.unit))
        self.protected = self.protected_state()
        self.report["protected_before"] = self.protected
        setup = ["setup", "--no-clients", "--service", "--service-inherit=COM8_SOCK_DIR",
                 "--service-inherit=COM8_SESSIONS_DIR", "--service-inherit=COM8_TMUX_SOCKET",
                 "--service-inherit=CLAUDE_CONFIG_DIR", "--service-inherit=CODEX_HOME"]
        self.setup_attempted = True
        self.cli(*setup)
        first, first_unit = self.await_runtime()
        active = (self.base / "data/current").resolve(strict=True)
        require(first["self"]["source_file"] == str(active / "vendor/lib/com8.py"), "daemon uses a source checkout or another payload")
        require(first["self"]["source_commit"] == self.report["artifact"]["source"]["commit"], "daemon commit does not match the artifact")
        require(first["self"]["sock_dir"] == self.env["COM8_SOCK_DIR"], "service lost its private socket root")
        require(Path(first_unit["FragmentPath"]).resolve() == self.unit.resolve(), "manager loaded a different unit")
        require(first_unit["UnitFileState"] == "enabled", "service was not enabled")
        service_record = json.loads((self.base / "data/install.json").read_text())["service"]
        for key in ("COM8_SOCK_DIR", "COM8_SESSIONS_DIR", "COM8_TMUX_SOCKET", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            require(service_record["environment"][key] == self.env[key], "service omitted an explicit fixture environment value")
        self.report["first_status"] = first["self"]
        self.check("CLI-only setup starts the scoped installed payload with private roots and matching manager PID")

        self.cli("claim", "systemd-proof")
        sentinel = "systemd-persistence-'literal'-$HOME-`no-shell`-" + self.base.name
        self.cli("send", "systemd-proof", "--from", "qualification", "--", sentinel)
        require(sentinel in self.cli("inbox", "systemd-proof").stdout, "durable inbox lacks literal sentinel")
        mail_path = self.base / "state/com8/mail/systemd-proof/inbox.jsonl"
        mail = mail_path.read_bytes()
        identities = json.loads((self.base / "state/com8/identities.json").read_text())
        unit_before = fingerprint(self.unit)
        self.cli(*setup)
        repeated, repeated_unit = self.await_runtime()
        require(repeated["self"]["pid"] == first["self"]["pid"], "repeat setup restarted the unchanged daemon")
        require(repeated_unit["ExecMainStartTimestampMonotonic"] == first_unit["ExecMainStartTimestampMonotonic"], "repeat setup replaced the process")
        require(fingerprint(self.unit) == unit_before and (self.base / "data/current").resolve() == active, "repeat setup changed unchanged service/code")
        self.check("repeat setup preserves the same daemon PID, unit bytes and active release")

        self.manager("restart", self.label)
        restarted, _ = self.await_runtime(first["self"]["pid"])
        require(restarted["self"]["source_file"] == first["self"]["source_file"], "restart changed runtime provenance")
        require(mail_path.read_bytes() == mail and json.loads((self.base / "state/com8/identities.json").read_text()) == identities,
                "manager restart changed durable identity or mail")
        require(sentinel in self.cli("inbox", "systemd-proof").stdout, "restarted service cannot read preserved mail")
        self.report["restarted_pid"] = restarted["self"]["pid"]
        self.check("actual systemd restart replaces the PID and retains the durable identity and literal message")

        # A retry belongs to failure cleanup, never to this acceptance check.
        self.cli("uninstall", "--no-clients")
        removed = self.state(self.label)
        require(removed["LoadState"] == "not-found" and removed["ActiveState"] == "inactive", "first uninstall left the service loaded")
        require(not self.unit.exists() and not self.unit.is_symlink(), "first uninstall left the owned unit")
        require(not Path(f'/proc/{restarted["self"]["pid"]}').exists(), "first uninstall left the managed process alive")
        require(self.status() is None, "first uninstall left a responding daemon")
        require(mail_path.read_bytes() == mail and json.loads((self.base / "state/com8/identities.json").read_text()) == identities,
                "uninstall removed or changed persistent identity/mail")
        self.check("first uninstall stops and removes the owned service while preserving identity and mail")
        require(artifact.files(self.runtime) == artifact_before, "qualification mutated the input artifact")
        require(self.protected_state() == self.protected, "pre-existing COM8 units or client configuration changed")
        self.check("input artifact, pre-existing COM8 units and client configuration remain unchanged")
        self.report["ok"] = True

    def cleanup(self):
        errors = []
        ledger_service = None
        ledger = self.base / "data/install.json"
        if self.setup_attempted and ledger.exists():
            try:
                ledger_service = json.loads(ledger.read_text()).get("service")
            except Exception as error:
                errors.append("cannot inspect owned service ledger: " + str(error))
        if self.setup_attempted and self.unit and (ledger_service or self.unit.exists() or self.unit.is_symlink()):
            # Use the installer's ownership checks; never blanket-stop COM8 units
            # or unlink a changed/foreign service just to make cleanup pass.
            try:
                self.cli("uninstall", "--no-clients")
            except Exception as error:
                errors.append("owned installer cleanup failed: " + str(error))
        if self.label and self.setup_attempted:
            try:
                state = self.state(self.label)
                require(state["LoadState"] == "not-found" and state["ActiveState"] == "inactive", "owned service remains loaded")
                if self.protected is not None:
                    after = self.protected_state()
                    self.report["protected_after"] = after
                    require(after == self.protected, "protected units or client configuration changed")
            except Exception as error:
                errors.append(str(error))
        if self.linger_changed:
            try:
                self.run("sudo", "-n", "loginctl", "disable-linger", pwd.getpwuid(os.getuid()).pw_name)
                self.report["linger_restored"] = True
            except Exception as error:
                errors.append(str(error))
        self.report["cleanup_errors"] = errors
        if errors:
            self.report["ok"] = False
        else:
            shutil.rmtree(self.base)
        output = self.work / "report.json"
        output.write_text(json.dumps(self.report, indent=2) + "\n")
        output.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--disposable-ci", action="store_true")
    parser.add_argument("--start-user-manager", action="store_true",
                        help="If needed, enable runner lingering and start user@UID.service; restore lingering afterwards")
    args = parser.parse_args()
    # Keep this guard ahead of artifact reads, mkdir, subprocesses and managers.
    if not (args.disposable_ci and sys.platform == "linux" and os.getuid() != 0 and
            os.environ.get("GITHUB_ACTIONS") == "true" and
            os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted" and
            Path(os.environ.get("RUNNER_TEMP", "/nonexistent")).is_dir()):
        parser.error("requires --disposable-ci on a non-root disposable GitHub-hosted Linux runner; shared hosts are refused")
    require(args.work.resolve().is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()), "evidence must be inside RUNNER_TEMP")
    require(not args.work.exists(), "evidence directory already exists; use a fresh run")
    proof = Proof(args.runtime.resolve(strict=True), args.work, args.start_user_manager)
    try:
        proof.execute()
    except Exception as error:
        proof.report["error"] = str(error)
        print("FAIL: " + str(error), file=sys.stderr, flush=True)
        if proof.setup_attempted and proof.label:
            try:
                journal = proof.run("journalctl", "--user-unit=" + proof.label,
                                    "--no-pager", "-n", "80", check=False, timeout=10)
                proof.report["owned_unit_journal"] = (journal.stdout + journal.stderr)[-16000:]
            except Exception as journal_error:
                proof.report["journal_error"] = str(journal_error)
    finally:
        proof.cleanup()
    return 0 if proof.report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
