#!/usr/bin/env python3
"""Add the managed launch hooks without modifying a provider configuration file."""
import json
from pathlib import Path
import shlex
import sys


def settings(command, supplied):
    value = {}
    if supplied:
        raw = supplied if supplied.lstrip().startswith("{") else Path(supplied).read_text()
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Claude --settings must be a JSON object")
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude hooks must be an object")
    for event, matcher, verb in [("SessionStart", "", "session-start"),
                                  ("StopFailure", "rate_limit", "stop-failure")]:
        entry = {"matcher": matcher, "hooks": [{"type": "command",
                 "command": shlex.quote(command) + " hook " + verb}]}
        rows = hooks.setdefault(event, [])
        if not isinstance(rows, list):
            raise ValueError("Claude hook events must be lists")
        if entry not in rows:
            rows.append(entry)
    return value


if __name__ == "__main__":
    try:
        print(json.dumps(settings(*sys.argv[1:]), separators=(",", ":")))
    except (ValueError, OSError, TypeError):
        # A settings object can carry private values; do not echo its contents.
        sys.exit("com8-account: cannot merge managed hooks into Claude --settings")
