#!/usr/bin/env python3
"""Exercise selected dependency installation on a disposable macOS CI runner.

Uses the extracted release, real Homebrew packages and real client plugin CLIs.
Never logs in, requests a model response, or starts a service. Shared hosts are
refused before artifact access or subprocess execution.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--disposable-ci", action="store_true")
    args = parser.parse_args()
    runner_temp = os.environ.get("RUNNER_TEMP")
    if not (args.disposable_ci and sys.platform == "darwin" and os.getuid() != 0
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted" and runner_temp):
        parser.error("requires a non-root disposable GitHub-hosted macOS runner")
    work = args.work.resolve()
    require(Path(runner_temp).resolve() in work.parents, "evidence must be inside RUNNER_TEMP")
    require(not work.exists(), "evidence already exists; refusing to reuse it")
    runtime = args.runtime.resolve(strict=True)
    manifest = json.loads((runtime / "release.json").read_text())
    brew = shutil.which("brew")
    require(brew is not None, "Homebrew must be provisioned by the CI job")
    work.mkdir(mode=0o700)
    home = work / "home"
    home.mkdir(mode=0o700)
    report = {"ok": False, "source": manifest["source"], "checks": [],
              "authentication_attempted": False, "model_requests": 0,
              "services_started": False, "third_party_packages_removed": False}
    # No provider, GitHub, SSH-agent, bus or controller credentials enter the
    # fresh home. Homebrew keeps its normal prefix; only this disposable runner
    # permits installing the selected third-party packages there.
    env = {key: os.environ[key] for key in ("PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")
           if key in os.environ}
    env.update(HOME=str(home), SHELL="/bin/zsh", TERM="dumb", CI="1",
               COMMUNICATE_DATA=str(home / "data"), COMM_STATE=str(home / "state"),
               CODEX_HOME=str(home / ".codex"), CLAUDE_CONFIG_DIR=str(home / ".claude"),
               COM8_SOCK_DIR=str(home / "sockets"), COM8_SESSIONS_DIR=str(home / "sessions"),
               HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1",
               HOMEBREW_NO_INSTALL_UPGRADE="1", HOMEBREW_NO_INSTALL_CLEANUP="1",
               HOMEBREW_NO_AUTOREMOVE="1")
    entry = runtime / "bin/com8"
    calls = 0

    def run(argv, *, check=True, timeout=120, command_env=None):
        nonlocal calls
        calls += 1
        result = subprocess.run([str(a) for a in argv], env=env if command_env is None else command_env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        log = work / f"command-{calls:02d}.log"
        log.write_text(result.stdout + result.stderr)
        log.chmod(0o600)
        report.setdefault("commands", []).append({"argv": [str(a) for a in argv],
                                                 "exit": result.returncode, "log": log.name})
        if check:
            require(result.returncode == 0, f"command {calls} failed; see {log.name}")
        return result

    def verify_payload():
        for name, digest in manifest["files"].items():
            file = runtime / name
            require(file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == digest,
                    "artifact changed: " + name)
        actual = {file.relative_to(runtime).as_posix() for file in runtime.rglob("*")
                  if file.is_file() and not file.is_symlink() and file != runtime / "release.json"}
        require(actual == set(manifest["files"]), "artifact regular-file inventory changed")

    def cancel_interactive_setup():
        # Real TTY dispatch/readline cancellation, with no consent to install.
        # The complete package installation below is deliberately automated.
        master, slave = pty.openpty()
        process = None
        output = bytearray()
        sent_eof = False
        try:
            process = subprocess.Popen([str(entry), "setup"], env=env,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    if not sent_eof and b"Set up Claude Code" in output:
                        os.write(master, b"\x04")
                        sent_eof = True
                elif process.poll() is not None:
                    break
            require(sent_eof, "bare setup did not offer guided choices on a real TTY")
            require(process.wait(timeout=5) == 130, "TTY EOF did not cancel setup")
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
            if slave is not None:
                os.close(slave)
            os.close(master)
            log = work / "interactive-cancel.log"
            log.write_bytes(output)
            log.chmod(0o600)

    def no_config_writes():
        return not any((home / relative).exists() for relative in
                       ("data", "state", ".config/com8", ".claude", ".codex", ".zshrc", ".bashrc", ".local/bin"))

    def installed_packages():
        return {kind: run([brew, "list", "--" + kind, "--versions"]).stdout.splitlines()
                for kind in ("formula", "cask")}

    choices = ["--install-missing", "--claude", "--codex", "--terminal", "--mesh", "--ghostty", "--no-service"]
    setup_attempted = False
    try:
        verify_payload()
        before = installed_packages()
        report["packages_before"] = before
        # Fresh client homes must really exercise installing the provider CLIs,
        # rather than treating an already-provisioned CI image as a fresh proof.
        require(not shutil.which("claude", path=env["PATH"]) and
                not shutil.which("codex", path=env["PATH"]),
                "fresh-client proof requires absent provider CLIs on this disposable image")
        cancel_interactive_setup()
        require(no_config_writes(), "cancelled TTY setup wrote configuration")
        report["checks"].append("bare setup starts the real TTY guide; EOF cancels before configuration")
        run([entry, "setup", *choices, "--dry-run"])
        require(no_config_writes(), "dry-run wrote installation, profile or client configuration")
        require(before == installed_packages(), "dry-run changed installed software")
        report["checks"].append("fresh dependency plan is inert")
        rejected = run([entry, "setup", *choices], check=False)
        require(rejected.returncode != 0 and no_config_writes(),
                "noninteractive dependency install proceeded without --yes")
        setup_attempted = True
        run([entry, "setup", *choices, "--yes"], timeout=1500)
        installed = installed_packages()
        report["packages_after_setup"] = installed
        ledger = json.loads((home / "data/install.json").read_text())
        require(set(ledger["clients"]) == {"claude", "codex"}, "both client integrations must be owned")
        require(not ledger.get("service"), "setup installed a service without selection")
        require((home / ".config/com8/profiles/active.sh").is_file(), "terminal/mesh profile missing")
        require((home / ".local/bin/com8-agent").is_file(), "agent launcher missing")
        commands = home / ".local/bin"
        for shortcut in ("cx", "cxx", "cxc", "cdx", "cdxx", "cdxxs", "al", "alw", "t"):
            require((commands / shortcut).is_file(), "shell-independent shortcut missing: " + shortcut)
        require(not (commands / "com8-shell").exists(), "setup replaced the interactive shell")
        profile = home / ".config/com8/profiles"
        require("default-shell" not in (profile / "tmux.conf").read_text(), "profile forces tmux shell")
        require("command =" not in (profile / "ghostty.conf").read_text(), "profile forces Ghostty shell")
        # Exercise the installed launchers from a fresh zsh with a GUI-like
        # PATH. An explicit inert account adapter prevents launching a real
        # provider even though both provider packages are now installed.
        adapter = work / "inert-account-adapter"
        adapter.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        adapter.chmod(0o700)
        literal = "literal 'quotes' $HOME `not-a-command`\nsecond line"
        shell_env = dict(env, PATH="/usr/bin:/bin", ZDOTDIR=str(home),
                         COM8_ACCOUNT_LAUNCHER=str(adapter))
        shell = run(["/bin/zsh", "-i", "-c",
                     '[[ -n $ZSH_VERSION ]] || exit 1; command -v cxx cdxx; '
                     'cxx "$1"; cdxx "$1"; printf "shell=%s\\n" "$SHELL"',
                     "fixture", literal], command_env=shell_env)
        expected = (str(commands / "cxx") + "\n" + str(commands / "cdxx") + "\n"
                    + "launch\n--provider\nclaude\n--\n--dangerously-skip-permissions\n" + literal + "\n"
                    + "launch\n--provider\ncodex\n--\n--yolo\n" + literal + "\nshell=/bin/zsh\n")
        require(shell.stdout == expected, "fresh zsh shortcut arguments or interactive shell changed")
        report["checks"].append("installed shortcuts work from fresh zsh with literal argv; interactive shell preserved")
        for client in ("claude", "codex"):
            run([client, "--version"])
        claude = json.loads(run(["claude", "plugin", "list", "--json"]).stdout)
        require(any(p.get("id") == "communicate@communicate" and p.get("enabled") for p in claude),
                "Claude cannot see enabled COM8 plugin")
        codex = json.loads(run(["codex", "plugin", "list", "--marketplace", "communicate", "--json"]).stdout)
        require(any(p.get("pluginId") == "communicate@communicate" and p.get("installed") and
                    p.get("enabled") for p in codex["installed"]),
                "Codex cannot see enabled COM8 plugin")
        report["checks"].append("real missing dependencies and both provider CLIs installed; plugins enabled without login")
        run([entry, "setup", *choices, "--yes"], timeout=240)
        require(installed == installed_packages(), "repeat setup changed third-party versions")
        report["checks"].append("repeat setup reuses installed software")
        saved = (home / "data/install.json").read_bytes()
        rejected = run([entry, "update", "--install-missing", "--yes"], check=False)
        require(rejected.returncode != 0 and (home / "data/install.json").read_bytes() == saved,
                "update accepted setup-only dependency installation flags")
        report["checks"].append("ordinary update cannot acquire setup-only dependency side effects")
        run([entry, "profile", "uninstall"])
        run([entry, "uninstall"])
        require(installed == installed_packages(), "COM8 uninstall removed user software")
        report["checks"].append("uninstall preserves third-party software")
        verify_payload()
        report["checks"].append("release payload remains immutable")
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        if setup_attempted and not report["ok"]:
            # Preserve any failed first attempt in the report. Retry only owned
            # configuration cleanup; never delete packages or the evidence home.
            for command in ([entry, "profile", "uninstall"], [entry, "uninstall"]):
                try:
                    result = run(command, check=False)
                    report.setdefault("cleanup", []).append({"command": str(command[-1]), "exit": result.returncode})
                except Exception as error:
                    report.setdefault("cleanup", []).append({"error": type(error).__name__})
        dest = work / "report.json"
        dest.write_text(json.dumps(report, indent=2) + "\n")
        dest.chmod(0o600)
    print(json.dumps({"ok": report["ok"], "checks": len(report["checks"]),
                      "error": report.get("error"), "report": str(work / "report.json")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
