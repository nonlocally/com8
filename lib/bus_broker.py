#!/usr/bin/env python3
"""Communicate's explicit bus registry and message gateway (stdlib only).

The HTTP listener is loopback-only. Remote clients reach it through a TLS
reverse proxy; no agent socket, shell, or local administration credential is
published by this module. Every data operation authenticates independently.
"""
import base64
import getpass
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import sqlite3
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlsplit
import uuid


MAX_BODY = 131072
MAX_MESSAGE = 32768
MAX_PENDING = 256
MAX_AGENTS = 128
MAX_TOTAL_AGENTS = 4096
MAX_PRINCIPALS = 1024
MAX_BUSES = 256
MAX_ACCOUNT_BUSES = 32
MAX_ACCOUNT_INVITES = 128
MAX_ACTIVE_EVENTS_PER_BUS = 8
MAX_EVENT_HISTORY = 20
MAX_INVITES = 4096
MAX_RECORDS = 20000
MAX_SSO_REPLAYS = 4096
MAX_VIEW_HEADER = 8192
MAX_CHATS = 4096
MAX_CHATS_PER_IDENTITY = 128
MAX_CHAT_MESSAGES = 20000
MAX_CHAT_MESSAGES_PER_CHAT = 2000
MAX_CHAT_SEEN = 80000
MAX_CHAT_PAGE_BYTES = 512 * 1024
MAX_CHAT_OPENWEBUI_TARGETS = 256
MESSAGE_TTL = 86400
CHAT_RETENTION = 30 * MESSAGE_TTL
RECEIPT_RETENTION = 7 * MESSAGE_TTL
LIVE_TTL = 45
LEASE_TTL = 60
BUS_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
USER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
DEVICE_FIELDS = {"hostname": 253, "platform": 64, "tailscale_hostname": 253, "tailscale_dns_name": 253}
ACTIVE = ("accepted", "leased")
TERMINAL = ("delivered", "queued", "failed", "expired", "cancelled")
CHAT_OPS = frozenset(("chat_open", "chat_list", "chat_messages", "chat_send", "chat_read"))
ACCOUNT_OPS = frozenset(("create", "member_add", "member_remove", "invite", "invite_revoke",
                         "event_create", "event_get", "event_revoke", "event_remove"))
CHAT_FIELDS = {"chat_open": {"bus", "agent"}, "chat_list": set(), "chat_messages": {"chat", "after", "limit"},
               "chat_send": {"chat", "request_id", "message"}, "chat_read": {"chat", "through"}}


class BusError(Exception):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value, label, limit, optional=False):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise BusError("invalid %s" % label)
    if (not optional and not value.strip()) or any(ord(c) < 32 for c in value):
        raise BusError("invalid %s" % label)
    return value


def _bus(value):
    if not isinstance(value, str) or not BUS_RE.fullmatch(value):
        raise BusError("bus names use lowercase letters, digits, dots, dashes, or underscores")
    return value


def _user(value):
    if not isinstance(value, str) or not USER_RE.fullmatch(value):
        raise BusError("invalid user handle")
    return value


def _device_metadata(value):
    if not isinstance(value, dict) or any(key not in DEVICE_FIELDS for key in value):
        raise BusError("invalid device metadata")
    return {key: _text(text, key, DEVICE_FIELDS[key]) for key, text in value.items()}


