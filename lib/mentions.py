#!/usr/bin/env python3
"""Fan @com8::bus:session mentions out through the existing bus send path.

Plain @name is left for the host. A handler registry makes `com8` the first
magic command; other handlers can be added without claiming every @. Delivery
calls bus.run for command send. This module does not write a pane.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse

# Same ceiling as bus_broker.MAX_MESSAGE. An oversize body is refused whole.
MAX_MESSAGE = 32768
MAX_COMPLETIONS = 50
MAX_PER_PROMPT = 16
MAX_PER_WINDOW = 30
WINDOW_SECONDS = 60

_BUS = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ATTEMPT = re.compile(r"(?<![A-Za-z0-9._@-])@([a-z][a-z0-9_-]{0,31})::([^\s`]*)")
_TRAILING = set("\"')]},.;:!?")
_RECEIPT_KEYS = (
    "id", "status", "target", "expires_at", "conversation_expires_at",
    "correlation_version", "conversation_id", "in_reply_to",
)
_AGENT_KEYS = ("id", "name", "kind", "status", "last_seen")
_SECRET = re.compile(
    r"(commbus1\.[A-Za-z0-9._=\-]+|bearer\s+\S+|token[=:]\S+|/tmp/\S+|socket=|"
    r"device_metadata|credential|invite[=:])",
    re.I,
)


def delivery_label(kind):
    return {
        "claude": "claude-socket",
        "codex": "codex-queue",
        "service": "service-bridge",
    }.get(kind, "bus-send")


def safe_detail(exc):
    text = " ".join(str(exc).split())
    lowered = text.lower()
    if _SECRET.search(text) or any(word in lowered for word in (
            "device_metadata", "socket", "credential", "invite", "commbus1")):
        return "delivery refused"
    return text[:200]


def normalize_hub(hub):
    """Return a public hub, None for the configured hub, or False if it must not be echoed."""
    if hub is None or hub == "":
        return None
    if hub == "local":
        return "local"
    parts = urllib.parse.urlsplit(hub)
    if (parts.username or parts.password or parts.query or parts.fragment
            or parts.scheme != "https" or not parts.hostname):
        return False
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class RateLimiter:
    """Rolling window of delivery timestamps. The file stores stamps and nothing else."""

    def __init__(self, path, *, limit=MAX_PER_WINDOW, window=WINDOW_SECONDS,
                 per_prompt=MAX_PER_PROMPT, now=None):
        self.path = Path(path) if path else None
        self.limit = limit
        self.window = window
        self.per_prompt = per_prompt
        self.now = now or time.time
        self._memory = []

    def check(self, count, *, consume):
        if count <= 0:
            return None
        now = float(self.now())
        stamps = self._read(now)
        if stamps is None:
            return {"code": "rate_limited", "limit": self.limit, "window_seconds": self.window}
        if len(stamps) + count > self.limit:
            return {"code": "rate_limited", "limit": self.limit, "window_seconds": self.window}
        if consume:
            stamps.extend([now] * count)
            self._write(stamps)
        return None

    def _read(self, now):
        if self.path is None:
            stamps = list(self._memory)
        else:
            try:
                raw = self.path.read_text()
            except FileNotFoundError:
                stamps = []
            except OSError:
                return None
            else:
                try:
                    data = json.loads(raw)
                except ValueError:
                    return None
                stamps = data.get("stamps") if isinstance(data, dict) else None
                if (not isinstance(stamps, list)
                        or any(isinstance(item, bool) or not isinstance(item, (int, float))
                               for item in stamps)):
                    return None
        cutoff = now - self.window
        return [item for item in stamps if item > cutoff]

    def _write(self, stamps):
        if self.path is None:
            self._memory = list(stamps)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"stamps": stamps}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)


def default_limiter():
    import bus as bus_client
    return RateLimiter(bus_client.state_dir() / "mentions-rate.json")


def _code_mask(text):
    mask = bytearray(len(text))
    index = 0
    limit = len(text)
    while index < limit:
        if text.startswith("```", index):
            end = text.find("```", index + 3)
            close = limit if end < 0 else end + 3
            mask[index:close] = b"\x01" * (close - index)
            index = close
            continue
        if text[index] == "`":
            end = text.find("`", index + 1)
            if end > index and "\n" not in text[index + 1:end]:
                mask[index:end + 1] = b"\x01" * (end + 1 - index)
                index = end + 1
                continue
        index += 1
    return mask


def _strip_trailing(payload):
    end = len(payload)
    while end > 0 and payload[end - 1] in _TRAILING:
        end -= 1
    return payload[:end]


class Com8Handler:
    name = "com8"

    def parse_payload(self, payload):
        if payload.count(":") != 1:
            return None
        bus, session = payload.split(":", 1)
        if not _BUS.fullmatch(bus) or not _SESSION.fullmatch(session):
            return None
        return bus, session

    def complete(self, argument, registry, lead):
        if ":" not in argument:
            names = sorted(
                name for name in registry.bus_names()
                if isinstance(name, str) and name.startswith(argument) and _BUS.fullmatch(name)
            )
            return [{"text": lead + name + ":", "kind": "bus", "bus": name} for name in names]
        bus, _, session_prefix = argument.partition(":")
        if not _BUS.fullmatch(bus) or bus not in set(registry.bus_names()):
            return []
        rows = []
        for agent in registry.sessions(bus):
            if not isinstance(agent, dict) or agent.get("retired"):
                continue
            if agent.get("status") not in ("live", "queueable"):
                continue
            name = agent.get("name")
            if not isinstance(name, str) or not name.startswith(session_prefix):
                continue
            ident = agent.get("id")
            if not isinstance(ident, str):
                continue
            seen = agent.get("last_seen")
            rows.append({
                "name": name,
                "id": ident,
                "kind": agent.get("kind"),
                "status": agent.get("status"),
                "last_seen": seen if isinstance(seen, (int, float)) and not isinstance(seen, bool) else 0,
            })
        rows.sort(key=lambda row: (-row["last_seen"], row["name"], row["id"]))
        rows = rows[:MAX_COMPLETIONS]
        return [{
            "text": "%s%s:%s" % (lead, bus, row["name"]),
            "kind": "session",
            "bus": bus,
            "name": row["name"],
            "id": row["id"],
            "session_kind": row["kind"],
            "status": row["status"],
            "last_seen": row["last_seen"],
        } for row in rows]


HANDLERS = {Com8Handler.name: Com8Handler()}


def parse(text):
    if not isinstance(text, str):
        text = ""
    mask = _code_mask(text)
    mentions = []
    errors = []
    for match in _ATTEMPT.finditer(text):
        if mask[match.start()]:
            continue
        handler_name = match.group(1)
        handler = HANDLERS.get(handler_name)
        if handler is None:
            continue
        payload = _strip_trailing(match.group(2))
        parsed = handler.parse_payload(payload)
        if parsed is None:
            errors.append({
                "code": "invalid_mention",
                "text": match.group(0)[:180],
                "start": match.start(),
            })
            continue
        bus, session = parsed
        mentions.append({
            "handler": handler_name,
            "bus": bus,
            "session": session,
            "start": match.start(),
            "end": match.start() + len("@%s::%s" % (handler_name, payload)),
        })
    return {"mentions": mentions, "errors": errors}


def project_snapshot(snapshot):
    """Keep the address fields a mention needs. Drop credentials and device detail."""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot")
    buses = {}
    for bus in snapshot.get("buses") or []:
        if not isinstance(bus, dict):
            continue
        name = bus.get("name")
        if not isinstance(name, str) or not _BUS.fullmatch(name):
            continue
        rows = []
        for agent in bus.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            row = {}
            for key in _AGENT_KEYS:
                if key in agent:
                    row[key] = agent[key]
            if isinstance(row.get("name"), str) and isinstance(row.get("id"), str):
                rows.append(row)
        buses[name] = rows
    return buses


class MemoryRegistry:
    def __init__(self, buses):
        self._buses = {}
        for name, rows in (buses or {}).items():
            self._buses[name] = [dict(row) for row in rows]

    def bus_names(self):
        return list(self._buses)

    def sessions(self, bus):
        return [dict(row) for row in self._buses.get(bus, ())]


def registry_from_snapshot(snapshot):
    return MemoryRegistry(project_snapshot(snapshot))


def load_registry(hub=None):
    import bus as bus_client
    conn = bus_client.connection(hub=hub)
    snapshot = bus_client.request(conn, "snapshot")
    return registry_from_snapshot(snapshot)


def _public_agent(bus, row):
    return {
        "ok": True,
        "bus": bus,
        "name": row.get("name"),
        "id": row.get("id"),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "last_seen": row.get("last_seen"),
        "delivery": delivery_label(row.get("kind")),
    }


def resolve(bus, session, registry):
    if bus not in set(registry.bus_names()):
        return {"ok": False, "code": "unknown_bus", "bus": bus, "name": session}
    rows = [row for row in registry.sessions(bus) if isinstance(row, dict) and not row.get("retired")]
    by_id = [row for row in rows if row.get("id") == session]
    if len(by_id) == 1:
        return _public_agent(bus, by_id[0])
    if len(by_id) > 1:
        return _ambiguous(bus, session, by_id)
    by_name = [row for row in rows if row.get("name") == session]
    if not by_name:
        return {"ok": False, "code": "unknown_session", "bus": bus, "name": session}
    if len(by_name) > 1:
        return _ambiguous(bus, session, by_name)
    return _public_agent(bus, by_name[0])


def _ambiguous(bus, session, rows):
    return {
        "ok": False,
        "code": "ambiguous",
        "bus": bus,
        "name": session,
        "candidates": sorted(row.get("id") for row in rows if isinstance(row.get("id"), str)),
    }


def _split_partial(partial):
    best = None
    for marker in ("@com8::", "com8::", "@com8", "com8"):
        index = partial.rfind(marker)
        if index < 0:
            continue
        if best is None or index < best[0] or (index == best[0] and len(marker) > best[1]):
            best = (index, len(marker))
    if best is None:
        return "", partial
    return partial[:best[0]], partial[best[0]:]


def complete(partial, registry):
    partial = partial if isinstance(partial, str) else ""
    prefix, focus = _split_partial(partial)
    lead_at = ""
    body = focus
    if body.startswith("@"):
        lead_at = "@"
        body = body[1:]
    if "::" not in body:
        rows = []
        for name in sorted(HANDLERS):
            if name.startswith(body):
                rows.append({
                    "text": prefix + lead_at + name + "::",
                    "kind": "handler",
                    "handler": name,
                })
        return {"completions": rows}
    handler_name, _, argument = body.partition("::")
    handler = HANDLERS.get(handler_name)
    if handler is None:
        return {"completions": []}
    lead = prefix + lead_at + handler_name + "::"
    return {"completions": handler.complete(argument, registry, lead)}


def message_body(prompt, context):
    prompt = prompt if isinstance(prompt, str) else ""
    if isinstance(context, str) and context.strip():
        return prompt + "\n\n" + context.strip()
    return prompt


def _result(*, dry_run, sender, hub, deliveries, errors, sent, body):
    encoded = body.encode("utf-8") if isinstance(body, str) else b""
    return {
        "ok": not errors,
        "dry_run": bool(dry_run),
        "sent": bool(sent),
        "sender": sender,
        "hub": hub if hub is not False else None,
        "message_bytes": len(encoded),
        "message_sha256": hashlib.sha256(encoded).hexdigest() if body is not None else None,
        "deliveries": deliveries,
        "errors": errors,
    }


def _row(found, *, sent, receipt=None):
    row = {
        "bus": found["bus"],
        "name": found["name"],
        "id": found["id"],
        "kind": found["kind"],
        "status": found["status"],
        "last_seen": found.get("last_seen"),
        "delivery": found["delivery"],
        "sent": sent,
    }
    if receipt is not None:
        row["receipt"] = receipt
    return row


def _receipt(payload):
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in _RECEIPT_KEYS if key in payload}


def route(prompt, registry=None, *, context="", sender="self", hub=None, dry_run=False,
          send=None, limiter=None):
    hub_value = normalize_hub(hub)
    if hub_value is False:
        return _result(
            dry_run=dry_run, sender=sender, hub=None, deliveries=[],
            errors=[{"code": "invalid_hub"}], sent=False, body=None,
        )
    prompt = prompt if isinstance(prompt, str) else ""
    parsed = parse(prompt)
    errors = list(parsed["errors"])
    if not parsed["mentions"]:
        return _result(
            dry_run=dry_run, sender=sender, hub=hub_value, deliveries=[],
            errors=errors, sent=False, body=message_body(prompt, context),
        )
    body = message_body(prompt, context)
    if "\x00" in body or len(body.encode("utf-8")) > MAX_MESSAGE:
        errors.append({"code": "too_large", "limit": MAX_MESSAGE})
        return _result(
            dry_run=dry_run, sender=sender, hub=hub_value, deliveries=[],
            errors=errors, sent=False, body=None,
        )
    if not body.strip():
        errors.append({"code": "empty_message"})
        return _result(
            dry_run=dry_run, sender=sender, hub=hub_value, deliveries=[],
            errors=errors, sent=False, body=body,
        )
    seen = set()
    unique = []
    for mention in parsed["mentions"]:
        key = (mention["bus"], mention["session"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(mention)
    per_prompt = limiter.per_prompt if limiter is not None else MAX_PER_PROMPT
    if len(unique) > per_prompt:
        errors.append({"code": "rate_limited", "scope": "prompt", "limit": per_prompt})
        return _result(
            dry_run=dry_run, sender=sender, hub=hub_value, deliveries=[],
            errors=errors, sent=False, body=body,
        )
    if registry is None:
        try:
            registry = load_registry(hub_value)
        except Exception as exc:
            errors.append({"code": "registry_unavailable", "detail": safe_detail(exc)})
            return _result(
                dry_run=dry_run, sender=sender, hub=hub_value, deliveries=[],
                errors=errors, sent=False, body=body,
            )
    resolved = []
    seen_ids = set()
    for mention in unique:
        found = resolve(mention["bus"], mention["session"], registry)
        if not found.get("ok"):
            error = {key: found[key] for key in ("code", "bus", "name", "candidates") if key in found}
            errors.append(error)
            continue
        if found["id"] in seen_ids:
            continue
        seen_ids.add(found["id"])
        resolved.append(found)
    if not resolved:
        return _result(
            dry_run=dry_run, sender=sender, hub=hub_value, deliveries=[],
            errors=errors, sent=False, body=body,
        )
    if limiter is None:
        limiter = default_limiter()
    limited = limiter.check(len(resolved), consume=not dry_run)
    if limited is not None:
        errors.append(limited)
        return _result(
            dry_run=dry_run, sender=sender, hub=hub_value,
            deliveries=[_row(found, sent=False) for found in resolved],
            errors=errors, sent=False, body=body,
        )
    if dry_run:
        return _result(
            dry_run=True, sender=sender, hub=hub_value,
            deliveries=[_row(found, sent=False) for found in resolved],
            errors=errors, sent=False, body=body,
        )
    sender_fn = send or existing_bus_send
    deliveries = []
    any_sent = False
    for found in resolved:
        try:
            receipt = sender_fn(found["id"], found["bus"], body, sender=sender, hub=hub_value)
        except Exception as exc:
            errors.append({
                "code": "delivery_failed",
                "bus": found["bus"],
                "name": found["name"],
                "detail": safe_detail(exc),
            })
            deliveries.append(_row(found, sent=False))
            continue
        any_sent = True
        deliveries.append(_row(found, sent=True, receipt=_receipt(receipt)))
    return _result(
        dry_run=False, sender=sender, hub=hub_value, deliveries=deliveries,
        errors=errors, sent=any_sent, body=body,
    )


def existing_bus_send(target, bus, message, sender="self", hub=None):
    """Call the same bus.run send path `communicate bus send` uses."""
    import bus as bus_client
    args = argparse.Namespace(
        command="send",
        hub=hub,
        target=target,
        bus=bus,
        sender=sender,
        expected_sender_epoch=None,
        expected_recipient_epoch=None,
        request_id=None,
        message=[message],
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        bus_client.run(args)
    raw = buffer.getvalue().strip()
    try:
        receipt = json.loads(raw) if raw else {}
    except ValueError:
        raise RuntimeError("delivery refused")
    return _receipt(receipt)


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, out=None, err=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._out = out
        self._err = err

    def print_help(self, file=None):
        super().print_help(file if file is not None else (self._out or sys.stdout))

    def exit(self, status=0, message=None):
        if message:
            stream = (self._err or sys.stderr) if status else (self._out or sys.stdout)
            print(message, file=stream)
        raise SystemExit(status)


def _read_text(stream, limit):
    return stream.read(limit + 1)


def main(argv=None, registry=None, send=None, limiter=None, stdin=None, stdout=None, stderr=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = stdout or sys.stdout
    parser = _Parser(
        prog="communicate mentions",
        out=stdout,
        err=stderr,
        description="Fan @com8::bus:session mentions out through bus send.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    route_parser = commands.add_parser("route", help="read a prompt on stdin and fan out mentions")
    route_parser.add_argument("--dry-run", action="store_true", help="show the plan and do not send")
    route_parser.add_argument("--json", action="store_true", help="print JSON (this command always does)")
    route_parser.add_argument("--from", dest="sender", default="self", help="bus sender, default self")
    route_parser.add_argument("--hub", default=None, help="configured hub override, as with bus send")
    route_parser.add_argument("--context", default="", help="extra context appended to the prompt")
    route_parser.add_argument("--context-file", default=None, help="read context from a file")
    route_parser.add_argument("--prompt-file", default=None, help="read the prompt from a file instead of stdin")
    complete_parser = commands.add_parser("complete", help="suggest handlers, buses, and live sessions")
    complete_parser.add_argument("partial", nargs="?", default="")
    complete_parser.add_argument("--text", action="store_true", help="print one insertion per line")
    complete_parser.add_argument("--hub", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code else 0
    if args.command == "complete":
        if registry is None:
            try:
                registry = load_registry(args.hub)
            except Exception as exc:
                print(json.dumps({"completions": [], "errors": [{
                    "code": "registry_unavailable",
                    "detail": safe_detail(exc),
                }]}) , file=stdout)
                return 1
        found = complete(args.partial, registry)
        if args.text:
            for row in found["completions"]:
                print(row["text"], file=stdout)
            return 0
        print(json.dumps(found), file=stdout)
        return 0
    try:
        if args.prompt_file:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")[:MAX_MESSAGE + 1]
        else:
            prompt = _read_text(stdin if stdin is not None else sys.stdin, MAX_MESSAGE)
        context = args.context
        if args.context_file:
            context = Path(args.context_file).read_text(encoding="utf-8")[:MAX_MESSAGE + 1]
    except OSError:
        print(json.dumps({"ok": False, "errors": [{"code": "unreadable_input"}]}), file=stdout)
        return 1
    result = route(
        prompt, registry=registry, context=context, sender=args.sender, hub=args.hub,
        dry_run=args.dry_run, send=send, limiter=limiter,
    )
    print(json.dumps(result), file=stdout)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
