"""Fixed authenticated service endpoints for the existing outbound bus worker.

The only caller identity is the broker poll envelope. Operator configuration is
local; remote messages cannot choose an endpoint, token, executable or user ID.
"""
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID

MAX_BODY = 65536
MAX_REPLY = 32768
MAX_PENDING = 128


def contract_version(value):
    if type(value) is not int or value not in (1, 2):
        raise ServiceError("unsupported service contract version")
    return value


class ServiceError(ValueError):
    pass


def endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ServiceError("service endpoint must be a fixed HTTPS URL")
    return value


def credential(path):
    """Read a regular private owner file without following its final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ServiceError("service credential must be a private owner file")
        if not 32 <= info.st_size <= 4096:
            raise ServiceError("service credential has invalid size")
        token = os.read(fd, 4097).decode("ascii").strip()
        if not 32 <= len(token) <= 4096 or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in token):
            raise ServiceError("invalid service credential")
        return token
    finally:
        os.close(fd)


def registration(args):
    service_id = str(UUID(args.service_id))
    endpoint(args.endpoint)
    token_file = str(Path(args.token_file).absolute())
    credential(token_file)
    return {"kind": "service", "session_key": "service:" + service_id, "name": args.name,
            "service_id": service_id, "endpoint": args.endpoint, "token_file": token_file,
            "contract_version": contract_version(getattr(args, "contract_version", 1)), "status": "queueable"}


def configured(record):
    try:
        endpoint(record["endpoint"])
        credential(record["token_file"])
        UUID(record["service_id"])
        contract_version(record.get("contract_version", 1))
        return True
    except (OSError, ValueError, KeyError, UnicodeError):
        return False


def journal(root):
    path = Path(root) / "service-jobs.sqlite"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ServiceError("invalid service journal")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN IMMEDIATE")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs(
        hub TEXT NOT NULL, id TEXT NOT NULL, recipient TEXT NOT NULL, service_id TEXT NOT NULL,
        envelope TEXT NOT NULL, request_hash TEXT NOT NULL, expires REAL NOT NULL,
        operation TEXT NOT NULL DEFAULT 'deliver', state TEXT NOT NULL DEFAULT 'pending',
        reply TEXT, receipt TEXT, next_at REAL NOT NULL DEFAULT 0, updated REAL NOT NULL,
        PRIMARY KEY(hub,id))""")
    # Existing admitted jobs retain v1 even if their registration is upgraded.
    if "contract_version" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
        db.execute("ALTER TABLE jobs ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1")
    db.commit()
    return db


def normalized(envelope, version=1):
    # Discard the delivery lease and all presentation labels. These assertions
    # come only from broker poll; never parse identity fields from message text.
    keys = ("id", "target", "bus", "reply_to", "message", "created_at", "expires_at", "conversation_expires_at")
    result = {key: envelope[key] for key in keys}
    result["sender"] = {key: envelope["sender"][key] for key in ("id", "kind", "user", "device_id")}
    if contract_version(version) == 2:
        # A legacy queued reply has no stored parent. Never mistake missing
        # correlation for a new request and trigger another agent turn.
        if type(envelope.get("correlation_version")) is not int or envelope["correlation_version"] != 1:
            raise ServiceError("broker reply correlation is unavailable")
        conversation = envelope.get("conversation_id")
        if not isinstance(conversation, str) or not re.fullmatch(r"(?:c|hc)_[0-9a-f]{32}", conversation):
            raise ServiceError("invalid broker conversation identity")
        if "in_reply_to" not in envelope:
            raise ServiceError("broker reply correlation is unavailable")
        parent = envelope["in_reply_to"]
        if parent is not None and (not isinstance(parent, str) or not re.fullmatch(r"(?:m|hm)_[0-9a-f]{32}", parent)):
            raise ServiceError("invalid broker reply identity")
        result.update(correlation_version=1, conversation_id=conversation, in_reply_to=parent)
    return result


def enqueue(root, record, envelope):
    if envelope["target"] != record["id"] or envelope["reply_to"] != envelope["id"]:
        raise ServiceError("service delivery identity mismatch")
    expiry = envelope["conversation_expires_at"]
    if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
            or not math.isfinite(expiry) or expiry <= time.time() or expiry > time.time() + 86401):
        raise ServiceError("invalid service reply deadline")
    db = journal(root)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE hub=? AND id=?", (record["url"], envelope["id"])).fetchone()
            version = contract_version(old["contract_version"] if old else record.get("contract_version", 1))
            item = normalized(envelope, version)
            body = json.dumps(item, sort_keys=True, separators=(",", ":"))
            if len(body.encode()) > MAX_BODY - 2048:
                raise ServiceError("service message exceeds limit")
            digest = hashlib.sha256(body.encode()).hexdigest()
            if old:
                if old["request_hash"] != digest or old["service_id"] != record["service_id"]:
                    raise ServiceError("service message identity conflict")
            else:
                count = db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
                if count >= MAX_PENDING:
                    raise ServiceError("service transport queue is full")
                db.execute("""INSERT INTO jobs(hub,id,recipient,service_id,envelope,request_hash,expires,updated,contract_version)
                              VALUES(?,?,?,?,?,?,?,?,?)""", (record["url"], item["id"], record["id"],
                              record["service_id"], body, digest, expiry, time.time(), version))
        return "queued", "durably queued for service; task acceptance and answer not yet confirmed"
    finally:
        db.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def bridge(record, job):
    payload = {"version": contract_version(job["contract_version"]), "op": job["operation"], "hub": job["hub"],
               "recipient_id": job["recipient"], "service_id": job["service_id"],
               "envelope": json.loads(job["envelope"])}
    body = json.dumps(payload).encode()
    if len(body) > MAX_BODY:
        raise ServiceError("service request exceeds limit")
    req = urllib.request.Request(endpoint(record["endpoint"]), data=body, method="POST", headers={
        "Authorization": "Bearer " + credential(record["token_file"]), "Content-Type": "application/json"})
    context = ssl.create_default_context()
    # Match the broker client: Apple's Python can ignore SSL_CERT_FILE in
    # load_default_certs. Explicitly honor operator-installed CAs, never skip TLS.
    if os.environ.get("SSL_CERT_FILE"):
        context.load_verify_locations(cafile=os.environ["SSL_CERT_FILE"])
    handlers = [NoRedirect(), urllib.request.HTTPSHandler(context=context)]
    if urllib.parse.urlsplit(record["endpoint"]).hostname in ("localhost", "127.0.0.1", "::1"):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=15) as response:
        if response.status != 200:
            raise ServiceError("service endpoint returned invalid status")
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ServiceError("service response exceeds limit")
    return validate_response(json.loads(raw), job["contract_version"])


