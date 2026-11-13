#!/usr/bin/env python3
"""Reply obligations: correlation, turn-end delivery, completion webhooks.

A delegated task is done only when the reply is posted to the address that
asked for it. The turn-end hook does that posting when the worker's own pane
would otherwise keep the answer. Existing clients that ignore the trailer
behave as they do today.

State stays under the COM8 state directory. Webhook URLs are operator
configuration, never message content. Targets are expected on a tailnet or
behind Cloudflare; this module does not listen.
"""
import argparse
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True

PREFIX = "[com8-obligation] "
OPEN = ("accepted", "in_progress")
TERMINAL = ("done", "failed", "expired")
EVENTS = (
    "obligation.accepted",
    "obligation.in_progress",
    "obligation.done",
    "obligation.failed",
    "obligation.expired",
    "session.reply",
)
DEFAULT_EVENTS = (
    "obligation.done",
    "obligation.failed",
    "obligation.expired",
    "session.reply",
)
EVENT_ALIAS = {
    "accepted": "obligation.accepted",
    "progress": "obligation.in_progress",
    "done": "obligation.done",
    "failed": "obligation.failed",
    "expired": "obligation.expired",
    "reply": "session.reply",
}
HOOK_KIND = {
    "UserPromptSubmit": "prompt",
    "beforeSubmitPrompt": "prompt",
    "PostToolUse": "tool",
    "postToolUse": "tool",
    "Stop": "stop",
    "stop": "stop",
    "StopFailure": "failure",
    "afterAgentResponse": "response",
}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_COM8_TO = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,63}@[a-z0-9][a-z0-9._-]{0,63}~[0-9a-f]{8,64}$")
_BUS_ID = re.compile(r"^(?:m|hm)_[0-9a-f]{32}$")
_MSG_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ReplyError(Exception):
    pass


def state_dir():
    override = os.environ.get("COM8_REPLY_STATE")
    if override:
        return override
    base = os.environ.get("COMM_STATE")
    if not base:
        base = os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
            "communicate")
    return os.path.join(base, "com8", "reply")


def policy_name():
    value = os.environ.get("COM8_REPLY_POLICY", "auto").strip().lower()
    return value if value in ("auto", "remind", "off") else "auto"


def hook_command():
    binary = os.environ.get("COM8_BIN")
    if not binary:
        binary = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bin", "com8")
    return shlex.quote(binary) + " hook agent"


def _empty():
    return {"obligations": {}, "session_open": {}, "deliveries": {},
            "outbox": [], "webhooks": []}


def _atomic_json(path, value):
    blob = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _check_url(url, *, https_only):
    if not isinstance(url, str) or any(c.isspace() for c in url) or len(url) > 512:
        raise ReplyError("invalid URL")
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ReplyError("invalid URL") from exc
    if parts.username or parts.password or parts.query or parts.fragment or not host:
        raise ReplyError("URL must not include credentials, a query, or a fragment")
    loopback = host.lower() in ("localhost", "127.0.0.1", "::1")
    if https_only:
        if parts.scheme != "https" or loopback:
            raise ReplyError("webhook URL must be https and not loopback")
    elif parts.scheme != "https" and not (parts.scheme == "http" and host in ("127.0.0.1", "::1")):
        raise ReplyError("hub URL must be https, or http on literal loopback")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_loopback or ip.is_link_local or ip.is_multicast
               or ip.is_unspecified or ip.is_reserved):
        if not (not https_only and ip.is_loopback):
            raise ReplyError("URL host is not allowed")
    if port is not None and not 0 < port < 65536:
        raise ReplyError("invalid URL port")
    return url.rstrip("/") if not parts.path else url


def validate_webhook_url(url):
    return _check_url(url, https_only=True)


def _hub_ok(url):
    if not url:
        return ""
    return _check_url(url, https_only=False)


def _reply_to_ok(transport, reply_to):
    if transport == "com8":
        return bool(_COM8_TO.fullmatch(reply_to or ""))
    if transport == "bus":
        return bool(_BUS_ID.fullmatch(reply_to or ""))
    if transport == "native":
        if not isinstance(reply_to, str) or len(reply_to) > 256:
            return False
        if not reply_to.startswith("/") or not reply_to.endswith(".sock"):
            return False
        if any(c in reply_to for c in "\n\r\x00 ") or ".." in reply_to.split("/"):
            return False
        return True
    return False


