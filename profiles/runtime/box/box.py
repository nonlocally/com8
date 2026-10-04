#!/usr/bin/env python3
"""Optional Apple/container adapter; never installs or starts a runtime.

Adapted from Anu 80d3c86 config/bash/fns/box at the owner's direction.
Only explicit run/build commands mutate anything. No source-checkout fallback.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent


class BoxError(Exception):
    pass


def setting(name, default):
    return os.environ.get("COM8_BOX_" + name) or os.environ.get("ANU_BOX_" + name) or default


def host_path(value, must_exist=True):
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise BoxError("box paths must be absolute: " + str(raw))
    result = raw.resolve()
    if any(c in str(result) for c in ":\n\r\0"):
        raise BoxError("box volume paths cannot contain colon or control characters")
    if must_exist and not result.is_dir():
        raise BoxError("box mount directory does not exist: " + str(result))
    return result


def git(cwd, *args):
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def runtime():
    configured = os.environ.get("COM8_BOX_RUNTIME", "container")
    found = shutil.which(configured)
    if not found:
        raise BoxError("Apple/container CLI unavailable; install/configure it separately (COM8_BOX_RUNTIME)")
    return found


def check_runtime(tool):
    result = subprocess.run([tool, "system", "status"], capture_output=True, text=True, timeout=15)
    if result.returncode or not re.search(r"^status\s+running\b", result.stdout, re.M):
        raise BoxError("container runtime is stopped; start it explicitly with `container system start`")


def image():
    value = setting("IMAGE", "com8-agent")
    if value.startswith("-") or any(c.isspace() for c in value):
        raise BoxError("invalid box image name")
    return value


def configuration(command):
    cwd = host_path(Path.cwd())
    mounts, create = [], []

    def mount(source, destination=None, readonly=False, purpose="workspace"):
        entry = {"source": str(source), "destination": str(destination or source),
                 "read_only": readonly, "purpose": purpose}
        if entry not in mounts:
            mounts.append(entry)

    repository = git(cwd, "rev-parse", "--show-toplevel")
    workspace = host_path(repository) if repository else cwd
    mount(workspace)
    if repository:
        shared = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not shared:
            raise BoxError("cannot resolve the shared Git directory; update Git before using a linked worktree")
        shared = host_path(shared)
        if shared != workspace and workspace not in shared.parents:
            # A linked worktree needs the shared Git metadata, not the other
            # checkout's source files. Absolute .git pointers keep working.
            mount(shared, purpose="shared-git")

    state = host_path(os.environ.get("COM8_BOX_STATE") or
                      str(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "com8/box"), False)
    explicit_claude = os.environ.get("COM8_BOX_CLAUDE_HOME")
    if not explicit_claude and (state / "claude").is_symlink():
        raise BoxError("default box Claude home is a symlink; configure the existing home explicitly")
    claude = host_path(explicit_claude or str(state / "claude"), bool(explicit_claude))
    if not explicit_claude:
        create.append(claude)
    mount(claude, "/root/.claude", purpose="claude-home")
    if os.environ.get("COM8_BOX_CODEX_HOME"):
        mount(host_path(os.environ["COM8_BOX_CODEX_HOME"]), "/root/.codex", purpose="codex-home")

    environment = {"TERM": os.environ.get("TERM", "xterm-256color")}
    helper = os.environ.get("COM8_BOX_PANE_BIN")
    replies = os.environ.get("COM8_BOX_PANE_DIR")
    if bool(helper) != bool(replies):
        raise BoxError("configure COM8_BOX_PANE_BIN and COM8_BOX_PANE_DIR together")
    if helper:
        helper = host_path(helper)
        if not os.access(helper / "pane", os.X_OK):
            raise BoxError("COM8_BOX_PANE_BIN must contain an executable pane helper")
        replies = host_path(replies)
        mount(helper, "/opt/com8/pane", readonly=True, purpose="pane-helper")
        mount(replies, purpose="pane-replies")
        environment.update(ANU_PANE_DIR=str(replies), COM8_PANE_DIR=str(replies))

    # Name-only flags keep secrets out of argv and the plan. An explicit
    # subscription token takes precedence over an ambient Anthropic API key.
    names = ["CLAUDE_CODE_OAUTH_TOKEN"] if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else ["ANTHROPIC_API_KEY"]
    names += ["OPENAI_API_KEY", "COM8_ACCOUNT", "ANU_ACCOUNT"]
    for name in names:
        if os.environ.get(name):
            environment[name] = os.environ[name]
    for field, suffix in [("user.name", "NAME"), ("user.email", "EMAIL")]:
        value = git(cwd, "config", field)
        if value:
            environment["GIT_AUTHOR_" + suffix] = value
            environment["GIT_COMMITTER_" + suffix] = value
    cpus, memory = setting("CPUS", "2"), setting("MEMORY", "4G")
    if not re.fullmatch(r"[1-9][0-9]*", cpus) or not re.fullmatch(r"[1-9][0-9]*[KMGTP]B?", memory, re.I):
        raise BoxError("box CPUs must be a positive integer; memory must be a size such as 4G")
    return {"backend": "apple/container", "image": image(), "cwd": str(cwd),
            "cpus": cpus, "memory": memory, "mounts": mounts,
            "environment_names": sorted(environment), "command": command or ["bash"],
            "create_private_directories": [str(p) for p in create]}, environment


def private_directory(path):
    # Explicit provider homes are user-owned configuration and never modified
    # here. Only the separate default box home is created/repaired.
    target = Path(path)
    if target.exists() and (not target.is_dir() or target.stat().st_uid != os.getuid()):
        raise BoxError("box state directory is not owned by this user: " + str(target))
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)


def run(command, preview=False):
    plan, environment = configuration(command)
    if preview:
        print(json.dumps({"read_only": True, **plan}, indent=2))
        return 0
    tool = runtime()
    check_runtime(tool)
    checked = subprocess.run([tool, "image", "inspect", plan["image"]], capture_output=True, timeout=15)
    if checked.returncode:
        raise BoxError("box image is missing; run `com8-box build` explicitly or configure COM8_BOX_IMAGE")
    for directory in plan["create_private_directories"]:
        private_directory(directory)
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", Path(plan["cwd"]).name).strip("-") or "root"
    argv = [tool, "run", "--rm", "--name", "com8-box-" + slug + "-" + uuid.uuid4().hex[:8],
            "--cpus", plan["cpus"], "--memory", plan["memory"], "--workdir", plan["cwd"], "--interactive"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        argv.append("--tty")
    for mount in plan["mounts"]:
        argv += ["--volume", mount["source"] + ":" + mount["destination"] + (":ro" if mount["read_only"] else "")]
    for name in plan["environment_names"]:
        argv += ["--env", name]
    argv += [plan["image"], *plan["command"]]
    return subprocess.call(argv, env={**os.environ, **environment})


def doctor():
    tool = runtime()
    check_runtime(tool)
    result = subprocess.run([tool, "image", "inspect", image()], capture_output=True, timeout=15)
    print(json.dumps({"backend": "apple/container", "runtime": tool, "running": True,
                      "image": image(), "image_present": result.returncode == 0,
                      "read_only": True}, indent=2))
    return 0 if result.returncode == 0 else 1


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    operation = args.pop(0) if args and args[0] in ["plan", "doctor", "build", "run", "help", "--help", "-h"] else "run"
    if operation in ["help", "--help", "-h"]:
        print("com8-box [run] [--] COMMAND [ARGS...]   run in a disposable Apple/container VM\n"
              "com8-box plan [--] COMMAND [ARGS...]    inspect mounts/env names without changes\n"
              "com8-box doctor                        inspect existing runtime and image\n"
              "com8-box build                         explicitly build the bundled agent image\n"
              "No command defaults to bash. Runtime startup and installation are always separate.")
        return 0
    if args and args[0] == "--":
        args.pop(0)
    if operation in ["doctor", "build"] and args:
        raise BoxError(operation + " takes no arguments")
    if operation == "doctor":
        return doctor()
    if operation == "build":
        tool = runtime()
        check_runtime(tool)
        return subprocess.call([tool, "build", "--tag", image(), "--file", str(HERE / "Containerfile"), str(HERE)])
    return run(args, preview=operation == "plan")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BoxError, OSError, subprocess.TimeoutExpired) as error:
        print("com8-box: " + str(error), file=sys.stderr)
        sys.exit(1)