def _unique_object(pairs):
    """Reject duplicate JSON fields at the gateway's authorization boundary."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _canonical_uuid(value, version=None):
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return str(parsed) == value and (version is None or parsed.version == version)
    except ValueError:
        return False


class Broker:
    """One durable broker; SQLite transactions serialize enrollment and leases."""

    def __init__(self, state_dir, clock=None):
        self.gateway_shared_secret = os.environ.get("BUS_GATEWAY_SHARED_SECRET")
        if self.gateway_shared_secret is not None and len(self.gateway_shared_secret) < 32:
            raise ValueError("BUS_GATEWAY_SHARED_SECRET must contain at least 32 characters")
        self.admin_readers = frozenset(reader.strip() for reader in os.environ.get("BUS_ADMIN_READERS", "").split(",")
                                      if reader.strip())
        self.openwebui_readers = os.environ.get("BUS_OPENWEBUI_READERS") == "1"
        try:
            self.reader_users = json.loads(os.environ.get("BUS_READER_USERS", "{}"))
            if (not isinstance(self.reader_users, dict) or len(self.reader_users) > MAX_PRINCIPALS
                    or any(not isinstance(reader, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", reader)
                           for reader in self.reader_users)):
                raise ValueError()
            for user in self.reader_users.values():
                _user(user)
        except (ValueError, TypeError, BusError):
            raise ValueError("BUS_READER_USERS must map reader logins to valid user handles") from None
        self.users = sorted(set(self.reader_users.values()))
        try:
            self.account_labels = json.loads(os.environ.get("BUS_ACCOUNT_LABELS", "{}"), object_pairs_hook=_unique_object)
            if (not isinstance(self.account_labels, dict) or len(self.account_labels) > MAX_PRINCIPALS
                    or any(user not in self.users or not isinstance(label, str)
                           or not 1 <= len(label) <= 128 or label != label.strip()
                           or any(ord(char) < 32 or ord(char) == 127 for char in label)
                           for user, label in self.account_labels.items())):
                raise ValueError()
        except (ValueError, TypeError, BusError):
            raise ValueError("BUS_ACCOUNT_LABELS must map configured accounts to readable labels") from None
        try:
            self.chat_readers = json.loads(os.environ.get("BUS_CHAT_READERS", "{}"), object_pairs_hook=_unique_object)
            if not isinstance(self.chat_readers, dict) or len(self.chat_readers) > MAX_PRINCIPALS:
                raise ValueError()
            for reader, grant in self.chat_readers.items():
                if (not isinstance(reader, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", reader)
                        or not isinstance(grant, dict) or set(grant) != {"identity", "buses"}
                        or not isinstance(grant["buses"], list) or len(grant["buses"]) > 64
                        or len(set(grant["buses"])) != len(grant["buses"])):
                    raise ValueError()
                _user(grant["identity"])
                for bus in grant["buses"]:
                    _bus(bus)
        except (ValueError, TypeError, BusError):
            raise ValueError("BUS_CHAT_READERS must map trusted readers to explicit identity and bus grants") from None
        try:
            targets = json.loads(os.environ.get("BUS_CHAT_OPENWEBUI_TARGETS", "[]"), object_pairs_hook=_unique_object)
            if not isinstance(targets, list) or len(targets) > MAX_CHAT_OPENWEBUI_TARGETS:
                raise ValueError()
            unique = set()
            for target in targets:
                if (not isinstance(target, dict) or set(target) != {"bus", "agent"}
                        or not isinstance(target["agent"], str) or not re.fullmatch(r"a_[0-9a-f]{32}", target["agent"])):
                    raise ValueError()
                unique.add((_bus(target["bus"]), target["agent"]))
            self.chat_openwebui_targets = tuple(sorted(unique))
        except (ValueError, TypeError, BusError):
            raise ValueError("BUS_CHAT_OPENWEBUI_TARGETS must list exact installed bus and agent targets") from None
        self.root = Path(state_dir).expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or self.root.stat().st_uid != os.getuid():
            raise ValueError("bus state directory must be owned by this user, not a symlink")
        os.chmod(self.root, 0o700)
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.db_path = self.root / "bus.sqlite3"
        token_path = self.root / "admin.token"
        # Publish a completely written file atomically: simultaneous cold
        # starts must never observe an empty token or rotate each other's key.
        if not token_path.exists():
            fd, staging = tempfile.mkstemp(prefix=".admin-", dir=self.root)
            try:
                with os.fdopen(fd, "w") as out:
                    out.write(secrets.token_urlsafe(32) + "\n")
                    out.flush()
                    os.fsync(out.fileno())
                try:
                    os.link(staging, token_path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(staging)
        if token_path.is_symlink() or token_path.stat().st_uid != os.getuid():
            raise ValueError("unsafe bus admin token file")
        os.chmod(token_path, 0o600)
        self.admin_token = token_path.read_text().strip()
        if len(self.admin_token) < 32:
            raise ValueError("invalid bus admin token file")
        if self.db_path.is_symlink():
            raise ValueError("unsafe bus database file")
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS principals(
                  id TEXT PRIMARY KEY, device TEXT NOT NULL, created_at REAL NOT NULL,
                  revoked INTEGER NOT NULL DEFAULT 0, is_admin INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tokens(
                  digest TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES principals(id));
                CREATE TABLE IF NOT EXISTS browser_credentials(
                  digest TEXT PRIMARY KEY REFERENCES tokens(digest) ON DELETE CASCADE,
                  reader TEXT NOT NULL, reader_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sso_replays(jti TEXT PRIMARY KEY, expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sso_replay_expiry ON sso_replays(expires_at);
                CREATE TABLE IF NOT EXISTS buses(name TEXT PRIMARY KEY, visibility TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bus_ownership(
                  bus TEXT PRIMARY KEY REFERENCES buses(name), user TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_memberships(
                  user TEXT NOT NULL, bus TEXT NOT NULL REFERENCES buses(name),
                  PRIMARY KEY(user,bus));
                CREATE TABLE IF NOT EXISTS event_invites(
                  id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
                  bus TEXT NOT NULL REFERENCES buses(name), created_at REAL NOT NULL,
                  expires_at REAL NOT NULL, max_uses INTEGER NOT NULL,
                  uses INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
                  issuer_user TEXT);
                CREATE TABLE IF NOT EXISTS event_joins(
                  event TEXT NOT NULL REFERENCES event_invites(id),
                  principal TEXT NOT NULL REFERENCES principals(id),
                  joined_at REAL NOT NULL, removed INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(event,principal));
                CREATE TABLE IF NOT EXISTS grants(
                  principal TEXT NOT NULL REFERENCES principals(id),
                  bus TEXT NOT NULL REFERENCES buses(name), PRIMARY KEY(principal,bus));
                CREATE TABLE IF NOT EXISTS invites(
                  digest TEXT PRIMARY KEY, bus TEXT NOT NULL REFERENCES buses(name),
                  expires_at REAL NOT NULL, redeemed_at REAL, principal TEXT);
                CREATE TABLE IF NOT EXISTS agents(
                  id TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES principals(id),
                  session_key TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                  description TEXT NOT NULL, status TEXT NOT NULL, last_seen REAL NOT NULL,
                  UNIQUE(principal,session_key));
                CREATE TABLE IF NOT EXISTS memberships(
                  agent TEXT NOT NULL REFERENCES agents(id),
                  bus TEXT NOT NULL REFERENCES buses(name), PRIMARY KEY(agent,bus));
                CREATE TABLE IF NOT EXISTS conversations(
                  id TEXT PRIMARY KEY, initiator TEXT NOT NULL REFERENCES agents(id),
                  published TEXT NOT NULL REFERENCES agents(id), bus TEXT NOT NULL REFERENCES buses(name),
                  expires_at REAL NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS messages(
                  id TEXT PRIMARY KEY, sender TEXT NOT NULL REFERENCES agents(id),
                  target TEXT NOT NULL REFERENCES agents(id), bus TEXT NOT NULL,
                  message TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL, expires_at REAL NOT NULL,
                  lease TEXT, lease_until REAL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS message_delivery ON messages(target,status,created_at);
                CREATE TABLE IF NOT EXISTS reply_requests(
                  sender TEXT NOT NULL, message TEXT NOT NULL, request_id TEXT NOT NULL,
                  content_hash TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL,
                  PRIMARY KEY(sender,message,request_id));
                CREATE TABLE IF NOT EXISTS human_chats(
                  id TEXT PRIMARY KEY, identity TEXT NOT NULL, bus TEXT NOT NULL REFERENCES buses(name),
                  agent TEXT NOT NULL REFERENCES agents(id), created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  next_seq INTEGER NOT NULL DEFAULT 1, read_seq INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(identity,bus,agent));
                CREATE TABLE IF NOT EXISTS human_chat_messages(
                  id TEXT PRIMARY KEY, chat TEXT NOT NULL REFERENCES human_chats(id) ON DELETE CASCADE,
                  identity TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL, status TEXT NOT NULL,
                  in_reply_to TEXT, request_id TEXT, sender_reader TEXT, expires_at REAL,
                  closed INTEGER NOT NULL DEFAULT 0, lease TEXT, lease_until REAL,
                  detail TEXT NOT NULL DEFAULT '', UNIQUE(chat,seq), UNIQUE(identity,request_id));
                CREATE INDEX IF NOT EXISTS human_chat_history ON human_chat_messages(chat,seq);
                CREATE TABLE IF NOT EXISTS human_chat_seen(
                  chat TEXT NOT NULL REFERENCES human_chats(id) ON DELETE CASCADE,
                  reader TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(chat,reader,seq),
                  FOREIGN KEY(chat,seq) REFERENCES human_chat_messages(chat,seq) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS outbound_queue(
                  ordering INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL UNIQUE,
                  target TEXT NOT NULL REFERENCES agents(id));
            """)
            # Upgrade existing hubs without changing credentials or guessing the
            # owner of an enrolled device from its self-reported display name.
            db.execute("BEGIN IMMEDIATE")
            for table, name, declaration in (("principals", "user", "TEXT"),
                                              ("principals", "device_metadata", "TEXT NOT NULL DEFAULT '{}'"),
                                              ("invites", "user", "TEXT"),
                                              ("invites", "issuer_user", "TEXT"),
                                              ("messages", "conversation", "TEXT REFERENCES conversations(id)"),
                                              ("messages", "in_reply_to", "TEXT"),
                                              ("messages", "correlation_version", "INTEGER NOT NULL DEFAULT 0")):
                columns = {row[1] for row in db.execute("PRAGMA table_info(%s)" % table)}
                if name not in columns:
                    db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, declaration))
            # Preserve queued payloads and their original expiry on upgrade,
            # while giving upgraded receivers the same scoped reply operation.
            for message in db.execute("SELECT * FROM messages WHERE conversation IS NULL").fetchall():
                conversation = "c_legacy_" + message["id"]
                db.execute("INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,?,0)",
                           (conversation, message["sender"], message["target"], message["bus"], message["expires_at"]))
                db.execute("UPDATE messages SET conversation=? WHERE id=?", (conversation, message["id"]))
            db.execute("INSERT OR IGNORE INTO outbound_queue(message,target) SELECT id,target FROM messages ORDER BY rowid")
            db.execute("INSERT OR IGNORE INTO meta VALUES('server_id',?)", (uuid.uuid4().hex,))
            db.execute("INSERT OR IGNORE INTO principals(id,device,created_at,is_admin) VALUES('admin','local',?,1)",
                       (self.clock(),))
            local_user = getpass.getuser().lower()
            db.execute("UPDATE principals SET user=COALESCE(user,?) WHERE id='admin'",
                       (local_user if USER_RE.fullmatch(local_user) else "local",))
            # Existing private buses stay operator-owned (NULL). Never infer
            # account ownership from names, past invitations or device labels.
            db.execute("INSERT OR IGNORE INTO buses(name,visibility) VALUES('general','open')")
            db.execute("INSERT OR IGNORE INTO tokens VALUES(?,'admin')", (_digest(self.admin_token),))
            self.server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
        db.close()
        os.chmod(self.db_path, 0o600)

    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def browser_session(self, reader, reader_hash, view=None):
        """Bootstrap only from the HTTP handler's authenticated gateway context."""
        if self.gateway_shared_secret is None:
            raise BusError("browser gateway is not configured", "not_found")
        self._reader_context(reader, reader_hash)
        view = self._view_assertion(reader, reader_hash, view, self.clock())
        principal = "web_" + _digest(reader)
        material = json.dumps(["communicate-browser-v1", self.server_id, reader, reader_hash],
                              separators=(",", ":")).encode("utf-8")
        token = "web1." + base64.urlsafe_b64encode(hmac.new(self.admin_token.encode("utf-8"), material,
                                                         hashlib.sha256).digest()).decode("ascii").rstrip("=")
        digest = _digest(token)
        with self.lock:
            db = self._connect()
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    view = self._view_assertion(reader, reader_hash, view, self.clock())
                    self._existing_view_buses(db, view)
                    existing = db.execute("SELECT revoked FROM principals WHERE id=?", (principal,)).fetchone()
                    if existing is not None and existing["revoked"]:
                        raise BusError("browser access was revoked", "forbidden")
                    if existing is None and db.execute("SELECT COUNT(*) FROM principals").fetchone()[0] >= MAX_PRINCIPALS:
                        raise BusError("device enrollment limit reached", "limit")
                    admin = self._browser_admin(reader)
                    user = self._reader_user(reader)
                    db.execute("""INSERT INTO principals(id,device,created_at,is_admin,user) VALUES(?,?,?,?,?)
                                  ON CONFLICT(id) DO UPDATE SET device=excluded.device,is_admin=excluded.is_admin,user=excluded.user""",
                               (principal, "browser:" + reader, self.clock(), admin, user))
                    db.execute("DELETE FROM grants WHERE principal=?", (principal,))
                    if view is None:
                        db.execute("INSERT INTO grants VALUES(?,'general')", (principal,))
                    # Password changes replace the credential, without changing the reader's identity.
                    db.execute("DELETE FROM tokens WHERE principal=? AND digest<>?", (principal, digest))
                    db.execute("INSERT OR IGNORE INTO tokens VALUES(?,?)", (digest, principal))
                    db.execute("INSERT OR REPLACE INTO browser_credentials VALUES(?,?,?)", (digest, reader, reader_hash))
                    current = self._auth(db, token, reader, reader_hash, view)
                    self._chat_observe_scope(db, current, self.clock())
                    self._cancel_chat_invalid(db, self.clock())
            finally:
                db.close()
        result = {"ok": True, "token": token, "user": user, "browser_session": True, "logout_url": "/_gateway/logout"}
        if view is not None and "display_name" in view:
            result["display_name"] = view["display_name"]
        return result

    def _browser_admin(self, reader):
        # Even a mistaken admin-reader setting cannot promote an OWUI viewer.
        return not reader.startswith("owui.") and reader in self.admin_readers

    def _reader_user(self, reader):
        if reader.startswith("owui."):
            if not self.openwebui_readers or not _canonical_uuid(reader[5:]):
                raise BusError("OpenWebUI browser reader is not enabled or valid", "forbidden")
            return reader
        if self.reader_users and reader not in self.reader_users:
            raise BusError("reader has no configured bus user", "forbidden")
        return _user(self.reader_users.get(reader, reader.lower()))

    def _view_assertion(self, reader, reader_hash, view, now):
        """Validate request-local directory access, never a device grant."""
        if view is None and not reader.startswith("owui."):
            return None
        try:
            if (not self.openwebui_readers or not reader.startswith("owui.") or
                    not _canonical_uuid(reader[5:]) or not isinstance(view, dict) or
                    set(view) - {"v", "reader", "reader_hash", "iat", "exp", "buses", "display_name"} or
                    len(json.dumps(view).encode("utf-8")) > MAX_VIEW_HEADER or
                    type(view.get("v")) is not int or view["v"] != 1 or
                    view.get("reader") != reader or view.get("reader_hash") != reader_hash or
                    type(view.get("iat")) is not int or type(view.get("exp")) is not int or
                    not 1 <= view["exp"] - view["iat"] <= 60 or view["iat"] > now + 5 or view["exp"] <= now):
                raise ValueError()
            buses = view.get("buses")
            if (not isinstance(buses, list) or len(buses) > 64 or
                    any(not isinstance(bus, str) or not BUS_RE.fullmatch(bus) for bus in buses) or
                    len(set(buses)) != len(buses)):
                raise ValueError()
            if "display_name" in view:
                label = view["display_name"]
                if (not isinstance(label, str) or len(label.encode("utf-8")) > 160 or
                        any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in label)):
                    raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BusError("valid current browser view assertion required", "unauthorized") from None
        return dict(view, buses=list(buses))

    @staticmethod
    def _existing_view_buses(db, view):
        if view is not None and any(not db.execute("SELECT 1 FROM buses WHERE name=?", (bus,)).fetchone()
                                    for bus in view["buses"]):
            raise BusError("browser view assertion contains an unknown bus", "unauthorized")

    @staticmethod
    def _device_info(principal):
        return {"user": principal["user"], "device": principal["device"], "device_id": principal["id"],
                "device_metadata": json.loads(principal["device_metadata"])}

    @staticmethod
    def _reader_context(reader, reader_hash):
        if (not isinstance(reader, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", reader)
                or not isinstance(reader_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", reader_hash)):
            raise BusError("authenticated browser reader required", "unauthorized")

    def _auth(self, db, token, reader=None, reader_hash=None, view=None):
        if not isinstance(token, str) or not 32 <= len(token) <= 512:
            raise BusError("authentication required", "unauthorized")
        p = db.execute("""SELECT p.*,b.reader AS browser_reader,b.reader_hash AS browser_hash
                          FROM principals p JOIN tokens t ON p.id=t.principal
                          LEFT JOIN browser_credentials b ON b.digest=t.digest
                          WHERE t.digest=? AND p.revoked=0""", (_digest(token),)).fetchone()
        if p is None:
            raise BusError("authentication required", "unauthorized")
        p = dict(p)
        if p["browser_reader"] is not None:
            self._reader_context(reader, reader_hash)
            if (self.gateway_shared_secret is None
                    or not secrets.compare_digest(reader, p["browser_reader"])
                    or not secrets.compare_digest(reader_hash, p["browser_hash"])):
                raise BusError("browser reader session does not match", "unauthorized")
            view = self._view_assertion(reader, reader_hash, view, self.clock())
            self._existing_view_buses(db, view)
            p["is_admin"] = self._browser_admin(reader)
            p["user"] = self._reader_user(reader)
            if view is not None:
                p["view_buses"] = view["buses"]
                if "display_name" in view:
                    p["display_name"] = view["display_name"]
            db.execute("UPDATE principals SET is_admin=?,user=? WHERE id=?", (p["is_admin"], p["user"], p["id"]))
            if view is None and not p["is_admin"]:
                db.execute("DELETE FROM grants WHERE principal=? AND bus<>'general'", (p["id"],))
        return p

    def consume_sso(self, request):
        """Consume a gateway-verified identity handoff once, across restarts."""
        if self.gateway_shared_secret is None:
            raise BusError("SSO replay endpoint is not configured", "not_found")
        now = self.clock()
        if (not isinstance(request, dict) or set(request) != {"jti", "exp"} or
                not _canonical_uuid(request.get("jti"), version=4) or type(request.get("exp")) is not int or
                not now - 30 <= request["exp"] <= now + 90):
            raise BusError("invalid SSO replay request")
        with self.lock:
            db = self._connect()
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    now = self.clock()
                    if not now - 30 <= request["exp"] <= now + 90:
                        raise BusError("invalid SSO replay request")
                    db.execute("DELETE FROM sso_replays WHERE expires_at < ?", (now,))
                    if db.execute("SELECT 1 FROM sso_replays WHERE jti=?", (request["jti"],)).fetchone():
                        raise BusError("SSO handoff already consumed", "conflict")
                    if db.execute("SELECT COUNT(*) FROM sso_replays").fetchone()[0] >= MAX_SSO_REPLAYS:
                        raise BusError("SSO replay store is full", "limit")
                    db.execute("INSERT INTO sso_replays VALUES(?,?)", (request["jti"], request["exp"] + 30))
            finally:
                db.close()
        return {"ok": True}

    @staticmethod
    def _admin(p):
        if not p["is_admin"]:
            raise BusError("administrator access required", "forbidden")

    def _account_user(self, p):
        """Only a currently gateway-authenticated mapped browser is an account.

        A device's user is invitation attribution, not evidence of a login:
        someone holding an invitation can enroll that device. Never elevate it.
        OpenWebUI group views remain separate, read-only directory authority.
        """
        reader = p.get("browser_reader")
        if (reader and not reader.startswith("owui.") and reader in self.reader_users
                and self.reader_users[reader] == p.get("user")):
            return p["user"]
        return None

    @staticmethod
    def _bus_definition(db, name):
        # Keep the original two-column buses table intact: older released
        # brokers use positional inserts during startup and bus creation.
        return db.execute("""SELECT b.*,o.user AS owner_user FROM buses b
                             LEFT JOIN bus_ownership o ON o.bus=b.name WHERE b.name=?""", (name,)).fetchone()

    @staticmethod
    def _account_member(db, bus, user):
        return bool(user and db.execute("""SELECT 1 FROM buses b WHERE b.name=? AND
          (EXISTS(SELECT 1 FROM bus_ownership o WHERE o.bus=b.name AND o.user=?) OR
           EXISTS(SELECT 1 FROM account_memberships m WHERE m.bus=b.name AND m.user=?))""",
                                       (bus, user, user)).fetchone())

    def _bus_role(self, db, p, bus):
        if p["is_admin"]:
            return "admin"
        user = self._account_user(p)
        if user and bus["owner_user"] == user:
            return "owner"
        if user and bus["owner_user"] is not None and self._account_member(db, bus["name"], user):
            return "member"
        return "viewer"

    def _visible_buses(self, db, p):
        if "view_buses" in p:
            return sorted(p["view_buses"])
        user = self._account_user(p)
        return [row[0] for row in db.execute("SELECT name FROM buses ORDER BY name")
                if self._granted(db, p["id"], row[0]) or self._account_member(db, row[0], user)]

    def _managed_bus(self, db, p, name, *, member=False):
        bus = self._bus_definition(db, name)
        roles = ("admin", "owner", "member") if member else ("admin", "owner")
        if bus is None or self._bus_role(db, p, bus) not in roles:
            raise BusError("bus management access required", "forbidden")
        return bus

    @staticmethod
    def _granted(db, principal, bus):
        return db.execute("""SELECT 1 FROM principals p WHERE p.id=? AND p.revoked=0
          AND (p.is_admin=1 OR EXISTS(SELECT 1 FROM grants g WHERE g.principal=p.id AND g.bus=?))""",
                          (principal, bus)).fetchone() is not None

    def _member(self, db, agent, bus):
        row = db.execute("SELECT principal FROM agents WHERE id=?", (agent,)).fetchone()
        return bool(row and self._granted(db, row[0], bus) and db.execute(
            "SELECT 1 FROM memberships WHERE agent=? AND bus=?", (agent, bus)).fetchone())

    @staticmethod
    def _owned(db, p, agent, admin=False):
        if not isinstance(agent, str):
            raise BusError("unknown or unavailable agent", "not_found")
        row = db.execute("SELECT * FROM agents WHERE id=?", (agent,)).fetchone()
        if row is None or (row["principal"] != p["id"] and not (admin and p["is_admin"])):
            raise BusError("unknown or unavailable agent", "not_found")
        return row

    def _expire(self, db, now):
        db.execute("DELETE FROM reply_requests WHERE created_at<?", (now - RECEIPT_RETENTION,))
        db.execute("""UPDATE human_chat_messages SET status='expired',detail='message expired',updated_at=?,closed=1
                      WHERE role='user' AND status IN ('accepted','leased') AND expires_at<=?""", (now, now))
        db.execute("UPDATE human_chat_messages SET closed=1 WHERE role='user' AND expires_at<=?", (now,))
        db.execute("DELETE FROM human_chat_messages WHERE created_at<?", (now - CHAT_RETENTION,))
        db.execute("""DELETE FROM human_chats WHERE updated_at<? AND NOT EXISTS
                      (SELECT 1 FROM human_chat_messages WHERE chat=human_chats.id)""", (now - CHAT_RETENTION,))
        db.execute("""UPDATE messages SET status='expired',detail='message expired',updated_at=?
                      WHERE status IN ('accepted','leased') AND expires_at<=?""", (now, now))
        # Retain receipts for seven days, with finite message and invite storage.
        db.execute("DELETE FROM messages WHERE updated_at<? AND status NOT IN ('accepted','leased')",
                   (now - RECEIPT_RETENTION,))
        db.execute("DELETE FROM invites WHERE expires_at<?", (now - MESSAGE_TTL,))
        db.execute("DELETE FROM conversations WHERE expires_at<? AND NOT EXISTS (SELECT 1 FROM messages WHERE conversation=conversations.id)",
                   (now - RECEIPT_RETENTION,))
        db.execute("""DELETE FROM outbound_queue WHERE NOT EXISTS (SELECT 1 FROM messages WHERE id=outbound_queue.message)
                      AND NOT EXISTS (SELECT 1 FROM human_chat_messages WHERE id=outbound_queue.message)""")
        db.execute("""DELETE FROM agents WHERE last_seen<?
                      AND NOT EXISTS (SELECT 1 FROM memberships WHERE agent=agents.id)
                      AND NOT EXISTS (SELECT 1 FROM messages WHERE sender=agents.id OR target=agents.id)
                      AND NOT EXISTS (SELECT 1 FROM human_chats WHERE agent=agents.id)
                      AND NOT EXISTS (SELECT 1 FROM outbound_queue WHERE target=agents.id)
                      AND NOT EXISTS (SELECT 1 FROM conversations WHERE initiator=agents.id OR published=agents.id)""",
                   (now - RECEIPT_RETENTION,))

    def _conversation_valid(self, db, conversation, now):
        if conversation is None or conversation["closed"] or conversation["expires_at"] <= now:
            return False
        bus = conversation["bus"]
        if not self._member(db, conversation["published"], bus):
            return False
        if bus != "general":
            return self._member(db, conversation["initiator"], bus)
        sender = db.execute("SELECT principal FROM agents WHERE id=?", (conversation["initiator"],)).fetchone()
        return bool(sender and self._granted(db, sender[0], bus))

    def _cancel_invalid(self, db, now):
        self._cancel_chat_invalid(db, now)
        # Close permanently on loss of admission/publication; rejoining must
        # never reactivate a previously withdrawn conversation.
        for conversation in db.execute("SELECT * FROM conversations WHERE closed=0").fetchall():
            if not self._conversation_valid(db, conversation, now):
                db.execute("UPDATE conversations SET closed=1 WHERE id=?", (conversation["id"],))
        pending = db.execute("SELECT id,sender,target,bus,conversation FROM messages WHERE status IN ('accepted','leased')").fetchall()
        for m in pending:
            if m["conversation"]:
                conversation = db.execute("SELECT * FROM conversations WHERE id=?", (m["conversation"],)).fetchone()
                valid = self._conversation_valid(db, conversation, now)
            else:
                # Preserve deliveries queued by the pre-conversation broker.
                valid = self._member(db, m["sender"], m["bus"]) and self._member(db, m["target"], m["bus"])
            if not valid:
                db.execute("UPDATE messages SET status='cancelled',detail='membership changed',updated_at=? WHERE id=?",
                           (now, m["id"]))

    def handle(self, token, request, *, reader=None, reader_hash=None, view=None):
        """Handle one bounded request, returning an ordinary JSON-safe result."""
        try:
            if not isinstance(request, dict) or not isinstance(request.get("op"), str):
                raise BusError("request must contain an operation")
            if len(json.dumps(request).encode("utf-8")) > MAX_BODY:
                raise BusError("request too large", "too_large")
            with self.lock:
                db = self._connect()
                observed = False
                try:
                    db.execute("BEGIN IMMEDIATE")
                    now = self.clock()
                    try:
                        p = self._auth(db, token, reader, reader_hash, view) if token else None
                    except BusError as error:
                        # Released clients retry unauthorized personal invites
                        # anonymously to recover a revoked installation. A shared
                        # event code must not silently replace its account with
                        # a guest after a bad/stale existing device credential.
                        if (error.code == "unauthorized" and request["op"] == "redeem"
                                and isinstance(request.get("invite"), str)
                                and request["invite"].startswith("event1.")):
                            raise BusError("event join requires valid existing device credentials; use a separate installation for a guest", "forbidden") from None
                        raise
                    if p is not None and p["browser_reader"] is not None:
                        # A valid reduced view is a revocation observation even
                        # when the requested operation is subsequently denied.
                        self._chat_observe_scope(db, p, now)
                    if p is not None:
                        self._cancel_chat_invalid(db, now)
                    # Preserve authenticated revocation observations when an
                    # operation fails, without releasing the database lock
                    # between authorization and the requested operation.
                    db.execute("SAVEPOINT requested_operation")
                    observed = True
                    if (p is not None and p["browser_reader"] is not None and not p["is_admin"]
                            and request["op"] != "snapshot" and request["op"] not in CHAT_OPS
                            and not (request["op"] in ACCOUNT_OPS and self._account_user(p))):
                        raise BusError("this browser reader has read-only directory access", "forbidden")
                    if request["op"] == "redeem":
                        result = self._redeem(db, p, token, request, now)
                        self._expire(db, now)
                    else:
                        if p is None:
                            raise BusError("authentication required", "unauthorized")
                        self._expire(db, now)
                        if request["op"] in CHAT_OPS:
                            self._chat_access(db, p)
                            if set(request) - CHAT_FIELDS[request["op"]] - {"op"}:
                                raise BusError("unexpected chat request fields")
                        fn = getattr(self, "_op_" + request["op"], None)
                        if fn is None:
                            raise BusError("unknown operation")
                        result = fn(db, p, request, now)
                    db.commit()
                    return {"ok": True, **result}
                except Exception:
                    if observed:
                        db.execute("ROLLBACK TO requested_operation")
                        db.commit()
                    else:
                        db.rollback()
                    raise
                finally:
                    db.close()
        except BusError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        except (TypeError, ValueError, OverflowError, RecursionError):
            return {"ok": False, "error": "invalid request", "code": "invalid_request"}

    def _op_create(self, db, p, r, now):
        user = self._account_user(p)
        if not p["is_admin"] and user is None:
            raise BusError("authenticated account access required", "forbidden")
        bus = _bus(r.get("bus"))
        existing = self._bus_definition(db, bus)
        if existing is not None:
            if not p["is_admin"] and (bus == "general" or existing["owner_user"] != user):
                raise BusError("bus name is already in use", "conflict")
            return {"bus": bus, "owner_user": existing["owner_user"]}
        if db.execute("SELECT COUNT(*) FROM buses").fetchone()[0] >= MAX_BUSES:
            raise BusError("bus limit reached", "limit")
        # Operator-created buses retain the existing operator-owned behavior.
        owner = None if p["is_admin"] else user
        if owner and db.execute("SELECT COUNT(*) FROM bus_ownership WHERE user=?", (owner,)).fetchone()[0] >= MAX_ACCOUNT_BUSES:
            raise BusError("account bus limit reached", "limit")
        db.execute("INSERT INTO buses(name,visibility) VALUES(?,'private')", (bus,))
        if owner:
            db.execute("INSERT INTO bus_ownership(bus,user) VALUES(?,?)", (bus, owner))
        return {"bus": bus, "owner_user": owner}

    def _op_member_add(self, db, p, r, now):
        bus = self._managed_bus(db, p, _bus(r.get("bus")))
        if bus["owner_user"] is None or bus["name"] == "general":
            raise BusError("account membership requires an account-owned private bus", "forbidden")
        user = _user(r.get("user"))
        if user not in self.users:
            raise BusError("unknown account", "forbidden")
        role = "owner" if user == bus["owner_user"] else "member"
        if role == "member":
            db.execute("INSERT OR IGNORE INTO account_memberships VALUES(?,?)", (user, bus["name"]))
        return {"bus": bus["name"], "user": user, "role": role, "added": True}

    def _op_member_remove(self, db, p, r, now):
        bus = self._managed_bus(db, p, _bus(r.get("bus")), member=True)
        user = _user(r.get("user"))
        if bus["owner_user"] is None or bus["name"] == "general" or user == bus["owner_user"]:
            raise BusError("the bus owner or operator-managed membership cannot be removed", "forbidden")
        if self._bus_role(db, p, bus) == "member" and user != self._account_user(p):
            raise BusError("members may only leave for their own account", "forbidden")
        name = bus["name"]
        db.execute("DELETE FROM account_memberships WHERE bus=? AND user=?", (name, user))
        # Remove this bus only. The same devices and agents can belong elsewhere.
        db.execute("DELETE FROM grants WHERE bus=? AND principal IN (SELECT id FROM principals WHERE user=?)", (name, user))
        db.execute("""DELETE FROM memberships WHERE bus=? AND agent IN (
                      SELECT a.id FROM agents a JOIN principals p ON p.id=a.principal WHERE p.user=?)""", (name, user))
        db.execute("UPDATE invites SET expires_at=MIN(expires_at,?) WHERE bus=? AND user=? AND redeemed_at IS NULL", (now, name, user))
        # Account access can also underpin an explicitly configured human chat.
        # Close it immediately; a later re-add must not resurrect a reply window.
        for reader, account in self.reader_users.items():
            if account == user:
                for row in db.execute("""SELECT m.id FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                                         WHERE c.bus=? AND m.sender_reader=? AND m.closed=0""", (name, reader)).fetchall():
                    self._chat_close(db, row["id"], now)
        self._cancel_invalid(db, now)
        return {"bus": name, "user": user, "removed": True}

    def _op_invite(self, db, p, r, now):
        bus = _bus(r.get("bus", "general"))
        managed = self._managed_bus(db, p, bus, member=True)
        ttl = r.get("ttl", 3600)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 60 <= ttl <= 604800:
            raise BusError("invite ttl must be 60 to 604800 seconds")
        if db.execute("SELECT COUNT(*) FROM invites WHERE redeemed_at IS NULL AND expires_at>?", (now,)).fetchone()[0] >= MAX_INVITES:
            raise BusError("outstanding invitation limit reached", "limit")
        account = self._account_user(p)
        user = r.get("user", account if not p["is_admin"] else (None if self.users else p["user"]))
        if user is None:
            raise BusError("select the user who will own this device")
        user = _user(user)
        if self.users and user not in self.users:
            raise BusError("unknown invitation user", "forbidden")
        if managed["owner_user"] is not None and not self._account_member(db, bus, user):
            raise BusError("add the account to this bus before enrolling its device", "forbidden")
        if self._bus_role(db, p, managed) == "member" and user != account:
            raise BusError("members may only enroll their own devices", "forbidden")
        if (not p["is_admin"] and db.execute("""SELECT COUNT(*) FROM invites WHERE issuer_user=?
              AND redeemed_at IS NULL AND expires_at>?""", (account, now)).fetchone()[0] >= MAX_ACCOUNT_INVITES):
            raise BusError("account invitation limit reached", "limit")
        secret = secrets.token_urlsafe(32)
        db.execute("INSERT INTO invites(digest,bus,expires_at,user,issuer_user) VALUES(?,?,?,?,?)",
                   (_digest(secret), bus, now + ttl, user, account))
        return {"invite": secret, "bus": bus, "user": user, "expires_at": now + ttl}

    def _op_invite_revoke(self, db, p, r, now):
        secret = _text(r.get("invite"), "invite", 512)
        invitation = db.execute("SELECT * FROM invites WHERE digest=?", (_digest(secret),)).fetchone()
        if invitation is None or invitation["redeemed_at"] is not None:
            raise BusError("unknown or already redeemed invitation", "not_found")
        bus = self._managed_bus(db, p, invitation["bus"], member=True)
        if self._bus_role(db, p, bus) == "member" and invitation["user"] != self._account_user(p):
            raise BusError("members may only revoke invitations for their own devices", "forbidden")
        db.execute("UPDATE invites SET expires_at=MIN(expires_at,?) WHERE digest=?", (now, _digest(secret)))
        return {"revoked": True, "bus": invitation["bus"]}

    def _event_record(self, db, event, now, *, participants=False):
        result = {key: event[key] for key in ("id", "bus", "created_at", "expires_at", "max_uses", "uses")}
        result["revoked"] = bool(event["revoked"])
        result["active"] = not event["revoked"] and event["expires_at"] > now and event["uses"] < event["max_uses"]
        if participants:
            result["participants"] = [{"principal": row["principal"], "user": row["user"], "device": row["device"],
                                       "joined_at": row["joined_at"],
                                       "removed": bool(row["removed"] or row["revoked"] or
                                                       not self._granted(db, row["principal"], event["bus"]))}
                                      for row in db.execute("""SELECT j.*,p.user,p.device,p.revoked FROM event_joins j
                                        JOIN principals p ON p.id=j.principal WHERE j.event=?
                                        ORDER BY j.joined_at,j.principal LIMIT 100""", (event["id"],))]
        return result

    def _managed_event(self, db, p, value):
        event = db.execute("SELECT * FROM event_invites WHERE id=?", (_text(value, "event", 128),)).fetchone()
        if event is None:
            raise BusError("unknown event", "not_found")
        self._managed_bus(db, p, event["bus"])
        return event

    def _op_event_create(self, db, p, r, now):
        bus = self._managed_bus(db, p, _bus(r.get("bus")))
        if bus["name"] == "general" or bus["visibility"] != "private":
            raise BusError("event codes require a private bus", "forbidden")
        ttl, limit = r.get("ttl", 14400), r.get("max_uses", 40)
        if type(ttl) is not int or not 60 <= ttl <= 86400:
            raise BusError("event ttl must be 60 to 86400 integer seconds")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BusError("event device limit must be an integer from 1 to 100")
        if (db.execute("SELECT COUNT(*) FROM event_invites").fetchone()[0] >= MAX_RECORDS
                or db.execute("""SELECT COUNT(*) FROM event_invites WHERE bus=? AND revoked=0
                    AND expires_at>? AND uses<max_uses""", (bus["name"], now)).fetchone()[0] >= MAX_ACTIVE_EVENTS_PER_BUS):
            raise BusError("event invitation limit reached", "limit")
        event, secret = "e_" + uuid.uuid4().hex, "event1." + secrets.token_urlsafe(32)
        # Separate from personal invites: an older broker must reject a shared
        # code, never redeem it as a one-use invitation attributed to an owner.
        db.execute("""INSERT INTO event_invites(id,digest,bus,created_at,expires_at,max_uses,issuer_user)
                      VALUES(?,?,?,?,?,?,?)""", (event, _digest(secret), bus["name"], now, now + ttl, limit, self._account_user(p)))
        row = db.execute("SELECT * FROM event_invites WHERE id=?", (event,)).fetchone()
        return {"event": self._event_record(db, row, now, participants=True), "invite": secret,
                "bus": bus["name"], "expires_at": now + ttl}

    def _op_event_get(self, db, p, r, now):
        event = self._managed_event(db, p, r.get("event"))
        # Participant details belong to one explicitly selected, owned event;
        # returning every historical list in snapshots exceeds released clients'
        # response cap. Admission already limits each event to 100 devices.
        return {"event": self._event_record(db, event, now, participants=True)}

    def _op_event_revoke(self, db, p, r, now):
        event = self._managed_event(db, p, r.get("event"))
        db.execute("UPDATE event_invites SET revoked=1 WHERE id=?", (event["id"],))
        # Admission closes; already enrolled devices deliberately keep access.
        return {"event": event["id"], "bus": event["bus"], "revoked": True}

    def _op_event_remove(self, db, p, r, now):
        event = self._managed_event(db, p, r.get("event"))
        principal = _text(r.get("principal"), "principal", 128)
        joined = db.execute("SELECT 1 FROM event_joins WHERE event=? AND principal=?", (event["id"], principal)).fetchone()
        if joined is None:
            raise BusError("unknown event participant", "not_found")
        # Block replay through any event that already admitted this same device
        # on this bus. A removed device must not restore the grant via another
        # idempotent retry. Other buses and the device credential stay intact.
        db.execute("""UPDATE event_joins SET removed=1 WHERE principal=? AND event IN
                      (SELECT id FROM event_invites WHERE bus=?)""", (principal, event["bus"]))
        db.execute("DELETE FROM grants WHERE principal=? AND bus=?", (principal, event["bus"]))
        db.execute("DELETE FROM memberships WHERE bus=? AND agent IN (SELECT id FROM agents WHERE principal=?)",
                   (event["bus"], principal))
        self._cancel_invalid(db, now)
        return {"event": event["id"], "principal": principal, "bus": event["bus"], "removed": True}

    def _redeem_event(self, db, p, token, r, event, now):
        if event["revoked"] or event["expires_at"] <= now:
            raise BusError("event code is expired or closed", "forbidden")
        if p is not None and (p["browser_reader"] is not None or p["is_admin"]):
            raise BusError("event codes enroll participant devices, not browser or operator credentials", "forbidden")
        device = _text(r.get("device", "device"), "device", 128)
        metadata = _device_metadata(r.get("device_metadata", {}))
        joined = None if p is None else db.execute("SELECT * FROM event_joins WHERE event=? AND principal=?",
                                                   (event["id"], p["id"])).fetchone()
        if joined is not None:
            if joined["removed"] or not self._granted(db, p["id"], event["bus"]):
                raise BusError("this device's event access was removed", "forbidden")
            # Retrying an authenticated join uses no extra slot, and can never
            # restore a removed grant or implicitly republish an agent.
            return self._enrollment_result(db, p["id"], token)
        if event["uses"] >= event["max_uses"]:
            raise BusError("event device limit reached", "limit")
        if db.execute("SELECT COUNT(*) FROM event_joins").fetchone()[0] >= MAX_RECORDS:
            raise BusError("event enrollment record limit reached", "limit")
        if p is None:
            if db.execute("SELECT COUNT(*) FROM principals").fetchone()[0] >= MAX_PRINCIPALS:
                raise BusError("device enrollment limit reached", "limit")
            principal, user, token = "p_" + uuid.uuid4().hex, "guest-" + uuid.uuid4().hex, secrets.token_urlsafe(32)
            db.execute("INSERT INTO principals(id,device,created_at,user,device_metadata) VALUES(?,?,?,?,?)",
                       (principal, device, now, user, json.dumps(metadata)))
            db.execute("INSERT INTO tokens VALUES(?,?)", (_digest(token), principal))
        else:
            # The authenticated bearer, never a claimed login/device name,
            # establishes this existing principal's account and device label.
            principal = p["id"]
            if "device_metadata" in r:
                db.execute("UPDATE principals SET device_metadata=? WHERE id=?", (json.dumps(metadata), principal))
        db.execute("INSERT OR IGNORE INTO grants VALUES(?,?)", (principal, event["bus"]))
        db.execute("INSERT INTO event_joins(event,principal,joined_at) VALUES(?,?,?)", (event["id"], principal, now))
        db.execute("UPDATE event_invites SET uses=uses+1 WHERE id=?", (event["id"],))
        return self._enrollment_result(db, principal, token)

    def _enrollment_result(self, db, principal, token):
        current = db.execute("SELECT * FROM principals WHERE id=?", (principal,)).fetchone()
        buses = ([b[0] for b in db.execute("SELECT name FROM buses ORDER BY name")] if current["is_admin"] else
                 [b[0] for b in db.execute("SELECT bus FROM grants WHERE principal=? ORDER BY bus", (principal,))])
        return {"token": token, "principal": principal, "buses": buses, "server_id": self.server_id, **self._device_info(current)}

    def _redeem(self, db, p, token, r, now):
        secret = _text(r.get("invite"), "invite", 512)
        event = db.execute("SELECT * FROM event_invites WHERE digest=?", (_digest(secret),)).fetchone()
        if event is not None:
            return self._redeem_event(db, p, token, r, event, now)
        invite = db.execute("SELECT * FROM invites WHERE digest=? AND redeemed_at IS NULL AND expires_at>?",
                            (_digest(secret), now)).fetchone()
        if invite is None:
            raise BusError("invite is invalid, expired, or already used", "forbidden")
        if invite["user"] is None and self.users:
            raise BusError("this older invitation has no user; request a new invitation", "forbidden")
        if self.users and invite["user"] not in self.users:
            raise BusError("invitation user is no longer configured", "forbidden")
        bus = self._bus_definition(db, invite["bus"])
        if bus["owner_user"] is not None and not self._account_member(db, bus["name"], invite["user"]):
            raise BusError("invitation account is no longer a bus member", "forbidden")
        device = _text(r.get("device", "device"), "device", 128)
        metadata = _device_metadata(r.get("device_metadata", {}))
        if p is None:
            if db.execute("SELECT COUNT(*) FROM principals").fetchone()[0] >= MAX_PRINCIPALS:
                raise BusError("device enrollment limit reached", "limit")
            principal = "p_" + uuid.uuid4().hex
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO principals(id,device,created_at,user,device_metadata) VALUES(?,?,?,?,?)",
                       (principal, device, now, invite["user"], json.dumps(metadata)))
            db.execute("INSERT INTO tokens VALUES(?,?)", (_digest(token), principal))
        else:
            principal = p["id"]
            if p["browser_reader"] is not None:
                raise BusError("enroll a device separately from a browser session", "forbidden")
            if p["user"] is not None and invite["user"] != p["user"]:
                raise BusError("invitation belongs to a different user; use a separate installation", "forbidden")
            db.execute("UPDATE principals SET user=COALESCE(user,?) WHERE id=?", (invite["user"], principal))
            if "device_metadata" in r:
                db.execute("UPDATE principals SET device_metadata=? WHERE id=?", (json.dumps(metadata), principal))
        db.execute("INSERT OR IGNORE INTO grants VALUES(?,?)", (principal, invite["bus"]))
        db.execute("UPDATE invites SET redeemed_at=?,principal=? WHERE digest=?", (now, principal, _digest(secret)))
        return self._enrollment_result(db, principal, token)

    def _op_device(self, db, p, r, now):
        if p["browser_reader"] is not None:
            raise BusError("this operation updates an enrolled device", "forbidden")
        if "user" in r or "owner" in r:
            raise BusError("device ownership is assigned by its invitation", "forbidden")
        if "device" in r:
            db.execute("UPDATE principals SET device=? WHERE id=?", (_text(r["device"], "device", 128), p["id"]))
        if "device_metadata" in r:
            db.execute("UPDATE principals SET device_metadata=? WHERE id=?",
                       (json.dumps(_device_metadata(r["device_metadata"])), p["id"]))
        return {"principal": p["id"], **self._device_info(db.execute("SELECT * FROM principals WHERE id=?", (p["id"],)).fetchone())}

    def _op_register(self, db, p, r, now):
        bus = _bus(r.get("bus", "general"))
        if not self._granted(db, p["id"], bus) or not db.execute("SELECT 1 FROM buses WHERE name=?", (bus,)).fetchone():
            raise BusError("bus membership has not been granted", "forbidden")
        return self._identify(db, p, r, now, publish=bus)

    def _op_identify(self, db, p, r, now):
        # An internal reply adapter is not directory membership. One device
        # credential admits its local agents to outbound general traffic.
        if not self._granted(db, p["id"], "general"):
            raise BusError("device has not been admitted to general", "forbidden")
        return self._identify(db, p, r, now)

    def _identify(self, db, p, r, now, publish=None):
        if p["browser_reader"] is not None:
            raise BusError("agents connect through an enrolled device", "forbidden")
        if "device_metadata" in r:
            db.execute("UPDATE principals SET device_metadata=? WHERE id=?",
                       (json.dumps(_device_metadata(r["device_metadata"])), p["id"]))
        session_key = _text(r.get("session_key"), "session_key", 256)
        name = _text(r.get("name"), "name", 128)
        kind = r.get("kind", "claude")
        if kind not in ("claude", "codex", "service"):
            raise BusError("kind must be claude, codex or service")
        description = _text(r.get("description", ""), "description", 2048, optional=True)
        status = r.get("status", "queueable" if kind == "codex" else "live")
        if status not in ("live", "queueable", "offline"):
            raise BusError("status must be live, queueable, or offline")
        existing = db.execute("SELECT id FROM agents WHERE principal=? AND session_key=?", (p["id"], session_key)).fetchone()
        if existing:
            agent = existing[0]
            db.execute("UPDATE agents SET name=?,kind=?,description=?,status=?,last_seen=? WHERE id=?",
                       (name, kind, description, status, now, agent))
        else:
            count = db.execute("SELECT COUNT(*) FROM agents WHERE principal=?", (p["id"],)).fetchone()[0]
            if count >= MAX_AGENTS or db.execute("SELECT COUNT(*) FROM agents").fetchone()[0] >= MAX_TOTAL_AGENTS:
                raise BusError("agent registration limit reached", "limit")
            agent = "a_" + uuid.uuid4().hex
            db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)",
                       (agent, p["id"], session_key, name, kind, description, status, now))
        if publish:
            db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?)", (agent, publish))
        buses = [b[0] for b in db.execute("SELECT bus FROM memberships WHERE agent=? ORDER BY bus", (agent,))]
        return {"id": agent, "agent": agent, "name": name, "bus": publish, "buses": buses,
                "principal": p["id"], "status": status, "last_seen": now, "server_id": self.server_id,
                **self._device_info(db.execute("SELECT * FROM principals WHERE id=?", (p["id"],)).fetchone())}

    def _op_heartbeat(self, db, p, r, now):
        rows = r.get("agents")
        if not isinstance(rows, list) or len(rows) > MAX_AGENTS:
            raise BusError("agents must be a bounded list")
        for entry in rows:
            if not isinstance(entry, dict) or entry.get("status") not in ("live", "queueable", "offline"):
                raise BusError("invalid heartbeat")
            agent = self._owned(db, p, entry.get("id"))
            db.execute("UPDATE agents SET status=?,last_seen=? WHERE id=?", (entry["status"], now, agent["id"]))
        return {"updated": len(rows), "ts": now}

    def _op_snapshot(self, db, p, r, now):
        allowed = self._visible_buses(db, p)
        buses = []
        for bus in allowed:
            definition = self._bus_definition(db, bus)
            agents = []
            for a in db.execute("""SELECT a.*,p.device,p.user,p.device_metadata FROM agents a JOIN memberships m ON m.agent=a.id
                                   JOIN principals p ON p.id=a.principal WHERE m.bus=? AND p.revoked=0
                                   ORDER BY a.name,a.id""", (bus,)):
                if not self._granted(db, a["principal"], bus):
                    continue
                memberships = [b[0] for b in db.execute("SELECT bus FROM memberships WHERE agent=? ORDER BY bus", (a["id"],))
                               if b[0] in allowed]
                row = {"id": a["id"], "name": a["name"], "kind": a["kind"], "device": a["device"],
                       "user": a["user"], "device_id": a["principal"], "device_metadata": json.loads(a["device_metadata"]),
                       "description": a["description"], "last_seen": a["last_seen"], "buses": memberships,
                       "status": a["status"] if now - a["last_seen"] <= LIVE_TTL else "offline"}
                if p["is_admin"]:
                    row["principal"] = a["principal"]
                agents.append(row)
            role = self._bus_role(db, p, definition)
            account_owned = definition["owner_user"] is not None and bus != "general"
            manage_members = account_owned and role in ("admin", "owner")
            event_join = bus != "general" and definition["visibility"] == "private" and role in ("admin", "owner")
            row = {"name": bus, "visibility": definition["visibility"], "agents": agents,
                   "owner_user": definition["owner_user"], "role": role,
                   "capabilities": {"invite": role == "admin" or (account_owned and role in ("owner", "member")),
                                    "manage_members": manage_members, "leave": account_owned and role == "member",
                                    "event_join": event_join}}
            if manage_members:
                row["members"] = [{"user": definition["owner_user"], "role": "owner"}] + [
                    {"user": member[0], "role": "member"} for member in db.execute(
                        "SELECT user FROM account_memberships WHERE bus=? ORDER BY user", (bus,))]
            if event_join:
                # Bounded recent summaries; participant lists are available only
                # through event_get, including for older exact event IDs. Neither
                # path exposes a plaintext admission code or token digest.
                row["events"] = [self._event_record(db, event, now) for event in db.execute(
                    "SELECT * FROM event_invites WHERE bus=? ORDER BY created_at DESC,id DESC LIMIT ?",
                    (bus, MAX_EVENT_HISTORY))]
            buses.append(row)
        self._snapshot_graphs(db, buses)
        result = {"server_id": self.server_id, "is_admin": bool(p["is_admin"]),
                  "can_create_bus": bool(p["is_admin"] or self._account_user(p)),
                  "principal": p["id"], "buses": buses, "ts": now, **self._device_info(p)}
        chat_buses = self._chat_buses(db, p)
        result["chat"] = {"enabled": bool(chat_buses), "buses": chat_buses,
                          "openwebui": [{"bus": bus, "agent": agent} for bus, agent in self.chat_openwebui_targets
                                        if bus in chat_buses and self._member(db, agent, bus)]}
        if p.get("browser_reader") is not None:
            result.update(browser_session=True, read_only=not result["can_create_bus"], logout_url="/_gateway/logout")
            if "display_name" in p:
                result["display_name"] = p["display_name"]
        if p["is_admin"]:
            principals = []
            for ent in db.execute("""SELECT p.* FROM principals p WHERE NOT EXISTS (
                    SELECT 1 FROM tokens t JOIN browser_credentials b ON b.digest=t.digest
                    WHERE t.principal=p.id) ORDER BY p.created_at,p.id"""):
                row = dict(ent)
                row.update(self._device_info(ent))
                row["is_admin"] = bool(row["is_admin"])
                row["revoked"] = bool(row["revoked"])
                row["buses"] = allowed if ent["is_admin"] else [b[0] for b in db.execute(
                    "SELECT bus FROM grants WHERE principal=? ORDER BY bus", (ent["id"],))]
                principals.append(row)
            result["principals"] = principals
        if self.users and result["can_create_bus"]:
            # Account IDs only for explicit collaborator selection. Device and
            # cross-bus membership records remain restricted to administrators.
            result["users"] = [{"id": user, **({"label": self.account_labels[user]} if user in self.account_labels else {})}
                               for user in self.users]
        return result

    @staticmethod
    def _snapshot_graphs(db, buses):
        """Only observed traffic between currently visible, same-bus agents.

        A general-bus initiator can be unpublished. Filtering by the bus alone
        would expose that hidden identity, including through replies. Reuse the
        snapshot's admitted endpoints before exposing any communication metadata.
        The one grouped query is bounded by the broker's global MAX_RECORDS
        message limit, even when a reader can view many buses. Message bodies,
        receipt details and delivery capabilities never enter the graph.
        """
        visible = {bus["name"]: {agent["id"] for agent in bus["agents"]} for bus in buses}
        grouped = {bus["name"]: {} for bus in buses}
        if buses:
            slots = ",".join("?" for _ in buses)
            rows = db.execute("""SELECT bus,sender,target,status,COUNT(*) AS message_count,
                                 MAX(created_at) AS last_sent_at FROM messages
                                 WHERE bus IN (%s) GROUP BY bus,sender,target,status""" % slots,
                              tuple(visible))
            for row in rows:
                if row["sender"] not in visible[row["bus"]] or row["target"] not in visible[row["bus"]]:
                    continue
                pair = (row["sender"], row["target"])
                edge = grouped[row["bus"]].setdefault(pair, {
                    "source": row["sender"], "target": row["target"], "message_count": 0,
                    "last_sent_at": row["last_sent_at"], "status_counts": {},
                })
                edge["message_count"] += row["message_count"]
                edge["last_sent_at"] = max(edge["last_sent_at"], row["last_sent_at"])
                edge["status_counts"][row["status"]] = row["message_count"]
        for bus in buses:
            # Retention starts at a terminal receipt's last update. This is
            # retained activity, not a claim to be a complete seven-day history.
            bus["graph"] = {"edges": [edge for _, edge in sorted(grouped[bus["name"]].items())],
                            "retention_seconds": RECEIPT_RETENTION, "basis": "retained_bus_messages"}

    def _op_members(self, db, p, r, now):
        self._admin(p)
        return self._op_snapshot(db, p, r, now)

    def _chat_buses(self, db, p):
        reader = p.get("browser_reader")
        grant = self.chat_readers.get(reader)
        if not reader or not grant:
            return []
        visible = set(self._visible_buses(db, p))
        return sorted(visible.intersection(grant["buses"]))

    def _chat_access(self, db, p):
        reader = p.get("browser_reader")
        grant = self.chat_readers.get(reader)
        if not reader or not grant:
            raise BusError("human chat is not enabled for this reader", "forbidden")
        return grant["identity"], self._chat_buses(db, p)

    @staticmethod
    def _chat_close(db, message, now):
        db.execute("""UPDATE human_chat_messages SET closed=1,
                      status=CASE WHEN status IN ('accepted','leased') THEN 'cancelled' ELSE status END,
                      detail='chat access changed',updated_at=? WHERE id=?""", (now, message))

    def _chat_observe_scope(self, db, p, now):
        """Do not persist a view as future authorization. Only remember losses.

        Agent requests cannot consult current OWUI groups. Their reply windows
        remain bounded by 24 hours, current broker config/publication, and any
        scope loss observed on a subsequent authenticated browser request.
        """
        buses = self._chat_buses(db, p)
        grant = self.chat_readers.get(p["browser_reader"])
        for row in db.execute("""SELECT m.id,c.bus,c.identity FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                                 WHERE m.role='user' AND m.closed=0 AND m.sender_reader=?""", (p["browser_reader"],)).fetchall():
            if not grant or row["identity"] != grant["identity"] or row["bus"] not in buses:
                self._chat_close(db, row["id"], now)

    def _cancel_chat_invalid(self, db, now):
        for row in db.execute("""SELECT m.*,c.bus,c.agent FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                                 WHERE m.role='user' AND m.closed=0""").fetchall():
            grant = self.chat_readers.get(row["sender_reader"])
            if row["expires_at"] <= now:
                db.execute("""UPDATE human_chat_messages SET closed=1,updated_at=?,
                              detail=CASE WHEN status IN ('accepted','leased') THEN 'message expired' ELSE detail END,
                              status=CASE WHEN status IN ('accepted','leased') THEN 'expired' ELSE status END WHERE id=?""",
                           (now, row["id"]))
                continue
            if (not grant or grant["identity"] != row["identity"] or row["bus"] not in grant["buses"]
                    or not self._member(db, row["agent"], row["bus"])):
                self._chat_close(db, row["id"], now)

    def _chat_owned(self, db, p, chat):
        identity, buses = self._chat_access(db, p)
        if not isinstance(chat, str):
            raise BusError("unknown or unavailable chat", "not_found")
        row = db.execute("SELECT * FROM human_chats WHERE id=? AND identity=?", (chat, identity)).fetchone()
        if row is None or row["bus"] not in buses or not self._member(db, row["agent"], row["bus"]):
            raise BusError("unknown or unavailable chat", "not_found")
        return row

    @staticmethod
    def _chat_message(row):
        result = {key: row[key] for key in ("id", "seq", "role", "content", "created_at", "status")}
        for key in ("in_reply_to", "request_id"):
            if row[key] is not None:
                result[key] = row[key]
        return result

    def _chat_summary(self, db, chat, now):
        agent = db.execute("""SELECT a.id,a.name,a.kind,a.status,a.last_seen,p.user,p.device,p.id AS device_id
                              FROM agents a JOIN principals p ON p.id=a.principal WHERE a.id=?""", (chat["agent"],)).fetchone()
        public = {key: agent[key] for key in ("id", "name", "kind", "user", "device", "device_id", "status")}
        if now - agent["last_seen"] > LIVE_TTL:
            public["status"] = "offline"
        unread = db.execute("SELECT COUNT(*) FROM human_chat_messages WHERE chat=? AND role='assistant' AND seq>?",
                            (chat["id"], chat["read_seq"])).fetchone()[0]
        return {"id": chat["id"], "bus": chat["bus"], "agent": public,
                "created_at": chat["created_at"], "updated_at": chat["updated_at"],
                "unread": unread, "can_send": self._member(db, chat["agent"], chat["bus"])}

    def _op_chat_open(self, db, p, r, now):
        identity, buses = self._chat_access(db, p)
        bus, agent = _bus(r.get("bus")), r.get("agent")
        if bus not in buses or not isinstance(agent, str) or not self._member(db, agent, bus):
            raise BusError("unknown or unavailable recipient", "not_found")
        chat = db.execute("SELECT * FROM human_chats WHERE identity=? AND bus=? AND agent=?", (identity, bus, agent)).fetchone()
        if chat is None:
            if (db.execute("SELECT COUNT(*) FROM human_chats").fetchone()[0] >= MAX_CHATS
                    or db.execute("SELECT COUNT(*) FROM human_chats WHERE identity=?", (identity,)).fetchone()[0] >= MAX_CHATS_PER_IDENTITY):
                raise BusError("human chat limit reached", "limit")
            cid = "hc_" + uuid.uuid4().hex
            db.execute("INSERT INTO human_chats(id,identity,bus,agent,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (cid, identity, bus, agent, now, now))
            chat = db.execute("SELECT * FROM human_chats WHERE id=?", (cid,)).fetchone()
        return {"chat": self._chat_summary(db, chat, now)}

    def _op_chat_list(self, db, p, r, now):
        identity, buses = self._chat_access(db, p)
        self._cancel_chat_invalid(db, now)
        chats = db.execute("SELECT * FROM human_chats WHERE identity=? ORDER BY updated_at DESC,id", (identity,))
        return {"chats": [self._chat_summary(db, chat, now) for chat in chats
                          if chat["bus"] in buses and self._member(db, chat["agent"], chat["bus"])]}

    def _op_chat_messages(self, db, p, r, now):
        chat = self._chat_owned(db, p, r.get("chat"))
        after, limit = r.get("after", 0), r.get("limit", 100)
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise BusError("invalid chat pagination")
        self._cancel_chat_invalid(db, now)
        rows = db.execute("SELECT * FROM human_chat_messages WHERE chat=? AND seq>? ORDER BY seq LIMIT ?",
                          (chat["id"], after, limit + 1)).fetchall()
        result = {"chat": self._chat_summary(db, chat, now), "messages": [], "has_more": bool(rows), "next_after": after}
        for row in rows[:limit]:
            result["messages"].append(self._chat_message(row))
            result.update(has_more=len(rows) > len(result["messages"]), next_after=row["seq"])
            # Match the HTTP encoder, including its Unicode/control escaping
            # and the handle() success wrapper, so every page fits the gateway.
            if len(json.dumps({"ok": True, **result}).encode("utf-8")) > MAX_CHAT_PAGE_BYTES:
                result["messages"].pop()
                if not result["messages"]:
                    raise BusError("chat message exceeds the response limit", "too_large")
                result.update(has_more=True, next_after=result["messages"][-1]["seq"])
                break
        self._chat_mark_seen(db, chat, p["browser_reader"], [row["seq"] for row in result["messages"]])
        return result

    def _op_chat_read(self, db, p, r, now):
        chat = self._chat_owned(db, p, r.get("chat"))
        through = r.get("through")
        if type(through) is not int or not 0 <= through < chat["next_seq"]:
            raise BusError("invalid read position")
        if db.execute("""SELECT 1 FROM human_chat_messages m WHERE m.chat=? AND m.seq>? AND m.seq<=?
                         AND NOT EXISTS (SELECT 1 FROM human_chat_seen s WHERE s.chat=m.chat AND s.seq=m.seq AND s.reader=?)""",
                      (chat["id"], chat["read_seq"], through, p["browser_reader"])).fetchone():
            raise BusError("only fetched messages can be marked read", "conflict")
        db.execute("UPDATE human_chats SET read_seq=MAX(read_seq,?) WHERE id=?", (through, chat["id"]))
        db.execute("DELETE FROM human_chat_seen WHERE chat=? AND seq<=?", (chat["id"], max(through, chat["read_seq"])))
        unread = db.execute("SELECT COUNT(*) FROM human_chat_messages WHERE chat=? AND role='assistant' AND seq>?",
                            (chat["id"], max(through, chat["read_seq"]))).fetchone()[0]
        return {"unread": unread}

    @staticmethod
    def _chat_mark_seen(db, chat, reader, sequences):
        unseen = [seq for seq in sequences if seq > chat["read_seq"] and not db.execute(
            "SELECT 1 FROM human_chat_seen WHERE chat=? AND reader=? AND seq=?", (chat["id"], reader, seq)).fetchone()]
        if unseen and db.execute("SELECT COUNT(*) FROM human_chat_seen").fetchone()[0] + len(unseen) > MAX_CHAT_SEEN:
            raise BusError("chat read tracking limit reached", "limit")
        db.executemany("INSERT INTO human_chat_seen VALUES(?,?,?)", [(chat["id"], reader, seq) for seq in unseen])

    @staticmethod
    def _message_content(message):
        if not isinstance(message, str) or not message.strip() or len(message.encode("utf-8")) > MAX_MESSAGE or "\x00" in message:
            raise BusError("message must contain 1 to %d UTF-8 bytes" % MAX_MESSAGE)
        return message

    @staticmethod
    def _chat_capacity(db, chat):
        if (db.execute("SELECT COUNT(*) FROM human_chat_messages").fetchone()[0] >= MAX_CHAT_MESSAGES
                or db.execute("SELECT COUNT(*) FROM human_chat_messages WHERE chat=?", (chat,)).fetchone()[0] >= MAX_CHAT_MESSAGES_PER_CHAT):
            raise BusError("human chat history is full", "limit")

    @staticmethod
    def _pending_count(db, target):
        return db.execute("""SELECT (SELECT COUNT(*) FROM messages WHERE target=? AND status IN ('accepted','leased')) +
                            (SELECT COUNT(*) FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                             WHERE c.agent=? AND m.role='user' AND m.status IN ('accepted','leased'))""", (target, target)).fetchone()[0]

    def _op_chat_send(self, db, p, r, now):
        chat = self._chat_owned(db, p, r.get("chat"))
        request_id, content = r.get("request_id"), self._message_content(r.get("message"))
        if not _canonical_uuid(request_id):
            raise BusError("request_id must be a canonical UUID")
        self._cancel_chat_invalid(db, now)
        old = db.execute("SELECT * FROM human_chat_messages WHERE identity=? AND request_id=?", (chat["identity"], request_id)).fetchone()
        if old:
            if old["chat"] != chat["id"] or old["content"] != content:
                raise BusError("request_id was already used for a different message", "conflict")
            self._chat_mark_seen(db, chat, p["browser_reader"], [old["seq"]])
            return {"chat": chat["id"], "message": self._chat_message(old), "deduplicated": True}
        self._chat_capacity(db, chat["id"])
        if self._pending_count(db, chat["agent"]) >= MAX_PENDING:
            raise BusError("recipient queue is full", "limit")
        mid = "hm_" + uuid.uuid4().hex
        db.execute("""INSERT INTO human_chat_messages(id,chat,identity,seq,role,content,created_at,updated_at,status,
                      request_id,sender_reader,expires_at) VALUES(?,?,?,?,'user',?,?,?,'accepted',?,?,?)""",
                   (mid, chat["id"], chat["identity"], chat["next_seq"], content, now, now, request_id, p["browser_reader"], now + MESSAGE_TTL))
        db.execute("UPDATE human_chats SET next_seq=next_seq+1,updated_at=? WHERE id=?", (now, chat["id"]))
        db.execute("INSERT INTO outbound_queue(message,target) VALUES(?,?)", (mid, chat["agent"]))
        self._chat_mark_seen(db, chat, p["browser_reader"], [chat["next_seq"]])
        return {"chat": chat["id"], "message": self._chat_message(db.execute("SELECT * FROM human_chat_messages WHERE id=?", (mid,)).fetchone()),
                "deduplicated": False}

    def _chat_reply(self, db, sender, mid, r, now):
        if any(field in r for field in ("target", "bus", "conversation", "chat", "identity")):
            raise BusError("reply destination and bus are fixed by the original message")
        self._cancel_chat_invalid(db, now)
        previous = db.execute("""SELECT m.* FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                                 WHERE m.id=? AND m.role='user' AND c.agent=?""", (mid, sender["id"])).fetchone()
        if (previous is None or previous["closed"] or previous["expires_at"] <= now
                or previous["status"] not in ("leased", "delivered", "queued")):
            raise BusError("unknown or unavailable conversation", "not_found")
        content = self._message_content(r.get("message"))
        chat = db.execute("SELECT * FROM human_chats WHERE id=?", (previous["chat"],)).fetchone()
        self._chat_capacity(db, chat["id"])
        reply = "hr_" + uuid.uuid4().hex
        db.execute("""INSERT INTO human_chat_messages(id,chat,identity,seq,role,content,created_at,updated_at,status,in_reply_to)
                      VALUES(?,?,?,?,'assistant',?,?,?,'replied',?)""",
                   (reply, chat["id"], chat["identity"], chat["next_seq"], content, now, now, mid))
        db.execute("UPDATE human_chats SET next_seq=next_seq+1,updated_at=? WHERE id=?", (now, chat["id"]))
        return {"id": reply, "status": "replied", "target": "human." + chat["identity"],
                "expires_at": previous["expires_at"], "conversation_expires_at": previous["expires_at"],
                "correlation_version": 1, "conversation_id": chat["id"], "in_reply_to": mid}

    def _chat_ack(self, db, agent, mid, lease, status, detail, now):
        self._cancel_chat_invalid(db, now)
        message = db.execute("""SELECT m.* FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                                WHERE m.id=? AND m.role='user' AND c.agent=?""", (mid, agent["id"])).fetchone()
        if message is None or not message["lease"] or not secrets.compare_digest(message["lease"], lease):
            raise BusError("unknown or unavailable message", "not_found")
        if not message["closed"] and message["status"] == status:
            return {"id": mid, "status": status}
        if message["closed"] or message["status"] != "leased" or message["lease_until"] <= now:
            raise BusError("delivery lease is no longer active", "conflict")
        db.execute("UPDATE human_chat_messages SET status=?,detail=?,updated_at=? WHERE id=?", (status, detail, now, mid))
        return {"id": mid, "status": status}

    def _op_send(self, db, p, r, now):
        sender = self._owned(db, p, r.get("sender"))
        if any(field in r for field in ("conversation_id", "in_reply_to", "correlation_version")):
            raise BusError("message correlation is assigned by the broker")
        bus = _bus(r.get("bus", "general"))
        target = _text(r.get("target"), "target", 128)
        if not self._granted(db, p["id"], bus) or (bus != "general" and not self._member(db, sender["id"], bus)):
            raise BusError("unknown or unavailable recipient", "not_found")
        candidates = db.execute("""SELECT a.id FROM agents a JOIN memberships m ON m.agent=a.id
                                  WHERE m.bus=? AND (a.id=? OR a.name=?) ORDER BY a.id""", (bus, target, target)).fetchall()
        ids = [c[0] for c in candidates if self._member(db, c[0], bus)]
        if target in ids:
            ids = [target]
        if not ids:
            raise BusError("unknown or unavailable recipient", "not_found")
        if len(ids) != 1:
            raise BusError("recipient name is ambiguous; use its agent id", "ambiguous")
        self._cancel_invalid(db, now)
        target = ids[0]
        if db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] >= MAX_RECORDS:
            raise BusError("conversation limit reached", "limit")
        conversation = "c_" + uuid.uuid4().hex
        expires_at = now + MESSAGE_TTL
        db.execute("INSERT INTO conversations VALUES(?,?,?,?,?,0)", (conversation, sender["id"], target, bus, expires_at))
        return self._enqueue(db, sender["id"], target, bus, r.get("message"), conversation, expires_at, now)

    def _op_reply(self, db, p, r, now):
        # Atomic with enqueue: a lost HTTP response must not duplicate an answer.
        # Existing clients omit request_id and retain their original semantics.
        sender = self._owned(db, p, r.get("sender"))
        if any(field in r for field in ("target", "bus", "conversation", "chat", "identity",
                                       "conversation_id", "in_reply_to", "correlation_version")):
            raise BusError("reply destination and bus are fixed by the original message")
        key = r.get("request_id")
        if key is None:
            return self._reply(db, p, r, now)
        key = _text(key, "reply request id", 128)
        mid = _text(r.get("id"), "message id", 128)
        digest = _digest(self._message_content(r.get("message")))
        previous = db.execute("SELECT * FROM reply_requests WHERE sender=? AND message=? AND request_id=?",
                              (sender["id"], mid, key)).fetchone()
        if previous:
            if previous["content_hash"] != digest:
                raise BusError("reply request id was reused with different content", "conflict")
            return json.loads(previous["result"])
        result = self._reply(db, p, r, now)
        db.execute("INSERT INTO reply_requests VALUES(?,?,?,?,?,?)",
                   (sender["id"], mid, key, digest, json.dumps(result), now))
        return result

    def _reply(self, db, p, r, now):
        sender = self._owned(db, p, r.get("sender"))
        mid = _text(r.get("id"), "message id", 128)
        if mid.startswith("hm_"):
            return self._chat_reply(db, sender, mid, r, now)
        # The message ID is not a bearer capability. The authenticated device
        # must own exactly the recipient of this already-fetched message.
        previous = db.execute("SELECT * FROM messages WHERE id=? AND target=?", (mid, sender["id"])).fetchone()
        if previous is None or previous["status"] not in ("leased", "delivered", "queued"):
            raise BusError("unknown or unavailable conversation", "not_found")
        if any(field in r for field in ("target", "bus", "conversation")):
            raise BusError("reply destination and bus are fixed by the original message")
        self._cancel_invalid(db, now)
        conversation = db.execute("SELECT * FROM conversations WHERE id=?", (previous["conversation"],)).fetchone()
        if not self._conversation_valid(db, conversation, now):
            raise BusError("conversation has ended or access changed", "not_found")
        return self._enqueue(db, sender["id"], previous["sender"], previous["bus"], r.get("message"),
                             conversation["id"], conversation["expires_at"], now, in_reply_to=mid)

    def _enqueue(self, db, sender, target, bus, message, conversation, expires_at, now, *, in_reply_to=None):
        self._message_content(message)
        pending = self._pending_count(db, target)
        if pending >= MAX_PENDING or db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] >= MAX_RECORDS:
            raise BusError("recipient queue is full", "limit")
        mid = "m_" + uuid.uuid4().hex
        db.execute("""INSERT INTO messages(id,sender,target,bus,message,status,created_at,expires_at,updated_at,conversation,
                      in_reply_to,correlation_version) VALUES(?,?,?,?,?,'accepted',?,?,?,?,?,1)""",
                   (mid, sender, target, bus, message, now, expires_at, now, conversation, in_reply_to))
        db.execute("INSERT INTO outbound_queue(message,target) VALUES(?,?)", (mid, target))
        return {"id": mid, "status": "accepted", "target": target, "expires_at": expires_at,
                "conversation_expires_at": expires_at, "correlation_version": 1,
                "conversation_id": conversation, "in_reply_to": in_reply_to}

    def _op_poll(self, db, p, r, now):
        # Leases permit crash recovery, not payload retraction: once fetched,
        # a client already has the message. Revocation blocks future fetches
        # and acknowledgments; it cannot undo a delivery already in progress.
        agent = self._owned(db, p, r.get("agent"))
        return self._poll(db, [agent["id"]], now, limit=1)

    def _op_poll_device(self, db, p, r, now):
        agents = r.get("agents")
        if not isinstance(agents, list) or not 1 <= len(agents) <= MAX_AGENTS:
            raise BusError("agents must be a nonempty bounded list of agent ids")
        # Validate all ownership before leasing anything; no partial fetch.
        owned = list(dict.fromkeys(self._owned(db, p, agent)["id"] for agent in agents))
        return self._poll(db, owned, now, limit=32)

    def _poll(self, db, agents, now, limit):
        self._cancel_invalid(db, now)
        slots = ",".join("?" for _ in agents)
        # The shared durable order prevents an agent delivery and a human
        # delivery from holding concurrent leases for one exact destination.
        rows = db.execute("""WITH pending AS (
                             SELECT q.*,COALESCE(m.status,h.status) AS status,
                                    COALESCE(m.lease_until,h.lease_until) AS lease_until
                             FROM outbound_queue q LEFT JOIN messages m ON m.id=q.message
                             LEFT JOIN human_chat_messages h ON h.id=q.message
                             WHERE COALESCE(m.status,h.status) IN ('accepted','leased'))
                             SELECT * FROM pending q WHERE q.target IN (%s)
                             AND (q.status='accepted' OR q.lease_until<=?)
                             AND NOT EXISTS (SELECT 1 FROM pending earlier WHERE earlier.target=q.target
                                             AND earlier.ordering<q.ordering)
                             ORDER BY q.ordering LIMIT ?""" % slots, (*agents, now, limit)).fetchall()
        messages = []
        for queued in rows:
            lease = secrets.token_urlsafe(18)
            if queued["message"].startswith("hm_"):
                m = db.execute("""SELECT m.*,c.bus,c.agent FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                                  WHERE m.id=?""", (queued["message"],)).fetchone()
                db.execute("UPDATE human_chat_messages SET status='leased',lease=?,lease_until=?,updated_at=? WHERE id=?",
                           (lease, now + LEASE_TTL, now, m["id"]))
                messages.append({"id": m["id"], "sender": {"id": "human." + m["identity"], "name": m["identity"],
                                 "kind": "human", "user": m["identity"], "device": "browser", "device_id": None},
                                 "sender_type": "human", "chat": m["chat"], "target": m["agent"], "bus": m["bus"],
                                 "message": m["content"], "created_at": m["created_at"], "expires_at": m["expires_at"],
                                 "lease": lease, "reply_to": m["id"], "conversation_expires_at": m["expires_at"],
                                 "correlation_version": 1, "conversation_id": m["chat"], "in_reply_to": None})
                continue
            m = db.execute("SELECT * FROM messages WHERE id=?", (queued["message"],)).fetchone()
            db.execute("UPDATE messages SET status='leased',lease=?,lease_until=?,updated_at=? WHERE id=?",
                       (lease, now + LEASE_TTL, now, m["id"]))
            sender = db.execute("SELECT a.id,a.name,a.kind,p.device,p.user,p.id AS device_id FROM agents a JOIN principals p ON a.principal=p.id WHERE a.id=?",
                                (m["sender"],)).fetchone()
            messages.append({"id": m["id"], "sender": dict(sender), "target": m["target"], "bus": m["bus"],
                             "message": m["message"], "created_at": m["created_at"], "expires_at": m["expires_at"], "lease": lease,
                             "reply_to": m["id"], "conversation_expires_at": m["expires_at"],
                             "correlation_version": m["correlation_version"],
                             "conversation_id": m["conversation"], "in_reply_to": m["in_reply_to"]})
        return {"messages": messages, "lease_seconds": LEASE_TTL}

    def _op_ack(self, db, p, r, now):
        agent = self._owned(db, p, r.get("agent"))
        status = r.get("status")
        if status not in ("delivered", "queued", "failed"):
            raise BusError("invalid delivery status")
        detail = _text(r.get("detail", ""), "detail", 1024, optional=True)
        mid = _text(r.get("id"), "message id", 128)
        lease = _text(r.get("lease"), "delivery lease", 128)
        if mid.startswith("hm_"):
            return self._chat_ack(db, agent, mid, lease, status, detail, now)
        m = db.execute("SELECT * FROM messages WHERE id=? AND target=?", (mid, agent["id"])).fetchone()
        if m is None or not m["lease"] or not secrets.compare_digest(m["lease"], lease):
            raise BusError("unknown or unavailable message", "not_found")
        self._cancel_invalid(db, now)
        m = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if m["status"] in ("delivered", "queued", "failed") and m["status"] == status:
            return {"id": mid, "status": status}
        if m["status"] != "leased" or m["lease_until"] <= now:
            raise BusError("delivery lease is no longer active", "conflict")
        db.execute("UPDATE messages SET status=?,detail=?,updated_at=? WHERE id=?", (status, detail, now, mid))
        return {"id": mid, "status": status}

    def _op_receipt(self, db, p, r, now):
        mid = _text(r.get("id"), "message id", 128)
        self._cancel_invalid(db, now)
        if mid.startswith(("hm_", "hr_")):
            m = db.execute("""SELECT m.* FROM human_chat_messages m JOIN human_chats c ON c.id=m.chat
                              JOIN agents a ON a.id=c.agent WHERE m.id=? AND a.principal=?""", (mid, p["id"])).fetchone()
            if m is None:
                raise BusError("unknown or unavailable message", "not_found")
            return {**{key: m[key] for key in ("id", "status", "detail", "created_at", "updated_at", "expires_at")},
                    "correlation_version": 1, "conversation_id": m["chat"], "in_reply_to": m["in_reply_to"]}
        m = db.execute("""SELECT m.* FROM messages m JOIN agents s ON s.id=m.sender JOIN agents t ON t.id=m.target
                          WHERE m.id=? AND (s.principal=? OR t.principal=?)""", (mid, p["id"], p["id"])).fetchone()
        if m is None:
            raise BusError("unknown or unavailable message", "not_found")
        return {"id": mid, "status": m["status"], "detail": m["detail"],
                "created_at": m["created_at"], "updated_at": m["updated_at"], "expires_at": m["expires_at"],
                "correlation_version": m["correlation_version"],
                "conversation_id": m["conversation"], "in_reply_to": m["in_reply_to"]}

    def _op_leave(self, db, p, r, now):
        agent = self._owned(db, p, r.get("agent"), admin=True)
        bus = _bus(r.get("bus", "general"))
        db.execute("DELETE FROM memberships WHERE agent=? AND bus=?", (agent["id"], bus))
        db.execute("UPDATE conversations SET closed=1 WHERE bus=? AND (initiator=? OR published=?)",
                   (bus, agent["id"], agent["id"]))
        self._cancel_invalid(db, now)
        return {"agent": agent["id"], "bus": bus, "left": True}

    def _op_revoke(self, db, p, r, now):
        self._admin(p)
        principal = _text(r.get("principal"), "principal", 128)
        if db.execute("""SELECT 1 FROM tokens t JOIN browser_credentials b ON b.digest=t.digest
                         WHERE t.principal=?""", (principal,)).fetchone():
            raise BusError("reader access is managed in the gateway roster", "forbidden")
        target = db.execute("SELECT * FROM principals WHERE id=?", (principal,)).fetchone()
        if target is None or target["is_admin"]:
            raise BusError("unknown or non-revocable principal", "not_found")
        db.execute("UPDATE principals SET revoked=1 WHERE id=?", (principal,))
        db.execute("DELETE FROM grants WHERE principal=?", (principal,))
        db.execute("DELETE FROM memberships WHERE agent IN (SELECT id FROM agents WHERE principal=?)", (principal,))
        db.execute("UPDATE agents SET status='offline' WHERE principal=?", (principal,))
        self._cancel_invalid(db, now)
        return {"principal": principal, "revoked": True}


