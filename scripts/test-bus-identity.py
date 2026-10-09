#!/usr/bin/env python3
"""Account attribution, stable device identity, and upgrade safety."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bus_broker import Broker, BusError


USERS = {"aadarsh": "aadarwal", "peer": "peer"}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bus-identity-")
        self.env = {"BUS_READER_USERS": json.dumps(USERS), "BUS_ADMIN_READERS": "aadarsh",
                    "BUS_GATEWAY_SHARED_SECRET": "test-only-gateway-" + "x" * 40}
        with mock.patch.dict(os.environ, self.env):
            self.b = Broker(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, op, token=None, **fields):
        result = self.b.handle(self.b.admin_token if token is None else token, dict(fields, op=op))
        self.assertTrue(result["ok"], result)
        return result

    def invite(self, user="aadarwal", bus="general"):
        return self.call("invite", user=user, bus=bus)["invite"]

    def enroll(self, user="aadarwal", **fields):
        return self.call("redeem", token="", invite=self.invite(user), device="same-name", **fields)

    def test_invitation_sets_owner_and_cross_user_extension_cannot_relabel_device(self):
        self.assertFalse(self.b.handle(self.b.admin_token, {"op": "invite"})["ok"])
        self.assertEqual(self.b.handle(self.b.admin_token, {"op": "invite", "user": "unknown"})["code"], "forbidden")
        device = self.enroll(user="aadarwal")
        self.assertEqual(device["user"], "aadarwal")
        self.assertEqual(device["device_id"], device["principal"])
        other_invite = self.invite("peer")
        denied = self.b.handle(device["token"], {"op": "redeem", "invite": other_invite, "user": "peer"})
        self.assertEqual(denied["code"], "forbidden")
        # A rejected cross-user attempt neither consumes the invite nor mutates ownership.
        other = self.call("redeem", token="", invite=other_invite, user="aadarwal")
        self.assertEqual(other["user"], "peer")
        self.assertNotEqual(other["device_id"], device["device_id"])
        registration = self.call("register", token=device["token"], session_key="session", name="worker", user="peer", owner="peer")
        self.assertEqual(registration["user"], "aadarwal")
        self.assertEqual(self.call("snapshot", token=device["token"])["user"], "aadarwal")

    def test_device_metadata_is_descriptive_bounded_and_does_not_change_identity(self):
        metadata = {"hostname": "mini.local", "platform": "darwin", "tailscale_hostname": "mini",
                    "tailscale_dns_name": "mini.example.ts.net"}
        device = self.enroll(device_metadata=metadata)
        agent = self.call("register", token=device["token"], session_key="s", name="worker")
        updated = self.call("device", token=device["token"], device="Lab Mini", device_metadata={"hostname": "mini-new.local"})
        self.assertEqual(updated["device_id"], device["device_id"])
        self.assertEqual(updated["user"], "aadarwal")
        self.assertEqual(updated["device"], "Lab Mini")
        snap = self.call("snapshot", token=device["token"])
        row = snap["buses"][0]["agents"][0]
        self.assertEqual(row["id"], agent["id"])
        self.assertEqual(row["device_metadata"], {"hostname": "mini-new.local"})
        self.assertEqual(row["device"], "Lab Mini")
        for payload in ({"user": "peer"}, {"owner": "peer"}, {"device_metadata": {"user": "peer"}},
                        {"device_metadata": {"hostname": "x" * 254}}, {"device_metadata": {"hostname": "bad\nname"}}):
            self.assertFalse(self.b.handle(device["token"], dict(payload, op="device"))["ok"])
        self.assertEqual(self.call("device", token=device["token"])["user"], "aadarwal")

    def test_device_metadata_reaches_only_its_device_its_account_and_administrators(self):
        metadata = {"hostname": "mini.local", "platform": "darwin", "tailscale_hostname": "mini",
                    "tailscale_dns_name": "mini.tail0000.ts.net"}
        owner = self.enroll("aadarwal", device_metadata=metadata)
        peer = self.enroll("peer", device_metadata={"hostname": "peer.local"})
        owned = self.call("register", token=owner["token"], session_key="s", name="worker")["id"]
        self.call("register", token=peer["token"], session_key="p", name="peer-worker")

        def rows(snapshot):
            return {agent["user"]: agent for agent in snapshot["buses"][0]["agents"]}

        self.assertEqual(rows(self.call("snapshot", token=owner["token"]))["aadarwal"]["device_metadata"], metadata)
        self.assertEqual(rows(self.call("snapshot"))["aadarwal"]["device_metadata"], metadata)
        by_peer = self.call("snapshot", token=peer["token"])
        self.assertEqual(rows(by_peer)["aadarwal"]["id"], owned)
        self.assertEqual(rows(by_peer)["aadarwal"]["device"], "same-name")
        self.assertNotIn("device_metadata", rows(by_peer)["aadarwal"])
        self.assertNotIn("mini.tail0000.ts.net", json.dumps(by_peer))
        self.assertNotIn("mini.local", json.dumps(by_peer))
        reader_hash = hashlib.sha256(b"peer-browser-fixture").hexdigest()
        browser = self.b.browser_session("peer", reader_hash)["token"]
        by_account = self.b.handle(browser, {"op": "snapshot"}, reader="peer", reader_hash=reader_hash)
        self.assertTrue(by_account["ok"], by_account)
        self.assertEqual(rows(by_account)["peer"]["device_metadata"], {"hostname": "peer.local"})
        self.assertNotIn("device_metadata", rows(by_account)["aadarwal"])

    def test_same_device_labels_and_agent_names_remain_distinct_and_private_scoped(self):
        self.call("create", bus="photonics")
        left, right = self.enroll("aadarwal"), self.enroll("peer")
        for device in (left, right):
            self.call("register", token=device["token"], session_key="same", name="reviewer")
        self.call("redeem", token=left["token"], invite=self.invite("aadarwal", "photonics"))
        self.call("register", token=left["token"], session_key="private", name="private-worker", bus="photonics")
        viewer = self.call("snapshot", token=right["token"])
        self.assertEqual([bus["name"] for bus in viewer["buses"]], ["general"])
        self.assertNotIn("users", viewer)
        self.assertNotIn("principals", viewer)
        agents = viewer["buses"][0]["agents"]
        self.assertEqual({agent["user"] for agent in agents}, {"aadarwal", "peer"})
        self.assertEqual(len({agent["device_id"] for agent in agents}), 2)
        self.assertNotIn("private-worker", json.dumps(viewer))
        sent = self.call("send", token=left["token"], sender=agents[0]["id"] if agents[0]["user"] == "aadarwal" else agents[1]["id"],
                         target=next(a["id"] for a in agents if a["user"] == "peer"), message="identity check")
        polled = self.call("poll", token=right["token"], agent=next(a["id"] for a in agents if a["user"] == "peer"))
        self.assertEqual(polled["messages"][0]["sender"]["user"], "aadarwal")
        self.assertEqual(polled["messages"][0]["sender"]["device_id"], left["device_id"])

    def test_browser_mapping_preserves_login_and_never_creates_an_enrolled_device(self):
        reader_hash = hashlib.sha256(b"github-identity-fixture").hexdigest()
        token = self.b.browser_session("aadarsh", reader_hash)["token"]
        snap = self.b.handle(token, {"op": "snapshot"}, reader="aadarsh", reader_hash=reader_hash)
        self.assertTrue(snap["is_admin"])
        self.assertEqual(snap["user"], "aadarwal")
        self.assertEqual(snap["users"], [{"id": "aadarwal"}, {"id": "peer"}])
        self.assertEqual([p["id"] for p in snap["principals"]], ["admin"])
        denied = self.b.handle(token, {"op": "redeem", "invite": self.invite()}, reader="aadarsh", reader_hash=reader_hash)
        self.assertEqual(denied["code"], "forbidden")

    def test_github_identity_rotation_replaces_browser_credential_without_changing_ownership(self):
        self.call("create", bus="photonics")
        for reader, login, user, is_admin in (("aadarsh", "aadarwal", "aadarwal", True),
                                              ("peer", "peer-handle", "peer", False)):
            with self.subTest(login=login):
                device = self.enroll(user)
                self.call("redeem", token=device["token"], invite=self.invite(user, "photonics"))
                agent = self.call("register", token=device["token"], session_key="existing", name=login,
                                  bus="photonics")
                before_device = self.call("snapshot", token=device["token"])

                # The gateway attests an immutable GitHub ID plus the allowlist
                # mapping. A changed identity digest must replace only web auth.
                def identity_hash(github_id):
                    material = "communicate/github-identity/v1\0%d\0%s\0%s" % (github_id, login, reader)
                    return hashlib.sha256(material.encode()).hexdigest()

                previous_hash, current_hash = identity_hash(123), identity_hash(456)
                previous_token = self.b.browser_session(reader, previous_hash)["token"]
                previous = self.b.handle(previous_token, {"op": "snapshot"}, reader=reader, reader_hash=previous_hash)
                self.assertTrue(previous["ok"], previous)
                self.assertEqual(previous["user"], user)
                self.assertEqual(previous["is_admin"], is_admin)
                self.assertEqual(self.b.handle(previous_token, {"op": "snapshot"}, reader=reader,
                                              reader_hash=current_hash)["code"], "unauthorized")

                current_token = self.b.browser_session(reader, current_hash)["token"]
                self.assertNotEqual(current_token, previous_token)
                current = self.b.handle(current_token, {"op": "snapshot"}, reader=reader, reader_hash=current_hash)
                self.assertTrue(current["ok"], current)
                self.assertEqual(current["principal"], previous["principal"])
                self.assertEqual(current["user"], user)
                self.assertEqual(current["is_admin"], is_admin)
                for context_hash in (previous_hash, current_hash):
                    self.assertEqual(self.b.handle(previous_token, {"op": "snapshot"}, reader=reader,
                                                  reader_hash=context_hash)["code"], "unauthorized")
                self.assertEqual(self.b.handle(current_token, {"op": "snapshot"})["code"], "unauthorized")
                if not is_admin:
                    self.assertEqual([bus["name"] for bus in current["buses"]], ["general"])
                    created = self.b.handle(current_token, {"op": "create", "bus": "account-private"},
                                            reader=reader, reader_hash=current_hash)
                    self.assertTrue(created["ok"], created)
                    self.assertEqual(created["owner_user"], user)

                # Existing machine credentials still work without web context;
                # GitHub identity changes cannot transfer or erase enrollment.
                after_device = self.call("snapshot", token=device["token"])
                for field in ("principal", "device_id", "device", "device_metadata", "user", "is_admin", "buses"):
                    self.assertEqual(after_device[field], before_device[field])
                self.assertEqual(after_device["device_id"], device["device_id"])
                self.assertEqual(after_device["user"], user)
                self.assertEqual([bus["name"] for bus in after_device["buses"]], ["general", "photonics"])
                self.assertIn(agent["id"], [row["id"] for row in after_device["buses"][1]["agents"]])
                admin_devices = self.call("snapshot")["principals"]
                self.assertNotIn(current["principal"], [entry["id"] for entry in admin_devices])

    def test_old_database_preserves_tokens_and_requires_explicit_owner_assignment(self):
        with tempfile.TemporaryDirectory(prefix="bus-old-identity-") as old:
            root = Path(old)
            token = "legacy-device-token-" + "q" * 40
            db = sqlite3.connect(str(root / "bus.sqlite3"))
            db.executescript("""CREATE TABLE principals(id TEXT PRIMARY KEY,device TEXT NOT NULL,created_at REAL NOT NULL,
                                 revoked INTEGER NOT NULL DEFAULT 0,is_admin INTEGER NOT NULL DEFAULT 0);
                              CREATE TABLE tokens(digest TEXT PRIMARY KEY,principal TEXT NOT NULL REFERENCES principals(id));""")
            db.execute("INSERT INTO principals VALUES('legacy','peer-laptop',0,0,0)")
            db.execute("INSERT INTO tokens VALUES(?,'legacy')", (hashlib.sha256(token.encode()).hexdigest(),))
            db.commit(); db.close()
            with mock.patch.dict(os.environ, self.env):
                broker = Broker(root)
            snap = broker.handle(token, {"op": "snapshot"})
            self.assertTrue(snap["ok"])
            self.assertIsNone(snap["user"], "a hostname cannot claim a user")
            invitation = broker.handle(broker.admin_token, {"op": "invite", "user": "aadarwal"})["invite"]
            enrolled = broker.handle(token, {"op": "redeem", "invite": invitation})
            self.assertTrue(enrolled["ok"], enrolled)
            self.assertEqual(enrolled["user"], "aadarwal")
            self.assertEqual(enrolled["principal"], "legacy")
            self.assertEqual(enrolled["token"], token)
            with mock.patch.dict(os.environ, self.env):
                reopened = Broker(root)
            self.assertEqual(reopened.handle(token, {"op": "snapshot"})["user"], "aadarwal")

    def test_invalid_server_user_mapping_fails_closed(self):
        for value in ('[]', '{"aadarsh":"bad user"}', '{"bad reader":"aadarwal"}', 'null'):
            with mock.patch.dict(os.environ, {"BUS_READER_USERS": value}):
                with self.assertRaisesRegex(ValueError, "BUS_READER_USERS"):
                    Broker(self.tmp.name)

    def test_user_mapping_changes_do_not_admit_unmapped_readers_or_old_invites(self):
        invitation = self.invite("peer")
        with self.assertRaisesRegex(BusError, "no configured bus user"):
            self.b.browser_session("unmapped", hashlib.sha256(b"github-identity").hexdigest())
        with mock.patch.dict(os.environ, {**self.env, "BUS_READER_USERS": json.dumps({"aadarsh": "aadarwal"})}):
            reopened = Broker(self.tmp.name)
        denied = reopened.handle(None, {"op": "redeem", "invite": invitation})
        self.assertEqual(denied["code"], "forbidden")
        self.assertIn("no longer configured", denied["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
