#!/usr/bin/env python3
"""Mention parser, registry resolution, fan-out, and completion.

The router is exercised with a fake registry and a fake send. Nothing here
contacts a broker or types into a terminal.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import mentions  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROMPT = (
    "@personal-assistant pls make sure you contribute to this.. and pls make "
    "sure that claude code sessions and codex sessions relevant for this project "
    "contribute correctly ! eg @com8::dirk-personal:neo4j-ops and "
    "@com8::dirk-personal:fleet-ops and @com8::dirk-personal:wilde-qit and "
    '@com8::dirk-personal:wilde-qit-astra"'
)


def agent(name, *, id=None, kind="codex", status="queueable", last_seen=1, **extra):
    row = {
        "id": id or ("a_" + hashlib.sha256(name.encode()).hexdigest()[:32]),
        "name": name,
        "kind": kind,
        "status": status,
        "last_seen": last_seen,
    }
    row.update(extra)
    return row


def registry(*buses):
    """buses is a sequence of (name, [agents])."""
    return mentions.MemoryRegistry({name: rows for name, rows in buses})


def dirk_registry():
    return registry(
        ("dirk-personal", [
            agent("neo4j-ops", id="a_" + "11" * 16, kind="codex", status="queueable", last_seen=30),
            agent("fleet-ops", id="a_" + "22" * 16, kind="claude", status="live", last_seen=20),
            agent("wilde-qit", id="a_" + "33" * 16, kind="codex", status="queueable", last_seen=10),
            agent("wilde-qit-astra", id="a_" + "44" * 16, kind="claude", status="live", last_seen=5),
            agent("quiet", id="a_" + "55" * 16, kind="claude", status="offline", last_seen=100),
        ]),
        ("other-bus", [agent("neo4j-ops", id="a_" + "66" * 16, kind="codex", status="queueable", last_seen=1)]),
    )


class ParserTests(unittest.TestCase):
    def test_example_prompt_keeps_four_mentions_and_ignores_plain_at(self):
        found = mentions.parse(PROMPT)
        self.assertEqual(
            [(m["bus"], m["session"]) for m in found["mentions"]],
            [
                ("dirk-personal", "neo4j-ops"),
                ("dirk-personal", "fleet-ops"),
                ("dirk-personal", "wilde-qit"),
                ("dirk-personal", "wilde-qit-astra"),
            ],
        )
        self.assertEqual(found["errors"], [])
        self.assertNotIn("personal-assistant", json.dumps(found["mentions"]))

    def test_trailing_punctuation_is_not_part_of_the_name(self):
        text = (
            '(@com8::dirk-personal:fleet-ops), see @com8::dirk-personal:wilde-qit-astra". '
            "and @com8::dirk-personal:neo4j-ops."
        )
        found = mentions.parse(text)
        self.assertEqual(
            [m["session"] for m in found["mentions"]],
            ["fleet-ops", "wilde-qit-astra", "neo4j-ops"],
        )
        self.assertEqual(found["errors"], [])

    def test_internal_dot_stays_and_a_trailing_dot_does_not(self):
        found = mentions.parse("Use @com8::lab.bus:worker.one. next")
        self.assertEqual(found["mentions"][0]["bus"], "lab.bus")
        self.assertEqual(found["mentions"][0]["session"], "worker.one")

    def test_duplicates_are_returned_in_order(self):
        found = mentions.parse(
            "@com8::dirk-personal:neo4j-ops and again @com8::dirk-personal:neo4j-ops."
        )
        self.assertEqual(len(found["mentions"]), 2)
        self.assertEqual(found["errors"], [])

    def test_invalid_forms_are_errors(self):
        text = " ".join([
            "@com8::",
            "@com8::dirk-personal",
            "@com8::dirk-personal:",
            "@com8::DirK:neo4j-ops",
            "@com8::dirk-personal:neo4j-ops:extra",
            "@com8::has space:no",
        ])
        found = mentions.parse(text)
        self.assertEqual(found["mentions"], [])
        self.assertGreaterEqual(len(found["errors"]), 5)
        self.assertTrue(all(err["code"] == "invalid_mention" for err in found["errors"]))

    def test_plain_names_single_colon_and_bare_form_are_left_alone(self):
        text = (
            "@personal-assistant @com8:dirk-personal:neo4j-ops "
            "com8::dirk-personal:neo4j-ops @Com8::dirk-personal:neo4j-ops"
        )
        found = mentions.parse(text)
        self.assertEqual(found["mentions"], [])
        self.assertEqual(found["errors"], [])

    def test_unknown_handler_is_left_alone(self):
        found = mentions.parse("ping @other::dirk-personal:neo4j-ops please")
        self.assertEqual(found["mentions"], [])
        self.assertEqual(found["errors"], [])

    def test_inline_code_and_fences_are_not_scanned(self):
        text = (
            "See `@com8::dirk-personal:neo4j-ops` and then @com8::dirk-personal:fleet-ops.\n"
            "```\n"
            "@com8::NOT-A-BUS:nope\n"
            "@com8::dirk-personal:wilde-qit\n"
            "```\n"
            "Done @com8::dirk-personal:wilde-qit-astra."
        )
        found = mentions.parse(text)
        self.assertEqual(
            [m["session"] for m in found["mentions"]],
            ["fleet-ops", "wilde-qit-astra"],
        )
        self.assertEqual(found["errors"], [])

    def test_unclosed_fence_hides_the_rest_of_the_prompt(self):
        found = mentions.parse("```\n@com8::dirk-personal:neo4j-ops\n")
        self.assertEqual(found["mentions"], [])
        self.assertEqual(found["errors"], [])


class ResolverTests(unittest.TestCase):
    def test_unique_name_on_the_named_bus(self):
        found = mentions.resolve("dirk-personal", "neo4j-ops", dirk_registry())
        self.assertTrue(found["ok"])
        self.assertEqual(found["name"], "neo4j-ops")
        self.assertEqual(found["kind"], "codex")
        self.assertEqual(found["delivery"], "codex-queue")
        self.assertNotEqual(
            found["id"],
            mentions.resolve("other-bus", "neo4j-ops", dirk_registry())["id"],
        )

    def test_kind_selects_the_existing_delivery_label(self):
        reg = registry(("general", [
            agent("claude-one", kind="claude", status="live"),
            agent("codex-one", kind="codex", status="queueable"),
            agent("bridge", kind="service", status="queueable"),
        ]))
        self.assertEqual(mentions.resolve("general", "claude-one", reg)["delivery"], "claude-socket")
        self.assertEqual(mentions.resolve("general", "codex-one", reg)["delivery"], "codex-queue")
        self.assertEqual(mentions.resolve("general", "bridge", reg)["delivery"], "service-bridge")

    def test_unknown_bus_and_unknown_session(self):
        reg = dirk_registry()
        missing_bus = mentions.resolve("no-such-bus", "neo4j-ops", reg)
        missing = mentions.resolve("dirk-personal", "missing", reg)
        self.assertEqual(missing_bus["code"], "unknown_bus")
        self.assertEqual(missing["code"], "unknown_session")
        self.assertNotIn("candidates", missing)

    def test_ambiguous_name_lists_ids_only(self):
        first = agent("shared", id="a_" + "1" * 32, token="sekret", device_metadata={"hostname": "hide"})
        second = agent("shared", id="a_" + "2" * 32, socket="/tmp/x.sock")
        reg = registry(("dirk-personal", [first, second]))
        found = mentions.resolve("dirk-personal", "shared", reg)
        self.assertEqual(found["code"], "ambiguous")
        self.assertEqual(found["candidates"], ["a_" + "1" * 32, "a_" + "2" * 32])
        blob = json.dumps(found)
        self.assertNotIn("sekret", blob)
        self.assertNotIn("hostname", blob)
        self.assertNotIn("socket", blob)

    def test_exact_id_wins_and_other_bus_does_not(self):
        ident = "a_" + "ab" * 16
        reg = registry(("dirk-personal", [agent("neo4j-ops", id=ident)]))
        found = mentions.resolve("dirk-personal", ident, reg)
        self.assertTrue(found["ok"])
        self.assertEqual(found["id"], ident)
        self.assertEqual(
            mentions.resolve("other-bus", ident, reg)["code"],
            "unknown_bus",
        )

    def test_retired_is_not_addressable_and_offline_is(self):
        reg = registry(("dirk-personal", [
            agent("gone", retired=True, status="offline"),
            agent("quiet", status="offline", kind="claude"),
        ]))
        self.assertEqual(mentions.resolve("dirk-personal", "gone", reg)["code"], "unknown_session")
        found = mentions.resolve("dirk-personal", "quiet", reg)
        self.assertTrue(found["ok"])
        self.assertEqual(found["status"], "offline")
        self.assertEqual(found["delivery"], "claude-socket")

    def test_snapshot_projection_drops_secrets(self):
        ident = "a_" + "cd" * 16
        snapshot = {
            "token": "device-token",
            "buses": [{
                "name": "dirk-personal",
                "events": [{"code": "commbus1.SECRET"}],
                "members": [{"user": "dirk"}],
                "agents": [{
                    "id": ident,
                    "name": "neo4j-ops",
                    "kind": "claude",
                    "status": "live",
                    "last_seen": 5,
                    "description": "token=abc",
                    "device_metadata": {"hostname": "secret-host"},
                    "socket": "/tmp/secret.sock",
                    "invite": "commbus1.NOPE",
                }],
            }],
        }
        projected = mentions.project_snapshot(snapshot)
        blob = json.dumps(projected)
        for secret in ("device-token", "SECRET", "secret-host", "token=abc", "secret.sock", "commbus1"):
            self.assertNotIn(secret, blob)
        self.assertEqual(projected["dirk-personal"][0]["id"], ident)
        self.assertEqual(set(projected["dirk-personal"][0]), {"id", "name", "kind", "status", "last_seen"})


class RouterTests(unittest.TestCase):
    def test_one_delivery_per_distinct_mention_with_the_full_prompt(self):
        sent = []

        def send(target, bus, message, sender="self", hub=None):
            sent.append((target, bus, message, sender, hub))
            return {"id": "m_" + target[-4:], "status": "accepted", "target": target, "token": "hide-me"}

        result = mentions.route(
            PROMPT, registry=dirk_registry(), send=send, sender="self",
            limiter=mentions.RateLimiter(None),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(sent), 4)
        self.assertEqual(len({call[0] for call in sent}), 4)
        self.assertTrue(all(call[1] == "dirk-personal" for call in sent))
        self.assertTrue(all(call[2] == PROMPT for call in sent))
        self.assertTrue(all(call[3] == "self" for call in sent))
        blob = json.dumps(result)
        self.assertNotIn("hide-me", blob)
        self.assertNotIn("pls make sure", blob)
        self.assertIn("m_", blob)

    def test_duplicates_and_id_alias_send_once(self):
        ident = "a_" + "11" * 16
        reg = registry(("dirk-personal", [agent("neo4j-ops", id=ident)]))
        sent = []
        prompt = (
            "@com8::dirk-personal:neo4j-ops "
            "@com8::dirk-personal:neo4j-ops "
            "@com8::dirk-personal:%s" % ident
        )
        mentions.route(
            prompt, registry=reg,
            send=lambda target, bus, message, sender="self", hub=None: sent.append(target) or {"id": "m_1", "status": "accepted", "target": target},
            limiter=mentions.RateLimiter(None),
        )
        self.assertEqual(sent, [ident])

    def test_context_is_appended_and_sender_is_preserved(self):
        captured = {}

        def send(target, bus, message, sender="self", hub=None):
            captured["message"] = message
            captured["sender"] = sender
            captured["hub"] = hub
            return {"id": "m_1", "status": "accepted", "target": target}

        prompt = "@com8::dirk-personal:fleet-ops look here"
        mentions.route(
            prompt, registry=dirk_registry(), send=send, context="extra notes",
            sender="a_" + "22" * 16, hub="https://bus.nonlocally.org",
            limiter=mentions.RateLimiter(None),
        )
        self.assertEqual(captured["message"], prompt + "\n\nextra notes")
        self.assertEqual(captured["sender"], "a_" + "22" * 16)
        self.assertEqual(captured["hub"], "https://bus.nonlocally.org")

    def test_dry_run_does_not_send_or_consume_budget(self):
        sent = []
        limiter = mentions.RateLimiter(None, limit=1, window=60, now=lambda: 1000)
        result = mentions.route(
            "@com8::dirk-personal:neo4j-ops", registry=dirk_registry(),
            send=lambda **kwargs: sent.append(kwargs), dry_run=True, limiter=limiter,
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["sent"])
        self.assertEqual(result["deliveries"][0]["name"], "neo4j-ops")
        self.assertEqual(result["deliveries"][0]["delivery"], "codex-queue")
        self.assertFalse(result["deliveries"][0]["sent"])
        follow = mentions.route(
            "@com8::dirk-personal:fleet-ops", registry=dirk_registry(),
            send=lambda target, bus, message, sender="self", hub=None: sent.append(target) or {"id": "m_1", "status": "accepted", "target": target},
            limiter=limiter,
        )
        self.assertTrue(follow["ok"], follow)
        self.assertEqual(sent, [mentions.resolve("dirk-personal", "fleet-ops", dirk_registry())["id"]])

    def test_unknown_and_ambiguous_are_not_sent(self):
        reg = registry(("dirk-personal", [
            agent("shared", id="a_" + "1" * 32),
            agent("shared", id="a_" + "2" * 32),
            agent("neo4j-ops"),
        ]))
        sent = []
        result = mentions.route(
            "@com8::missing:neo4j-ops @com8::dirk-personal:nope "
            "@com8::dirk-personal:shared @com8::dirk-personal:neo4j-ops",
            registry=reg,
            send=lambda target, bus, message, sender="self", hub=None: sent.append(target) or {"id": "m_1", "status": "accepted", "target": target},
            limiter=mentions.RateLimiter(None),
        )
        self.assertEqual(len(sent), 1)
        codes = {err["code"] for err in result["errors"]}
        self.assertEqual(codes, {"unknown_bus", "unknown_session", "ambiguous"})
        self.assertFalse(result["ok"])

    def test_per_prompt_cap_sends_nothing(self):
        rows = [agent("s%d" % i) for i in range(3)]
        reg = registry(("general", rows))
        prompt = " ".join("@com8::general:s%d" % i for i in range(3))
        sent = []
        result = mentions.route(
            prompt, registry=reg,
            send=lambda target, bus, message, sender="self", hub=None: sent.append(target),
            limiter=mentions.RateLimiter(None, per_prompt=2),
        )
        self.assertEqual(sent, [])
        self.assertEqual(result["deliveries"], [])
        self.assertEqual(result["errors"][0]["code"], "rate_limited")
        self.assertEqual(result["errors"][0]["scope"], "prompt")

    def test_window_refuses_without_sending(self):
        path = Path(tempfile.mkdtemp()) / "rate.json"
        limiter = mentions.RateLimiter(path, limit=1, window=60, now=lambda: 5000)
        reg = dirk_registry()
        send = mock.Mock(return_value={"id": "m_1", "status": "accepted", "target": "t"})
        first = mentions.route("@com8::dirk-personal:neo4j-ops", registry=reg, send=send, limiter=limiter)
        second = mentions.route("@com8::dirk-personal:fleet-ops", registry=reg, send=send, limiter=limiter)
        self.assertTrue(first["ok"])
        self.assertEqual(send.call_count, 1)
        self.assertFalse(second["sent"])
        self.assertTrue(any(err["code"] == "rate_limited" for err in second["errors"]))
        stored = json.loads(path.read_text())
        self.assertEqual(set(stored), {"stamps"})
        self.assertNotIn("neo4j", path.read_text())

    def test_corrupt_rate_file_fails_closed(self):
        path = Path(tempfile.mkdtemp()) / "rate.json"
        path.write_text("{not json invite=commbus1.SECRET")
        sent = []
        result = mentions.route(
            "@com8::dirk-personal:neo4j-ops", registry=dirk_registry(),
            send=lambda **kwargs: sent.append(kwargs),
            limiter=mentions.RateLimiter(path),
        )
        self.assertEqual(sent, [])
        blob = json.dumps(result)
        self.assertNotIn("commbus1", blob)
        self.assertNotIn("SECRET", blob)
        self.assertIn("rate_limited", blob)

    def test_send_exception_is_redacted(self):
        def send(target, bus, message, sender="self", hub=None):
            raise RuntimeError("broker said commbus1.SECRETTOKEN and socket=/tmp/a.sock")

        result = mentions.route(
            "@com8::dirk-personal:neo4j-ops", registry=dirk_registry(), send=send,
            limiter=mentions.RateLimiter(None),
        )
        blob = json.dumps(result)
        self.assertNotIn("SECRETTOKEN", blob)
        self.assertNotIn("commbus1", blob)
        self.assertNotIn("/tmp/a.sock", blob)
        self.assertIn("delivery refused", blob)
        self.assertFalse(result["deliveries"][0]["sent"])

    def test_oversize_prompt_is_not_truncated_or_sent(self):
        sent = []
        prompt = "@com8::dirk-personal:neo4j-ops " + ("x" * mentions.MAX_MESSAGE)
        result = mentions.route(
            prompt, registry=dirk_registry(),
            send=lambda **kwargs: sent.append(1),
            limiter=mentions.RateLimiter(None),
        )
        self.assertEqual(sent, [])
        self.assertEqual(result["errors"][0]["code"], "too_large")

    def test_message_limit_matches_the_broker(self):
        import bus_broker
        self.assertEqual(mentions.MAX_MESSAGE, bus_broker.MAX_MESSAGE)

    def test_credential_in_hub_is_refused(self):
        sent = []
        result = mentions.route(
            "@com8::dirk-personal:neo4j-ops", registry=dirk_registry(),
            send=lambda **kwargs: sent.append(1),
            hub="https://user:token@bus.nonlocally.org/path?x=1",
            limiter=mentions.RateLimiter(None),
        )
        self.assertEqual(sent, [])
        self.assertEqual(result["errors"][0]["code"], "invalid_hub")
        self.assertNotIn("token", json.dumps(result))

    def test_invalid_mention_does_not_contact_the_bus(self):
        with mock.patch.object(mentions, "load_registry", side_effect=AssertionError("bus")):
            result = mentions.route("@com8::NOPE")
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "invalid_mention")

    def test_router_does_not_type_into_a_pane(self):
        text = (ROOT / "lib" / "mentions.py").read_text()
        for banned in ("send-keys", "tmux", "codex queue", "messagingSocketPath"):
            self.assertNotIn(banned, text)


class CompletionTests(unittest.TestCase):
    def test_handler_and_bus_suggestions(self):
        reg = dirk_registry()
        handlers = mentions.complete("@com", reg)["completions"]
        self.assertEqual(handlers[0]["text"], "@com8::")
        self.assertEqual(handlers[0]["kind"], "handler")
        buses = mentions.complete("com8::dirk", reg)["completions"]
        self.assertEqual([row["text"] for row in buses], ["com8::dirk-personal:"])
        self.assertEqual(buses[0]["kind"], "bus")
        bare = mentions.complete("@com8::", reg)["completions"]
        self.assertEqual(
            [row["bus"] for row in bare],
            ["dirk-personal", "other-bus"],
        )

    def test_sessions_are_live_and_ranked_by_recent_activity(self):
        reg = dirk_registry()
        found = mentions.complete("@com8::dirk-personal:", reg)["completions"]
        names = [row["name"] for row in found]
        self.assertEqual(names, ["neo4j-ops", "fleet-ops", "wilde-qit", "wilde-qit-astra"])
        self.assertNotIn("quiet", names)
        self.assertEqual(found[0]["text"], "@com8::dirk-personal:neo4j-ops")
        self.assertGreater(found[0]["last_seen"], found[1]["last_seen"])
        prefixed = mentions.complete("@com8::dirk-personal:wilde", reg)["completions"]
        self.assertEqual([row["name"] for row in prefixed], ["wilde-qit", "wilde-qit-astra"])

    def test_equal_activity_is_ordered_by_name(self):
        reg = registry(("general", [
            agent("zeta", last_seen=5, status="live", kind="claude"),
            agent("alpha", last_seen=5, status="queueable"),
            agent("mid", last_seen=9, status="offline"),
        ]))
        found = mentions.complete("com8::general:", reg)["completions"]
        self.assertEqual([row["name"] for row in found], ["alpha", "zeta"])

    def test_unknown_bus_and_secrets_yield_nothing_sensitive(self):
        reg = registry(("dirk-personal", [
            agent("neo4j-ops", status="live", kind="claude", invite="commbus1.SECRET", socket="/tmp/a.sock"),
        ]))
        self.assertEqual(mentions.complete("@com8::missing:", reg)["completions"], [])
        blob = json.dumps(mentions.complete("@com8::dirk-personal:", reg))
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("socket", blob)
        self.assertNotIn("commbus1", blob)

    def test_leading_punctuation_is_preserved(self):
        found = mentions.complete("(@com8::dir", dirk_registry())["completions"]
        self.assertEqual(found[0]["text"], "(@com8::dirk-personal:")

    def test_completion_cap(self):
        rows = [agent("s%02d" % i, last_seen=1000 - i, status="live", kind="claude") for i in range(60)]
        reg = registry(("general", rows))
        found = mentions.complete("com8::general:", reg)["completions"]
        self.assertEqual(len(found), mentions.MAX_COMPLETIONS)
        self.assertEqual(found[0]["name"], "s00")


class ExistingSendTests(unittest.TestCase):
    def test_existing_bus_send_calls_bus_run_and_strips_the_receipt(self):
        import bus as bus_client
        receipt = {
            "id": "m_1", "status": "accepted", "target": "a_1", "token": "sekret",
            "expires_at": 1, "conversation_id": "c_1", "device_metadata": {"hostname": "nope"},
        }

        def fake_run(args):
            self.assertEqual(args.command, "send")
            self.assertEqual(args.target, "a_1")
            self.assertEqual(args.bus, "dirk-personal")
            self.assertEqual(args.sender, "self")
            self.assertEqual(args.hub, "https://bus.nonlocally.org")
            self.assertEqual(args.message, ["hello\nthere"])
            print(json.dumps(receipt))

        with mock.patch.object(bus_client, "run", fake_run):
            got = mentions.existing_bus_send(
                "a_1", "dirk-personal", "hello\nthere", sender="self",
                hub="https://bus.nonlocally.org",
            )
        self.assertEqual(got["id"], "m_1")
        self.assertEqual(got["conversation_id"], "c_1")
        self.assertNotIn("token", got)
        self.assertNotIn("device_metadata", got)


class CliTests(unittest.TestCase):
    def test_dry_run_json_reads_stdin(self):
        out = io.StringIO()
        code = mentions.main(
            ["route", "--dry-run", "--json"],
            registry=dirk_registry(),
            limiter=mentions.RateLimiter(None),
            stdin=io.StringIO(PROMPT),
            stdout=out,
        )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(len(payload["deliveries"]), 4)
        self.assertTrue(payload["dry_run"])
        self.assertNotIn("pls make sure", out.getvalue())

    def test_complete_text(self):
        out = io.StringIO()
        code = mentions.main(
            ["complete", "--text", "@com8::dirk-personal:"],
            registry=dirk_registry(),
            stdout=out,
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().splitlines()[0], "@com8::dirk-personal:neo4j-ops")

    def test_complete_loads_the_registry_when_needed(self):
        reg = dirk_registry()
        out = io.StringIO()
        with mock.patch.object(mentions, "load_registry", return_value=reg) as load:
            code = mentions.main(["complete", "com8::"], stdout=out)
        self.assertEqual(code, 0, out.getvalue())
        load.assert_called_once()
        self.assertIn("com8::dirk-personal:", out.getvalue())

    def test_cli_invalid_mention_does_not_contact_the_bus(self):
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch.object(mentions, "load_registry", side_effect=AssertionError("bus")):
            code = mentions.main(
                ["route", "--json"],
                stdin=io.StringIO("@com8::NOPE\n"),
                stdout=out,
                stderr=err,
            )
        self.assertEqual(code, 1)
        self.assertIn("invalid_mention", out.getvalue())
        self.assertNotIn("NOPE", err.getvalue())

    def test_help_lists_route_and_complete(self):
        out = io.StringIO()
        code = mentions.main(["--help"], stdout=out)
        self.assertEqual(code, 0)
        self.assertIn("route", out.getvalue())
        self.assertIn("complete", out.getvalue())

    def test_completion_scripts_parse(self):
        checked = 0
        for name, shell in (("mentions-completion.bash", "bash"), ("mentions-completion.zsh", "zsh")):
            path = ROOT / "lib" / name
            binary = subprocess.run(["bash", "-lc", "command -v %s" % shell], capture_output=True, text=True)
            if binary.returncode != 0:
                continue
            result = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mentions complete", path.read_text())
            checked += 1
        self.assertGreaterEqual(checked, 1)

    def test_default_limiter_keeps_only_timestamps_in_bus_state(self):
        import bus as bus_client
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bus_client, "state_dir", return_value=Path(tmp)):
                limiter = mentions.default_limiter()
            self.assertEqual(limiter.path, Path(tmp) / "mentions-rate.json")
            self.assertEqual(limiter.limit, 30)
            self.assertEqual(limiter.per_prompt, 16)


if __name__ == "__main__":
    unittest.main()
