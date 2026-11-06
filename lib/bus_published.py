"""A service's published agent list: the one validator the worker and the hub share.

The list is a service's claim about the agents it runs. Each entry carries an
`id` and may carry `name`, `status`, `role` and `parent`, nothing else, so no
message text can ride along. A bad entry is dropped on its own with a reason;
the rest of the list stands. Stdlib only; no local state.
"""
import json

MAX_PUBLISHED_AGENTS = 256
MAX_PUBLISHED_AGENTS_BYTES = 32768
PUBLISHED_AGENT_FIELDS = {"id": 128, "name": 128, "status": 16, "role": 64, "parent": 128}
PUBLISHED_STATUSES = ("live", "queueable", "offline")


def published_text(value, limit):
    """A bounded printable string: UTF-8 bytes within the limit, not blank, no control characters."""
    return (isinstance(value, str) and len(value.encode("utf-8")) <= limit and bool(value.strip())
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value))


def validate_published_agents(value):
    """Return (kept, dropped): the entries kept, normalised, in order, and (index, reason) per dropped one.

    Raises ValueError when the value is not a list at all. Defaults: `name` is
    the id, `status` is queueable. A parent that is not in the list, or the
    entry itself, is removed so the entry hangs from the service. The list is
    cut at MAX_PUBLISHED_AGENTS entries and then at MAX_PUBLISHED_AGENTS_BYTES.
    """
    if not isinstance(value, list):
        raise ValueError("published agents must be a list")
    kept, indices, dropped, seen = [], [], [], set()
    for index, entry in enumerate(value):
        reason, row = None, {}
        if not isinstance(entry, dict):
            reason = "not an object"
        elif set(entry) - set(PUBLISHED_AGENT_FIELDS):
            reason = "unknown field"
        elif "id" not in entry:
            reason = "no id"
        else:
            for key, limit in PUBLISHED_AGENT_FIELDS.items():
                if key in entry:
                    if not published_text(entry[key], limit):
                        reason = "invalid " + key
                        break
                    row[key] = entry[key]
        if reason is None:
            row.setdefault("name", row["id"])
            row.setdefault("status", "queueable")
            if row["status"] not in PUBLISHED_STATUSES:
                reason = "unknown status"
            elif row["id"] in seen:
                reason = "duplicate id"
            elif len(kept) >= MAX_PUBLISHED_AGENTS:
                reason = "over the %d entries" % MAX_PUBLISHED_AGENTS
        if reason is not None:
            dropped.append((index, reason))
            continue
        seen.add(row["id"])
        kept.append(row)
        indices.append(index)
    for row in kept:
        if "parent" in row and (row["parent"] not in seen or row["parent"] == row["id"]):
            del row["parent"]
    while kept and len(json.dumps(kept, separators=(",", ":")).encode("utf-8")) > MAX_PUBLISHED_AGENTS_BYTES:
        kept.pop()
        dropped.append((indices.pop(), "over %d bytes" % MAX_PUBLISHED_AGENTS_BYTES))
    return kept, dropped
