"""Observed lifecycle boundaries for isolated COM8 test daemons only."""
import os
from pathlib import Path
import subprocess
import sys
import time


def _tail(path, limit=4096):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode("utf-8", "replace")
    except OSError as error:
        return str(error)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An exited orphan may remain a zombie briefly; it cannot own sockets or
    # write state. The fixture never signals or reaps a process it did not start.
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=1)
    return result.returncode == 0 and not result.stdout.lstrip().startswith("Z")


def lifecycle(operation, communicate, state, *, timeout=10, env=None):
    state = Path(state)
    pid = None
    output = ""
    paths = [state / name for name in ("daemon.pid", "daemon.lock", "com8.sock")]
    try:
        if operation == "stop":
            pid = int(paths[0].read_text().strip())
            if pid <= 1:
                raise ValueError("invalid owned fixture PID")
        result = subprocess.run([str(communicate), "com8", operation], env=env,
                                capture_output=True, text=True, timeout=timeout)
        output = (result.stdout + result.stderr)[-4096:]
        if result.returncode:
            raise RuntimeError(f"{operation} exited {result.returncode}")
        if operation == "stop":
            deadline = time.monotonic() + timeout
            while _alive(pid) or any(path.exists() for path in paths):
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned fixture daemon did not exit and release state")
                time.sleep(0.05)
    except Exception as error:
        if isinstance(error, subprocess.TimeoutExpired):
            output = str(error.stdout or "")[-4096:] + str(error.stderr or "")[-4096:]
        present = [path.name for path in paths if path.exists()]
        raise RuntimeError(
            f"fixture {operation} failed: {error}; pid={pid}; remaining={present}\n"
            f"command output:\n{output}\ndaemon log tail:\n{_tail(state / 'daemon.log')}"
        ) from error


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("start", "stop"):
        raise SystemExit("usage: com8_fixture_lifecycle.py start|stop COMMUNICATE STATE")
    try:
        lifecycle(*sys.argv[1:])
    except RuntimeError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