def validate_marker(obj):
    if not isinstance(obj, dict):
        return None
    expect = obj.get("expect")
    transport = obj.get("transport")
    ident = obj.get("id")
    if obj.get("v") != 1 or expect not in ("reply", "none"):
        return None
    if transport not in ("com8", "bus", "native") or not isinstance(ident, str):
        return None
    if not _ID.fullmatch(ident):
        return None
    reply_to = obj.get("reply_to") if isinstance(obj.get("reply_to"), str) else ""
    reply_as = obj.get("reply_as") if isinstance(obj.get("reply_as"), str) else ""
    if expect == "reply" and not _reply_to_ok(transport, reply_to):
        return None
    if reply_as and not _NAME.fullmatch(reply_as):
        return None
    if expect == "reply" and transport in ("com8", "bus") and not reply_as:
        return None
    hub = obj.get("hub") or ""
    bus = obj.get("bus") or ""
    try:
        if hub:
            hub = _hub_ok(hub)
    except ReplyError:
        return None
    if bus and not _NAME.fullmatch(bus):
        return None
    ttl = obj.get("ttl", 3600 if transport == "bus" else 1800)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        return None
    ttl = int(ttl)
    if not 1 <= ttl <= 86400:
        return None
    return {"v": 1, "id": ident, "transport": transport, "expect": expect,
            "reply_to": reply_to, "reply_as": reply_as or "turn-end",
            "hub": hub or None, "bus": bus or None, "ttl": ttl}


def marker_line(**fields):
    marker = validate_marker(dict(fields, v=1))
    if not marker:
        raise ReplyError("invalid marker")
    return PREFIX + json.dumps(marker, sort_keys=True, separators=(",", ":"))


def last_marker(text):
    if not isinstance(text, str) or PREFIX not in text:
        return None
    source = text[-65536:]
    found = None
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith(PREFIX):
            continue
        try:
            obj = json.loads(stripped[len(PREFIX):])
        except (ValueError, TypeError):
            continue
        parsed = validate_marker(obj)
        if parsed:
            found = parsed
    return found


def strip_markers(text):
    if not isinstance(text, str):
        return ""
    kept = [line for line in text.splitlines() if not line.strip().startswith(PREFIX)]
    return "\n".join(kept).strip()


def ask_wrapping(text, token, reply_as, ttl):
    ttl = int(float(ttl))
    ttl = 1 if ttl < 1 else min(ttl, 86400)
    corr = token.rsplit("~", 1)[-1]
    line = marker_line(id=corr, transport="com8", expect="reply", reply_to=token,
                       reply_as=reply_as, ttl=ttl)
    instruction = ('[reply with: communicate com8 reply %s "<answer>" --from %s]'
                   % (token, reply_as))
    return "%s\n\n%s\n%s" % (text, instruction, line)


def rewrite_fleet_tokens(text, fleet):
    """Point ask tokens at the fleet petname the receiving device can route."""
    if not isinstance(text, str) or not isinstance(fleet, str) or not fleet:
        return text

    def prose(match):
        return "%s@%s~%s" % (match.group(1), fleet, match.group(2))

    text = re.sub(
        r"(reply\s+[a-z0-9][a-z0-9._-]*)@[a-z0-9._-]+~([0-9a-f]+)", prose, text)
    if not _NAME.fullmatch(fleet):
        return text

    def boxed(match):
        return '"reply_to":"%s@%s~%s"' % (match.group(1), fleet, match.group(2))

    return re.sub(
        r'"reply_to":"([a-z0-9][a-z0-9._-]*)@[a-z0-9._-]+~([0-9a-f]+)"',
        boxed, text)


def bus_trailer(record, envelope):
    """Append a trailer only when the broker authored correlation v1."""
    try:
        if not isinstance(envelope, dict) or envelope.get("correlation_version") != 1:
            return ""
        mid = envelope.get("id") or ""
        if not _BUS_ID.fullmatch(mid):
            return ""
        reply_as = (record or {}).get("id") or ""
        hub = (record or {}).get("url") or ""
        bus_name = envelope.get("bus") or "general"
        parent = envelope.get("in_reply_to")
        if isinstance(parent, str) and parent:
            ident = parent if _ID.fullmatch(parent) else mid
            return marker_line(id=ident, transport="bus", expect="none",
                               reply_to=mid, reply_as=reply_as, hub=hub, bus=bus_name)
        ttl = 3600
        expires = envelope.get("conversation_expires_at")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool):
            remaining = int(expires - time.time())
            if remaining > 0:
                ttl = max(60, min(86400, remaining))
        reply_to = envelope.get("reply_to") or mid
        return marker_line(id=mid, transport="bus", expect="reply", reply_to=reply_to,
                           reply_as=reply_as, hub=hub, bus=bus_name, ttl=ttl)
    except (ReplyError, TypeError, ValueError):
        return ""


def delivery_id(event, obligation_id):
    return hashlib.sha256(("%s\n%s" % (event, obligation_id)).encode("utf-8")).hexdigest()


