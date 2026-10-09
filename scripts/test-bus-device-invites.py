#!/usr/bin/env python3
"""Enrolled devices inviting their own account's other devices, within their own access."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import bus
from bus_broker import Broker


USERS = {"aadarsh": "aadarwal", "peer": "peer"}


class DeviceInvitationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bus-device-invites-")
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.env = {"BUS_READER_USERS": json.dumps(USERS), "BUS_ADMIN_READERS": "aadarsh",
                    "BUS_GATEWAY_SHARED_SECRET": "test-only-gateway-" + "x" * 40}
        self.open_broker()
        self.call("create", bus="qpaig")
        self.device = self.enroll("aadarwal", "qpaig")

    def open_broker(self, **env):
        with mock.patch.dict(os.environ, dict(self.env, **env)):
            self.b = Broker(self.tmp.name, clock=lambda: self.now)

    def raw(self, op, token=None, **fields):
        return self.b.handle(self.b.admin_token if token is None else token, dict(fields, op=op))

    def call(self, op, token=None, **fields):
        result = self.raw(op, token, **fields)
        self.assertTrue(result.get("ok"), result)
        return result

    def refused(self, op, token, code, **fields):
        result = self.raw(op, token, **fields)
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["code"], code, result)
        return result

    def enroll(self, user, bus_name, token=""):
        invite = self.call("invite", user=user, bus=bus_name)["invite"]
        return self.call("redeem", token=token, invite=invite, device="fixture-" + user)

    def issuer(self, principal):
        with contextlib.closing(sqlite3.connect(Path(self.tmp.name) / "bus.sqlite3")) as db:
            return db.execute("SELECT issuer_user,issuer_principal FROM invites WHERE principal=?",
                              (principal,)).fetchone()

    def test_device_invites_another_device_of_its_own_account_onto_its_bus(self):
        issued = self.call("invite", self.device["token"], bus="qpaig")
        self.assertEqual((issued["bus"], issued["user"]), ("qpaig", "aadarwal"))
        self.assertEqual(issued["expires_at"], self.now + 900)
        second = self.call("redeem", token="", invite=issued["invite"], device="second")
        self.assertEqual(second["user"], "aadarwal")
        self.assertEqual(second["buses"], ["qpaig"])
        self.assertNotEqual(second["principal"], self.device["principal"])
        self.assertEqual(self.issuer(second["principal"]), ("aadarwal", self.device["principal"]))

    def test_device_cannot_invite_a_device_for_another_account(self):
        self.refused("invite", self.device["token"], "forbidden", bus="qpaig", user="peer")

    def test_device_cannot_invite_onto_a_bus_it_cannot_reach(self):
        self.call("create", bus="other")
        self.refused("invite", self.device["token"], "forbidden", bus="other")
        self.refused("invite", self.device["token"], "forbidden", bus="general")
        self.refused("invite", self.device["token"], "forbidden", bus="missing")

    def test_device_invitations_default_to_fifteen_minutes_and_last_at_most_an_hour(self):
        self.assertEqual(self.call("invite", self.device["token"], bus="qpaig", ttl=3600)["expires_at"], self.now + 3600)
        self.refused("invite", self.device["token"], "invalid_request", bus="qpaig", ttl=3601)
        self.refused("invite", self.device["token"], "invalid_request", bus="qpaig", ttl=59)
        self.refused("invite", self.device["token"], "invalid_request", bus="qpaig", ttl=True)

    def test_each_device_has_a_small_outstanding_invitation_limit(self):
        for _ in range(5):
            self.call("invite", self.device["token"], bus="qpaig")
        self.refused("invite", self.device["token"], "limit", bus="qpaig")
        other = self.enroll("aadarwal", "qpaig")
        self.call("invite", other["token"], bus="qpaig")
        self.now += 901
        self.call("invite", self.device["token"], bus="qpaig")

    def test_hub_operator_can_turn_device_invitations_off(self):
        self.open_broker(BUS_DEVICE_INVITES="off")
        self.refused("invite", self.device["token"], "forbidden", bus="qpaig")
        self.assertFalse(self.call("snapshot", self.device["token"])["buses"][0]["capabilities"]["invite"])
        with self.assertRaises(ValueError):
            self.open_broker(BUS_DEVICE_INVITES="everyone")

    def test_revoked_device_cannot_invite(self):
        self.call("revoke", principal=self.device["principal"])
        self.refused("invite", self.device["token"], "unauthorized", bus="qpaig")

    def test_event_access_cannot_be_extended_with_invitations(self):
        self.call("create", bus="workshop")
        code = self.call("event_create", bus="workshop")["invite"]
        joined = self.call("redeem", token=self.device["token"], invite=code, device="fixture-aadarwal")
        self.assertIn("workshop", joined["buses"])
        self.refused("invite", self.device["token"], "forbidden", bus="workshop")
        self.call("invite", self.device["token"], bus="qpaig")

    def test_device_can_revoke_only_invitations_it_issued(self):
        own = self.call("invite", self.device["token"], bus="qpaig")["invite"]
        self.call("invite_revoke", self.device["token"], invite=own)
        self.refused("redeem", "", "forbidden", invite=own, device="late")
        operator = self.call("invite", user="aadarwal", bus="qpaig")["invite"]
        self.refused("invite_revoke", self.device["token"], "forbidden", invite=operator)

    def test_snapshot_reports_device_invitation_capability(self):
        self.call("create", bus="other")
        self.call("redeem", token=self.device["token"],
                  invite=self.call("invite", user="aadarwal", bus="other")["invite"], device="fixture-aadarwal")
        buses = {row["name"]: row for row in self.call("snapshot", self.device["token"])["buses"]}
        self.assertTrue(buses["qpaig"]["capabilities"]["invite"])
        self.assertTrue(buses["other"]["capabilities"]["invite"])

    def test_operator_invitations_are_unchanged_and_record_their_issuer(self):
        long_lived = self.call("invite", user="peer", bus="qpaig", ttl=86400)
        self.assertEqual(long_lived["expires_at"], self.now + 86400)
        peer = self.call("redeem", token="", invite=long_lived["invite"], device="peer")
        self.assertEqual(self.issuer(peer["principal"]), (None, "admin"))


class DeviceInvitationCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bus-device-invite-cli-")
        self.addCleanup(self.tmp.cleanup)
        env = {"BUS_READER_USERS": json.dumps(USERS), "BUS_ADMIN_READERS": "aadarsh",
               "BUS_GATEWAY_SHARED_SECRET": "test-only-gateway-" + "x" * 40}
        with mock.patch.dict(os.environ, env):
            self.broker = Broker(Path(self.tmp.name) / "broker")
        self.broker.handle(self.broker.admin_token, {"op": "create", "bus": "qpaig"})
        invite = self.broker.handle(self.broker.admin_token, {"op": "invite", "bus": "qpaig", "user": "aadarwal"})
        device = self.broker.handle("", {"op": "redeem", "invite": invite["invite"], "device": "agent-device"})
        self.conn = {"url": "https://hub.example", "token": device["token"]}
        self.requests = []

    def request(self, conn, op, **payload):
        self.requests.append(op)
        result = self.broker.handle(conn.get("token"), dict(payload, op=op))
        if not result.get("ok"):
            raise bus.BusError(result.get("error", "request failed"))
        return result

    def run_cli(self, *argv):
        with mock.patch("bus.connection", return_value=self.conn), mock.patch("bus.request", side_effect=self.request):
            return bus.run(bus.parser().parse_args(["invite", *argv]))

    def test_out_writes_a_private_file_and_prints_only_its_path(self):
        target = Path(self.tmp.name) / "invitation"
        result = self.run_cli("qpaig", "--url", "https://hub.example", "--out", str(target))
        self.assertIsInstance(result, str)
        self.assertIn(str(target), result)
        self.assertIn("--invite-stdin", result)
        self.assertNotIn("commbus1.", result)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        code = target.read_text().strip()
        self.assertTrue(code.startswith("commbus1."))
        raw = code.split(".", 1)[1]
        card = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        self.assertEqual(card["url"], "https://hub.example")
        enrolled = self.broker.handle("", {"op": "redeem", "invite": card["invite"], "device": "second"})
        self.assertEqual(enrolled["user"], "aadarwal")

    def test_out_refuses_symlinks_existing_files_and_relative_paths_before_requesting(self):
        existing = Path(self.tmp.name) / "existing"
        existing.write_text("keep")
        link = Path(self.tmp.name) / "link"
        link.symlink_to(Path(self.tmp.name) / "elsewhere")
        for out in (str(existing), str(link), "relative-invitation"):
            with self.subTest(out=out), self.assertRaises(bus.BusError):
                self.run_cli("qpaig", "--url", "https://hub.example", "--out", out)
        self.assertEqual(self.requests, [])
        self.assertEqual(existing.read_text(), "keep")
        self.assertFalse((Path(self.tmp.name) / "elsewhere").exists())

    def test_printed_invitation_warns_that_it_is_a_credential(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.run_cli("qpaig", "--url", "https://hub.example")
        self.assertTrue(code.startswith("commbus1."))
        self.assertIn("credential", stderr.getvalue())
        self.assertIn("--out", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