def handler_factory(broker, assets_dir=None):
    """HTTP handler for a loopback server; static UI never receives credentials."""
    asset_root = Path(assets_dir).resolve() if assets_dir else Path(__file__).resolve().parent

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "CommunicateBus/1"
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def parse_request(self):
            if not super().parse_request():
                return False
            self.gateway_context = self._gateway_context()
            return self.gateway_context is not None

        def _gateway_context(self):
            if broker.gateway_shared_secret is None:
                # Browser identity headers are never trusted in ordinary local mode.
                return {}
            secrets_sent = self.headers.get_all("X-Communicate-Bus-Gateway", [])
            if (len(secrets_sent) != 1 or not secrets.compare_digest(
                    secrets_sent[0].encode("utf-8"), broker.gateway_shared_secret.encode("utf-8"))):
                self._respond(401, {"ok": False, "error": "trusted gateway required", "code": "unauthorized"})
                return None
            if self.path == "/_bus/sso/consume":
                if any(self.headers.get_all(name) is not None for name in (
                        "X-Communicate-Bus-Reader", "X-Communicate-Bus-Reader-Hash", "X-Communicate-Bus-View",
                        "Authorization", "Cookie")):
                    self._respond(403, {"ok": False, "error": "SSO consume requires gateway-only context", "code": "forbidden"})
                    return None
                return {}
            if self.path == "/_bus/chat" and any(self.headers.get_all(name) is not None for name in ("Authorization", "Cookie")):
                self._respond(403, {"ok": False, "error": "chat bridge requires gateway-only credentials", "code": "forbidden"})
                return None
            readers = self.headers.get_all("X-Communicate-Bus-Reader", [])
            hashes = self.headers.get_all("X-Communicate-Bus-Reader-Hash", [])
            views = self.headers.get_all("X-Communicate-Bus-View", [])
            if not readers and not hashes and not views:
                return {}
            try:
                if len(readers) != 1 or len(hashes) != 1 or len(views) > 1:
                    raise BusError("authenticated browser reader required", "unauthorized")
                broker._reader_context(readers[0], hashes[0])
                view = None
                if views:
                    if len(views[0].encode("utf-8")) > MAX_VIEW_HEADER:
                        raise ValueError("oversized view assertion")
                    view = json.loads(views[0], object_pairs_hook=_unique_object)
                    # A JSON null is not an absent assertion.
                    if not isinstance(view, dict):
                        raise ValueError("invalid view assertion")
                view = broker._view_assertion(readers[0], hashes[0], view, broker.clock())
            except (BusError, ValueError, UnicodeError, RecursionError):
                self._respond(401, {"ok": False, "error": "authenticated browser reader and view required", "code": "unauthorized"})
                return None
            context = {"reader": readers[0], "reader_hash": hashes[0]}
            if view is not None:
                context["view"] = view
            return context

        def log_message(self, fmt, *args):
            # Tokens, invitation codes, and message text never enter access logs.
            return

        def _respond(self, status, data, mime="application/json; charset=utf-8"):
            payload = json.dumps(data).encode("utf-8") if isinstance(data, dict) else data
            script_policy = "'self'"
            if mime.startswith("text/html"):
                scripts = re.findall(rb"<script\b[^>]*>(.*?)</script\s*>", payload, flags=re.DOTALL | re.IGNORECASE)
                script_policy += "".join(" 'sha256-%s'" % base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
                                         for script in scripts if script.strip())
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src %s; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" % script_policy)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/_bus/session":
                if broker.gateway_shared_secret is None:
                    return self._respond(404, {"ok": False, "error": "not found", "code": "not_found"})
                try:
                    result = broker.browser_session(**self.gateway_context) if self.gateway_context else None
                    if result is None:
                        raise BusError("authenticated browser reader required", "unauthorized")
                except BusError as exc:
                    return self._respond(403 if exc.code == "forbidden" else 401,
                                         {"ok": False, "error": str(exc), "code": exc.code})
                return self._respond(200, result)
            if path == "/health":
                return self._respond(200, {"ok": True})
            if path in ("/", "/index.html", "/graph", "/graph/"):
                candidate = asset_root / "bus_ui.html"
            elif path.startswith("/assets/"):
                candidate = (asset_root / path.lstrip("/")).resolve()
                if asset_root / "assets" not in candidate.parents:
                    return self._respond(404, {"ok": False, "error": "not found"})
            else:
                return self._respond(404, {"ok": False, "error": "not found"})
            if not candidate.is_file() or candidate.stat().st_size > 2 * 1024 * 1024:
                return self._respond(404, {"ok": False, "error": "not found"})
            mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(candidate.suffix, "application/octet-stream")
            self._respond(200, candidate.read_bytes(), mime)

        def do_POST(self):
            consume = self.path == "/_bus/sso/consume"
            chat_bridge = self.path == "/_bus/chat"
            if self.path != "/v1" and not consume and not chat_bridge:
                return self._respond(404, {"ok": False, "error": "not found"})
            if (consume or chat_bridge) and broker.gateway_shared_secret is None:
                return self._respond(404, {"ok": False, "error": "not found", "code": "not_found"})
            origin = self.headers.get("Origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    safe_origin = (parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host")
                                   and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
                except ValueError:
                    safe_origin = False
                if not safe_origin:
                    return self._respond(403, {"ok": False, "error": "cross-origin requests are not allowed", "code": "forbidden"})
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return self._respond(400, {"ok": False, "error": "application/json body required", "code": "invalid_request"})
            try:
                if len(self.headers.get_all("Content-Length", [])) != 1 or len(self.headers.get_all("Content-Type", [])) != 1:
                    raise ValueError("ambiguous body headers")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self._respond(413, {"ok": False, "error": "invalid body size", "code": "too_large"})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete body")
                request = json.loads(raw.decode("utf-8"), **({"object_pairs_hook": _unique_object} if consume or chat_bridge else {}))
            except (ValueError, UnicodeError, OSError, RecursionError):
                return self._respond(400, {"ok": False, "error": "invalid JSON body", "code": "invalid_request"})
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            try:
                if chat_bridge:
                    if not self.gateway_context:
                        raise BusError("authenticated browser reader required", "unauthorized")
                    if not isinstance(request, dict) or request.get("op") not in CHAT_OPS:
                        raise BusError("chat bridge accepts only chat operations", "forbidden")
                    token = broker.browser_session(**self.gateway_context)["token"]
                result = broker.consume_sso(request) if consume else broker.handle(token, request, **self.gateway_context)
            except BusError as exc:
                result = {"ok": False, "error": str(exc), "code": exc.code}
            except Exception:
                return self._respond(500, {"ok": False, "error": "broker operation failed", "code": "internal_error"})
            status = 200 if result.get("ok") else {"unauthorized": 401, "forbidden": 403, "not_found": 404,
                "conflict": 409, "ambiguous": 409, "limit": 429, "too_large": 413}.get(result.get("code"), 400)
            self._respond(status, result)

    return Handler


def _check_loopback(host):
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("bus broker must bind to loopback; use an HTTPS reverse proxy")
        except ValueError:
            raise ValueError("bus broker must bind to loopback; use an HTTPS reverse proxy") from None


class BusHTTPServer(http.server.ThreadingHTTPServer):
    """Bound concurrent requests, including clients that never finish headers."""
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, max_connections=32):
        _check_loopback(address[0])
        if ":" in address[0]:
            import socket
            self.address_family = socket.AF_INET6
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def server_bind(self):
        # HTTPServer resolves the bound address with getfqdn. The broker only
        # advertises its numeric loopback address, so DNS must not delay startup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def serve(broker, host="127.0.0.1", port=0, assets_dir=None):
    """Run the backend on loopback only. TLS and remote exposure are explicit."""
    server = BusHTTPServer((host, port), handler_factory(broker, assets_dir))
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