def sign_body(secret, timestamp, body):
    digest = hmac.new(secret, timestamp.encode("utf-8") + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def read_secret(path):
    if os.path.islink(path):
        raise ReplyError("webhook secret must be a regular file")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ReplyError("webhook secret is unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ReplyError("webhook secret must be mode 0600 and owned by this user")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ReplyError("webhook secret must be mode 0600 and owned by this user")
    with open(path, "rb") as handle:
        data = handle.read(4097)
    data = data.strip()
    if not 16 <= len(data) <= 4096 or b"\x00" in data:
        raise ReplyError("webhook secret length is invalid")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_post(url, body, headers, timeout):
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=timeout) as response:
            code = getattr(response, "status", 200)
            response.read(1024)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return False, "redirect refused"
        return False, "http %s" % exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "unreachable"
    if 200 <= code < 300:
        return True, "ok"
    return False, "http %s" % code


class Ledger:
    def __init__(self, root=None, post=None, clock=None):
        self.root = root or state_dir()
        self.post = post or default_post
        self.clock = clock or time.time

    @contextlib.contextmanager
    def locked(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        fd = os.open(os.path.join(self.root, "ledger.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _path(self):
        return os.path.join(self.root, "ledger.json")

    def _load(self):
        try:
            with open(self._path(), encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return _empty()
        except (OSError, ValueError) as exc:
            raise ReplyError("reply ledger is unreadable") from exc
        if not isinstance(data, dict):
            raise ReplyError("reply ledger is unreadable")
        base = _empty()
        for key in base:
            if isinstance(data.get(key), type(base[key])):
                base[key] = data[key]
        return base

    def _save(self, data):
        _atomic_json(self._path(), data)

    def _queue(self, data, event, obligation_id, fields, now):
        targets = [item for item in data["webhooks"]
                   if event in item.get("events", [])]
        if not targets:
            return
        did = delivery_id(event, obligation_id)
        if did in data["deliveries"]:
            return
        body = json.dumps(dict(fields, event=event, delivery_id=did,
                               obligation_id=obligation_id, at=now),
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        data["deliveries"][did] = {"status": "queued", "at": now}
        for item in targets:
            data["outbox"].append({
                "id": did, "event": event, "url": item["url"],
                "secret_file": item["secret_file"], "body": body.decode("utf-8"),
                "attempts": 0, "next_at": now})
        if len(data["outbox"]) > 100:
            data["outbox"] = data["outbox"][-100:]

    def emit(self, event, obligation_id, fields, now=None):
        now = self.clock() if now is None else now
        with self.locked():
            data = self._load()
            self._queue(data, event, obligation_id, fields, now)
            self._save(data)
        return self.dispatch(now)

    def dispatch(self, now=None, budget=2.0):
        now = self.clock() if now is None else now
        sent = failed = 0
        started = time.monotonic()
        while time.monotonic() - started < budget:
            with self.locked():
                data = self._load()
                index = next((i for i, item in enumerate(data["outbox"])
                              if item.get("next_at", 0) <= now), None)
                if index is None:
                    break
                item = data["outbox"].pop(index)
                self._save(data)
            try:
                secret = read_secret(item["secret_file"])
                stamp = str(int(now))
                body = item["body"].encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "com8-reply",
                    "X-Com8-Event": item.get("event") or "",
                    "X-Com8-Delivery": item["id"],
                    "X-Com8-Timestamp": stamp,
                    "X-Com8-Signature": sign_body(secret, stamp, body),
                }
                ok, detail = self.post(item["url"], body, headers, timeout=2)
            except Exception:
                ok, detail = False, "delivery failed"
            if ok:
                sent += 1
                continue
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = detail
            if item["attempts"] >= 5:
                failed += 1
                continue
            item["next_at"] = now + (2 ** (item["attempts"] - 1))
            with self.locked():
                data = self._load()
                data["outbox"].append(item)
                self._save(data)
        return {"sent": sent, "deferred": failed}

    def open_obligation(self, marker, session, now):
        if marker.get("expect") != "reply":
            return None
        with self.locked():
            data = self._load()
            current = data["obligations"].get(marker["id"])
            if current and current.get("status") in TERMINAL:
                self._save(data)
                return current
            previous = data["session_open"].get(session) if session else None
            if previous and previous != marker["id"]:
                old = data["obligations"].get(previous)
                if old and old.get("status") in OPEN:
                    old.update(status="expired", reason="superseded", updated_at=now)
                    data["session_open"].pop(session, None)
                    self._queue(data, "obligation.expired", old["id"], {
                        "status": "expired", "reason": "superseded",
                        "transport": old.get("transport")}, now)
            if not current:
                current = {
                    "id": marker["id"], "transport": marker["transport"],
                    "reply_to": marker["reply_to"], "reply_as": marker["reply_as"],
                    "hub": marker.get("hub"), "bus": marker.get("bus"),
                    "session_id": session, "status": "accepted",
                    "created_at": now, "updated_at": now,
                    "deadline": now + marker["ttl"],
                    "reply_msg_id": "obl-" + marker["id"],
                    "reminded": False, "via": None, "reason": None,
                }
                data["obligations"][marker["id"]] = current
                self._queue(data, "obligation.accepted", marker["id"], {
                    "status": "accepted", "transport": marker["transport"],
                    "reply_to": marker["reply_to"]}, now)
            else:
                current["session_id"] = session or current.get("session_id")
                current["updated_at"] = now
            if session and current.get("status") in OPEN + ("sending",):
                data["session_open"][session] = marker["id"]
            self._save(data)
            return dict(current)

    def mark(self, obligation_id, status, now, via=None, reason=None, text=None):
        with self.locked():
            data = self._load()
            row = data["obligations"].get(obligation_id)
            if not row or row.get("status") in TERMINAL:
                return row
            row.update(status=status, updated_at=now, via=via or row.get("via"),
                       reason=reason)
            session = row.get("session_id")
            if session and data["session_open"].get(session) == obligation_id:
                data["session_open"].pop(session, None)
            event = {"done": "obligation.done", "failed": "obligation.failed",
                     "expired": "obligation.expired"}.get(status)
            if event:
                fields = {"status": status, "transport": row.get("transport"),
                          "reply_to": row.get("reply_to"), "via": row.get("via"),
                          "reason": reason}
                if text:
                    fields["text"] = text[:32000]
                self._queue(data, event, obligation_id, fields, now)
            self._save(data)
            return dict(row)

    def note_explicit(self, command, session, now):
        if not session or not isinstance(command, str):
            return None
        blob = command[:65536]
        if not re.search(r"\b(com8|communicate)\b", blob):
            return None
        if not re.search(r"\b(reply|send)\b", blob):
            return None
        with self.locked():
            data = self._load()
            ident = data["session_open"].get(session)
            row = data["obligations"].get(ident) if ident else None
            if not row or row.get("status") not in OPEN:
                return None
            if row["id"] not in blob and (row.get("reply_to") or "") not in blob:
                return None
            self._save(data)
        return self.mark(row["id"], "done", now, via="explicit")

    def progress(self, session, now):
        with self.locked():
            data = self._load()
            ident = data["session_open"].get(session) if session else None
            row = data["obligations"].get(ident) if ident else None
            if not row or row.get("status") != "accepted":
                return None
            row.update(status="in_progress", updated_at=now)
            self._queue(data, "obligation.in_progress", row["id"], {
                "status": "in_progress", "transport": row.get("transport")}, now)
            self._save(data)
            return dict(row)

    def claim_open(self, session, now):
        """Return the session's open obligation and mark it sending, or None."""
        with self.locked():
            data = self._load()
            ident = data["session_open"].get(session) if session else None
            row = data["obligations"].get(ident) if ident else None
            if not row:
                return None
            if row.get("status") == "sending" and now - row.get("updated_at", now) < 30:
                return {"busy": True, "row": dict(row)}
            if row.get("status") == "sending":
                row["status"] = "accepted"
            if row.get("status") not in OPEN:
                return None
            if row.get("deadline", now) <= now:
                row.update(status="expired", reason="deadline", updated_at=now)
                data["session_open"].pop(session, None)
                self._queue(data, "obligation.expired", row["id"], {
                    "status": "expired", "reason": "deadline",
                    "transport": row.get("transport")}, now)
                self._save(data)
                return {"expired": True, "row": dict(row)}
            row.update(status="sending", updated_at=now)
            self._save(data)
            return {"row": dict(row)}

    def release_sending(self, obligation_id, now):
        with self.locked():
            data = self._load()
            row = data["obligations"].get(obligation_id)
            if row and row.get("status") == "sending":
                row.update(status="in_progress", updated_at=now)
                self._save(data)

    def remember_reminded(self, obligation_id, now):
        with self.locked():
            data = self._load()
            row = data["obligations"].get(obligation_id)
            if row and row.get("status") in OPEN:
                row.update(reminded=True, updated_at=now)
                self._save(data)

    def list_obligations(self):
        with self.locked():
            data = self._load()
        rows = []
        for row in data["obligations"].values():
            rows.append({key: row.get(key) for key in (
                "id", "status", "transport", "reply_to", "reply_as", "session_id",
                "created_at", "updated_at", "deadline", "via", "reason")})
        rows.sort(key=lambda item: item.get("created_at") or 0)
        return rows

    def webhook_add(self, url, secret_file, events, now):
        url = validate_webhook_url(url)
        read_secret(secret_file)
        events = tuple(events)
        with self.locked():
            data = self._load()
            for item in data["webhooks"]:
                if (item["url"] == url and item["secret_file"] == secret_file
                        and tuple(item["events"]) == events):
                    return dict(item)
            item = {                    "id": "wh_" + hashlib.sha256(
                        ("%s\n%s\n%s" % (url, secret_file, now)).encode()).hexdigest()[:12],
                    "url": url, "secret_file": secret_file,
                    "events": list(events), "created_at": now}
            data["webhooks"].append(item)
            self._save(data)
            return dict(item)

    def webhook_remove(self, ident):
        with self.locked():
            data = self._load()
            before = len(data["webhooks"])
            data["webhooks"] = [item for item in data["webhooks"] if item.get("id") != ident]
            self._save(data)
            return before != len(data["webhooks"])

    def webhooks(self):
        with self.locked():
            data = self._load()
        return [dict(item) for item in data["webhooks"]]

    def sweep(self, now=None):
        now = self.clock() if now is None else now
        expired = []
        with self.locked():
            data = self._load()
            for row in list(data["obligations"].values()):
                if row.get("status") == "sending" and now - row.get("updated_at", now) > 30:
                    row.update(status="in_progress", updated_at=now)
                if row.get("status") in OPEN and row.get("deadline", now) <= now:
                    row.update(status="expired", reason="deadline", updated_at=now)
                    session = row.get("session_id")
                    if session and data["session_open"].get(session) == row["id"]:
                        data["session_open"].pop(session, None)
                    self._queue(data, "obligation.expired", row["id"], {
                        "status": "expired", "reason": "deadline",
                        "transport": row.get("transport")}, now)
                    expired.append(row["id"])
            self._save(data)
        report = self.dispatch(now, budget=5)
        report["expired"] = expired
        return report


def note_reply_landed(corr, text, frm, msg_id=None):
    """The daemon calls this when a correlated reply is stored. Failures stay local."""
    if not isinstance(corr, str) or not _ID.fullmatch(corr):
        return
    fields = {"status": "done", "via": "stored", "from": frm or ""}
    if isinstance(text, str) and text:
        fields["text"] = text[:32000]
    if msg_id:
        fields["reply_msg_id"] = msg_id
    try:
        Ledger().emit("obligation.done", corr, fields)
    except Exception:
        return


def session_of(payload):
    for key in ("session_id", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value and len(value) <= 200:
            return value
    return ""


def _cache_path(root, session):
    name = hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
    return os.path.join(root, "turns", name)


def cache_text(root, session, text):
    if not session or not isinstance(text, str) or not text.strip():
        return
    directory = os.path.join(root, "turns")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = _cache_path(root, session)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text[:32000])
    os.replace(path + ".tmp", path)


def cached_text(root, session):
    if not session:
        return ""
    try:
        with open(_cache_path(root, session), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def final_text(payload, root):
    text = payload.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text
    cached = cached_text(root, session_of(payload))
    if cached.strip():
        return cached
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def already_continued(payload):
    if payload.get("stop_hook_active"):
        return True
    try:
        return int(payload.get("loop_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def background_pending(payload):
    tasks = payload.get("background_tasks")
    return isinstance(tasks, list) and len(tasks) > 0


def continue_turn(payload, reason):
    if payload.get("hook_event_name") in ("stop", "subagentStop"):
        return {"followup_message": reason}
    return {"decision": "block", "reason": reason}


def reply_body(row, text):
    clean = strip_markers(text)
    if not clean:
        return ""
    if len(clean) > 32000:
        clean = clean[:32000] + "\n[truncated]"
    trailer = marker_line(id=row["id"], transport=row["transport"], expect="none",
                          reply_to=row.get("reply_to") or row["id"],
                          reply_as=row.get("reply_as") or "turn-end",
                          hub=row.get("hub"), bus=row.get("bus"))
    return "[com8 turn-end reply %s]\n%s\n%s" % (row["id"], clean, trailer)


def default_sender(row, text):
    here = os.path.dirname(os.path.abspath(__file__))
    transport = row.get("transport")
    if transport == "com8":
        cmd = [sys.executable, "-B", os.path.join(here, "com8.py"), "call", "reply",
               row["reply_to"], "--from", row.get("reply_as") or "turn-end",
               "--msg-id", row["reply_msg_id"], "--stdin"]
        proc = subprocess.run(cmd, input=text.encode("utf-8"),
                              capture_output=True, timeout=20)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            return {"ok": False, "err": (err[-1] if err else "reply failed")[:200]}
        return {"ok": True}
    if transport == "bus":
        cmd = [sys.executable, "-B", os.path.join(here, "bus.py")]
        if row.get("hub"):
            cmd.extend(["--hub", row["hub"]])
        cmd.extend(["reply", row["reply_to"], "--from", row.get("reply_as") or "self",
                    "--request-id", row["reply_msg_id"], "--stdin"])
        proc = subprocess.run(cmd, input=text.encode("utf-8"),
                              capture_output=True, timeout=25)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            return {"ok": False, "err": (err[-1] if err else "bus reply failed")[:200]}
        return {"ok": True}
    if transport == "native":
        import cc_peer
        try:
            cc_peer.deliver(row["reply_to"], text, row["reply_to"], from_name="turn-end")
        except OSError:
            return {"ok": False, "err": "native reply socket unavailable"}
        return {"ok": True}
    return {"ok": False, "err": "unknown reply transport"}


def _tool_blob(payload):
    tool = payload.get("tool_input")
    if isinstance(tool, str):
        return tool[:65536]
    if isinstance(tool, dict):
        try:
            return json.dumps(tool)[:65536]
        except (TypeError, ValueError):
            return ""
    return ""


def handle(payload, ledger=None, sender=None, now=None, policy=None):
    """One coding-agent hook invocation. Always returns a JSON-serializable dict."""
    if not isinstance(payload, dict):
        return {}
    ledger = ledger or Ledger()
    sender = sender or default_sender
    now = time.time() if now is None else now
    policy = policy or policy_name()
    kind = HOOK_KIND.get(str(payload.get("hook_event_name") or ""))
    session = session_of(payload)
    try:
        ledger.sweep(now) if kind == "stop" else None
        if kind == "response":
            cache_text(ledger.root, session, payload.get("text") if isinstance(
                payload.get("text"), str) else "")
            return {}
        if kind == "prompt":
            marker = last_marker(payload.get("prompt") or "")
            if not marker:
                return {}
            if marker["expect"] == "none":
                ledger.emit("session.reply", marker["id"], {
                    "status": "received", "transport": marker["transport"],
                    "text": strip_markers(payload.get("prompt") or "")[:8000]}, now)
                return {}
            ledger.open_obligation(marker, session, now)
            ledger.dispatch(now, budget=1)
            return {}
        if kind == "tool":
            if session:
                ledger.progress(session, now)
                ledger.note_explicit(_tool_blob(payload), session, now)
            return {}
        if kind == "failure":
            claimed = ledger.claim_open(session, now) if session else None
            row = (claimed or {}).get("row")
            if row and row.get("status") not in TERMINAL and not claimed.get("expired"):
                if claimed.get("busy"):
                    return {}
                ledger.mark(row["id"], "failed", now, via="stop-failure",
                            reason=str(payload.get("error") or "stop-failure")[:80])
            return {}
        if kind != "stop" or not session:
            return {}
        status = payload.get("status")
        if status == "aborted":
            return {}
        claimed = ledger.claim_open(session, now)
        if not claimed or claimed.get("expired"):
            return {}
        if claimed.get("busy"):
            return {}
        row = claimed["row"]
        if status == "error":
            ledger.mark(row["id"], "failed", now, via="stop", reason="error")
            return {}
        if policy == "off" or background_pending(payload):
            ledger.release_sending(row["id"], now)
            return {}
        reason = ("This session still owes a COM8 reply for %s. "
                  "Answer through the reply instruction in the request. "
                  "Answering only in this pane does not reach the requester."
                  % row["id"])
        if policy == "remind":
            if row.get("reminded") or already_continued(payload):
                ledger.release_sending(row["id"], now)
                return {}
            ledger.release_sending(row["id"], now)
            ledger.remember_reminded(row["id"], now)
            return continue_turn(payload, reason)
        text = reply_body(row, final_text(payload, ledger.root))
        if not text:
            if already_continued(payload):
                ledger.mark(row["id"], "failed", now, via="turn-end", reason="empty")
                return {}
            ledger.release_sending(row["id"], now)
            ledger.remember_reminded(row["id"], now)
            return continue_turn(payload, reason)
        try:
            result = sender(row, text)
        except Exception:
            result = {"ok": False, "err": "send failed"}
        if result.get("ok"):
            ledger.mark(row["id"], "done", now, via="turn-end", text=text)
            ledger.dispatch(now, budget=2)
            return {}
        if already_continued(payload):
            ledger.mark(row["id"], "failed", now, via="turn-end",
                        reason=str(result.get("err") or "send failed")[:200])
            return {}
        ledger.release_sending(row["id"], now)
        return continue_turn(payload, reason + " Delivery failed: " +
                             str(result.get("err") or "send failed")[:200])
    except ReplyError:
        return {}


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    if os.path.islink(path) or not os.path.isfile(path):
        raise ReplyError("refusing to edit %s" % os.path.basename(path))
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ReplyError("settings file is not valid JSON") from exc
    return value


def _write_json(path, value):
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.islink(path):
        raise ReplyError("refusing to edit a symlink")
    if os.path.exists(path):
        backup = path + ".com8-reply-backup"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
    _atomic_json(path, value)


def _command_present(rows, command, nested):
    for row in rows or []:
        hooks = row.get("hooks") if nested else [row]
        for hook in hooks or []:
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


def _append_group(hooks, event, command, matcher=None, timeout=20):
    rows = hooks.setdefault(event, [])
    if not isinstance(rows, list):
        raise ReplyError("hook settings for %s are not a list" % event)
    if _command_present(rows, command, nested=True):
        return False
    entry = {"type": "command", "command": command, "timeout": timeout}
    group = {"hooks": [entry]}
    if matcher:
        group["matcher"] = matcher
    rows.append(group)
    return True


def agent_paths(home=None):
    if home:
        base = home
        claude = os.path.join(base, ".claude")
        codex = os.path.join(base, ".codex")
        cursor = os.path.join(base, ".cursor")
    else:
        base = os.path.expanduser("~")
        claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(base, ".claude")
        codex = os.environ.get("CODEX_HOME") or os.path.join(base, ".codex")
        cursor = os.path.join(base, ".cursor")
    return {"claude": os.path.join(claude, "settings.json"),
            "codex_hooks": os.path.join(codex, "hooks.json"),
            "codex_config": os.path.join(codex, "config.toml"),
            "cursor": os.path.join(cursor, "hooks.json")}


def install_hooks(agent, home=None, command=None, dry_run=False):
    command = command or hook_command()
    paths = agent_paths(home)
    if agent == "claude":
        path = paths["claude"]
        data = _read_json(path, {})
        if not isinstance(data, dict):
            raise ReplyError("Claude settings must be a JSON object")
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ReplyError("Claude hooks must be an object")
        changed = False
        changed |= _append_group(hooks, "UserPromptSubmit", command, timeout=15)
        changed |= _append_group(hooks, "PostToolUse", command, matcher="Bash|Shell", timeout=15)
        changed |= _append_group(hooks, "Stop", command, timeout=20)
        changed |= _append_group(hooks, "StopFailure", command, timeout=10)
        if changed and not dry_run:
            _write_json(path, data)
        return {"ok": True, "path": path, "changed": changed, "agent": agent}
    if agent == "codex":
        config = paths["codex_config"]
        if os.path.isfile(config) and not os.path.islink(config):
            with open(config, encoding="utf-8") as handle:
                text = handle.read()
            if re.search(r"(?m)^\[\[hooks\.|^\[hooks\]", text):
                raise ReplyError(
                    "Codex already has inline hooks in config.toml; add one "
                    "command per event there instead of hooks.json")
        path = paths["codex_hooks"]
        data = _read_json(path, {"hooks": {}})
        if not isinstance(data, dict):
            raise ReplyError("Codex hooks.json must be a JSON object")
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ReplyError("Codex hooks must be an object")
        changed = False
        for event, timeout in (("UserPromptSubmit", 15), ("Stop", 20), ("StopFailure", 10)):
            changed |= _append_group(hooks, event, command, timeout=timeout)
        changed |= _append_group(hooks, "PostToolUse", command, matcher="Bash|Shell", timeout=15)
        if changed and not dry_run:
            _write_json(path, data)
        return {"ok": True, "path": path, "changed": changed, "agent": agent}
    if agent == "cursor":
        path = paths["cursor"]
        data = _read_json(path, {"version": 1, "hooks": {}})
        if not isinstance(data, dict) or data.get("version") not in (None, 1):
            raise ReplyError("Cursor hooks.json version is unsupported")
        data["version"] = 1
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ReplyError("Cursor hooks must be an object")
        changed = False
        for event in ("beforeSubmitPrompt", "afterAgentResponse", "stop", "postToolUse"):
            rows = hooks.setdefault(event, [])
            if not isinstance(rows, list):
                raise ReplyError("Cursor hook settings are not a list")
            if _command_present(rows, command, nested=False):
                continue
            entry = {"command": command}
            if event == "postToolUse":
                entry["matcher"] = "Shell"
            rows.append(entry)
            changed = True
        if changed and not dry_run:
            _write_json(path, data)
        return {"ok": True, "path": path, "changed": changed, "agent": agent}
    raise ReplyError("unknown agent")


def _drop_command(rows, command, nested):
    kept = []
    changed = False
    for row in rows or []:
        if nested:
            hooks = [hook for hook in (row.get("hooks") or [])
                     if not (isinstance(hook, dict) and hook.get("command") == command)]
            if len(hooks) != len(row.get("hooks") or []):
                changed = True
            if hooks:
                row = dict(row, hooks=hooks)
                kept.append(row)
            else:
                changed = True
        elif not (isinstance(row, dict) and row.get("command") == command):
            kept.append(row)
        else:
            changed = True
    return kept, changed


def uninstall_hooks(agent, home=None, command=None):
    command = command or hook_command()
    paths = agent_paths(home)
    if agent == "claude":
        path = paths["claude"]
        key_nested = True
        events = ("UserPromptSubmit", "PostToolUse", "Stop", "StopFailure")
        data = _read_json(path, None)
    elif agent == "codex":
        path = paths["codex_hooks"]
        key_nested = True
        events = ("UserPromptSubmit", "PostToolUse", "Stop", "StopFailure")
        data = _read_json(path, None)
    elif agent == "cursor":
        path = paths["cursor"]
        key_nested = False
        events = ("beforeSubmitPrompt", "afterAgentResponse", "stop", "postToolUse")
        data = _read_json(path, None)
    else:
        raise ReplyError("unknown agent")
    if not isinstance(data, dict):
        return {"ok": True, "path": path, "changed": False, "agent": agent}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {"ok": True, "path": path, "changed": False, "agent": agent}
    changed = False
    for event in events:
        rows, did = _drop_command(hooks.get(event) or [], command, key_nested)
        if did:
            changed = True
            if rows:
                hooks[event] = rows
            else:
                hooks.pop(event, None)
    if changed:
        _write_json(path, data)
    return {"ok": True, "path": path, "changed": changed, "agent": agent}


def _events_from(value):
    if not value:
        return list(DEFAULT_EVENTS)
    found = []
    for part in value.split(","):
        part = part.strip()
        name = EVENT_ALIAS.get(part, part)
        if name not in EVENTS:
            raise ReplyError("unknown webhook event")
        if name not in found:
            found.append(name)
    return found


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="com8", description="COM8 reply obligations")
    commands = parser.add_subparsers(dest="cmd")

    hook = commands.add_parser("hook")
    hook_commands = hook.add_subparsers(dest="hook_cmd")
    hook_commands.add_parser("agent")
    for name in ("install", "uninstall"):
        sub = hook_commands.add_parser(name)
        sub.add_argument("agent", choices=("claude", "codex", "cursor"))
        sub.add_argument("--home")
        sub.add_argument("--json", action="store_true")
        if name == "install":
            sub.add_argument("--dry-run", action="store_true")
    marker = commands.add_parser("marker")
    marker.add_argument("--id", required=True)
    marker.add_argument("--transport", required=True, choices=("com8", "bus", "native"))
    marker.add_argument("--expect", required=True, choices=("reply", "none"))
    marker.add_argument("--reply-to", default="")
    marker.add_argument("--reply-as", default="")
    marker.add_argument("--hub", default="")
    marker.add_argument("--bus", default="")
    marker.add_argument("--ttl", type=float)

    obligation = commands.add_parser("obligation")
    obligation_commands = obligation.add_subparsers(dest="ob_cmd")
    for name in ("list", "sweep"):
        sub = obligation_commands.add_parser(name)
        sub.add_argument("--json", action="store_true")
    webhook = obligation_commands.add_parser("webhook")
    webhook_commands = webhook.add_subparsers(dest="wh_cmd")
    add = webhook_commands.add_parser("add")
    add.add_argument("--url", required=True)
    add.add_argument("--secret-file", required=True)
    add.add_argument("--event", default="")
    add.add_argument("--json", action="store_true")
    remove = webhook_commands.add_parser("remove")
    remove.add_argument("id")
    listing = webhook_commands.add_parser("list")
    listing.add_argument("--json", action="store_true")

    # `com8_reply.py marker` is the ask.sh entry. `com8 hook …` is the CLI entry.
    if argv and argv[0] == "marker":
        args = parser.parse_args(argv)
    else:
        args = parser.parse_args(argv)
    try:
        if args.cmd == "marker":
            fields = {"id": args.id, "transport": args.transport, "expect": args.expect,
                      "reply_to": args.reply_to, "reply_as": args.reply_as,
                      "hub": args.hub, "bus": args.bus}
            if args.ttl is not None:
                fields["ttl"] = args.ttl
            print(marker_line(**fields))
            return 0
        if args.cmd == "hook" and args.hook_cmd == "agent":
            raw = sys.stdin.read(1_000_000)
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except ValueError:
                payload = {}
            json.dump(handle(payload), sys.stdout, sort_keys=True, separators=(",", ":"))
            print()
            return 0
        if args.cmd == "hook" and args.hook_cmd == "install":
            result = install_hooks(args.agent, home=args.home, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(result))
            else:
                print("hook install %s %s" % (args.agent, "updated" if result["changed"] else "unchanged"))
            return 0
        if args.cmd == "hook" and args.hook_cmd == "uninstall":
            result = uninstall_hooks(args.agent, home=args.home)
            if args.json:
                print(json.dumps(result))
            else:
                print("hook uninstall %s %s" % (args.agent, "updated" if result["changed"] else "unchanged"))
            return 0
        if args.cmd != "obligation" or not args.ob_cmd:
            parser.print_help(sys.stderr)
            return 2
        ledger = Ledger()
        if args.ob_cmd == "list":
            rows = ledger.list_obligations()
            print(json.dumps(rows) if args.json else
                  "\n".join("%s %s %s" % (row["id"], row["status"], row["transport"])
                            for row in rows))
            return 0
        if args.ob_cmd == "sweep":
            report = ledger.sweep()
            print(json.dumps(report) if args.json else
                  "expired %d, webhooks sent %d" % (len(report["expired"]), report["sent"]))
            return 0
        if args.ob_cmd == "webhook" and args.wh_cmd == "add":
            item = ledger.webhook_add(args.url, args.secret_file, _events_from(args.event), time.time())
            print(json.dumps({"id": item["id"], "url": item["url"], "events": item["events"]})
                  if args.json else item["id"])
            return 0
        if args.ob_cmd == "webhook" and args.wh_cmd == "remove":
            print("removed" if ledger.webhook_remove(args.id) else "absent")
            return 0
        if args.ob_cmd == "webhook" and args.wh_cmd == "list":
            rows = [{"id": item["id"], "url": item["url"], "events": item["events"]}
                    for item in ledger.webhooks()]
            print(json.dumps(rows) if args.json else
                  "\n".join("%s %s" % (item["id"], item["url"]) for item in rows))
            return 0
    except ReplyError as exc:
        sys.stderr.write("com8: %s\n" % exc)
        return 1
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