def validate_response(result, version):
    contract_version(version)
    if not isinstance(result, dict) or type(result.get("pending")) is not bool:
        raise ServiceError("invalid service response")
    if result["pending"]:
        if set(result) != {"pending"}:
            raise ServiceError("invalid pending service response")
    elif version == 2 and result == {"pending": False, "disposition": "acknowledge"}:
        pass
    elif (set(result) != {"pending", "reply"} or not isinstance(result["reply"], str)
          or not result["reply"].strip() or "\x00" in result["reply"]
          or not 1 <= len(result["reply"].encode()) <= MAX_REPLY):
        raise ServiceError("invalid terminal service response")
    return result


def advance(root, registrations, connections, broker_request, *, call_bridge=bridge, now=None):
    """Bounded drain, with the final response persisted before idempotent reply.

    One worker owns this drain. SQLite transactions cover concurrent enqueue;
    a restart resumes deliver/poll/reply without trusting an in-memory receipt.
    """
    now = time.time() if now is None else now
    db = journal(root)
    errors = []
    try:
        with db:
            db.execute("UPDATE jobs SET state='expired',updated=? WHERE state='pending' AND expires<=?", (now, now))
            db.execute("DELETE FROM jobs WHERE state!='pending' AND updated<?", (now - 172800,))
        records = {(r["url"], r["id"]): r for r in registrations.values() if r.get("kind") == "service"}
        jobs = db.execute("SELECT * FROM jobs WHERE state='pending' AND next_at<=? ORDER BY next_at,updated LIMIT 128", (now,)).fetchall()
        available = []
        for job in jobs:
            record = records.get((job["hub"], job["recipient"]))
            if (record and record["service_id"] == job["service_id"] and job["hub"] in connections
                    and (record.get("buses") or record.get("reply_until", 0) > now)):
                available.append((dict(job), record))
        available = available[:8]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(call_bridge, record, job): (job, record)
                       for job, record in available if job["reply"] is None}
            ready = [(job, record) for job, record in available if job["reply"] is not None]
            for future in concurrent.futures.as_completed(futures):
                job, record = futures[future]
                try:
                    result = validate_response(future.result(), job["contract_version"])
                    reply = result.get("reply")
                except Exception:
                    # Endpoint errors can contain credentials or private task data.
                    # Keep only a fixed diagnostic; retries preserve the deadline.
                    errors.append({"agent": job["recipient"], "error": "service endpoint unavailable; retry scheduled"})
                    with db:
                        db.execute("UPDATE jobs SET next_at=?,updated=? WHERE hub=? AND id=?",
                                   (now + 5, now, job["hub"], job["id"]))
                    continue
                if result.get("disposition") == "acknowledge":
                    # A terminal app-side receipt, not another broker message.
                    # Persist it before returning; restart must not resubmit.
                    with db:
                        db.execute("UPDATE jobs SET state='acknowledged',updated=? WHERE hub=? AND id=?",
                                   (now, job["hub"], job["id"]))
                    continue
                with db:
                    db.execute("UPDATE jobs SET operation='poll',reply=?,next_at=?,updated=? WHERE hub=? AND id=?",
                               (reply, now + 5, now, job["hub"], job["id"]))
                if reply is not None:
                    job["reply"] = reply
                    ready.append((job, record))
            for job, record in ready:
                try:
                    receipt = broker_request(connections[job["hub"]], "reply", sender=job["recipient"],
                        id=json.loads(job["envelope"])["reply_to"], message=job["reply"],
                        request_id="service:" + hashlib.sha256((job["hub"] + "|" + job["id"]).encode()).hexdigest())
                    with db:
                        db.execute("UPDATE jobs SET state='sent',receipt=?,updated=? WHERE hub=? AND id=?",
                                   (json.dumps(receipt), now, job["hub"], job["id"]))
                except Exception as exc:
                    state = "expired" if getattr(exc, "code", None) in {"not_found", "forbidden", "unauthorized"} else "pending"
                    with db:
                        db.execute("UPDATE jobs SET state=?,next_at=?,updated=? WHERE hub=? AND id=?",
                                   (state, now + 5, now, job["hub"], job["id"]))
                    errors.append({"agent": job["recipient"], "error": "service reply unavailable; " + state})
        return errors
    finally:
        db.close()
