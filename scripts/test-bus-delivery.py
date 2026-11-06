#!/usr/bin/env python3
"""A service recipient receives hundreds of messages a minute from the real worker.

Runs scripts/measure-bus-delivery.py (an isolated TLS hub, a fake service
answering at once, and this checkout's outbound worker) with 100 messages sent
in 10 s, then checks the numbers are the worker's own: every sent message is
leased, delivered and answered exactly once, every hub operation is accounted
for, service messages are leased in batches and the rate at the service is in
the hundreds a minute. Isolated temporary state only; no network.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_harness():
    spec = importlib.util.spec_from_file_location("measure_bus_delivery", ROOT / "scripts/measure-bus-delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeliveryRate(unittest.TestCase):
    MESSAGES = 100

    @classmethod
    def setUpClass(cls):
        cls.harness = load_harness()
        cls.state = tempfile.TemporaryDirectory(prefix="bus-delivery-test-")
        cls.report = cls.harness.measure(messages=cls.MESSAGES, window=10, mode="poll", timeout=240, state=cls.state.name)

    @classmethod
    def tearDownClass(cls):
        cls.state.cleanup()

    def test_every_message_reaches_the_service_exactly_once_and_is_answered(self):
        report = self.report
        self.assertTrue(report["parameters"]["complete"], json.dumps(report["worker"]))
        sent = [item["id"] for item in report["timeline"]["sends"]]
        delivers = [event["id"] for event in report["timeline"]["bridge"] if event["op"] == "deliver"]
        self.assertEqual(sorted(delivers), sorted(sent))
        self.assertEqual(report["service"]["duplicate_delivers"], 0)
        self.assertEqual(report["service"]["distinct_delivered"], len(sent))
        # The dispatch-service pattern: deliver answers pending, the first poll answers.
        self.assertEqual(report["service"]["poll"], len(sent))
        self.assertEqual(report["messages"]["replied"], len(sent))
        self.assertEqual(report["messages"]["terminal"], {"queued": len(sent)})
        self.assertEqual(report["worker"]["errors"], [])

    def test_hub_operations_are_the_workers_round_trips_and_each_lease_is_used_once(self):
        report, hub = self.report, self.report["hub"]
        count = report["messages"]["sent"]
        # One acknowledgment and one reply per message; every lease was acknowledged as queued.
        self.assertEqual(hub["ack"]["count"], count)
        self.assertEqual(hub["ack"]["statuses"], {"queued": count})
        self.assertEqual(hub["reply"]["count"], count)
        leased = [mid for event in report["timeline"]["hub"] if event["op"] == "poll_device" for mid in event["ids"]]
        # At most once per lease: no message was leased twice, since every lease was acknowledged in time.
        self.assertEqual(sorted(leased), sorted(item["id"] for item in report["timeline"]["sends"]))

    def test_service_messages_are_leased_in_batches_at_hundreds_a_minute(self):
        poll, service = self.report["hub"]["poll_device"], self.report["service"]
        self.assertTrue(poll["batch_requested"])
        self.assertGreaterEqual(poll["lease_size"]["max"], 8)
        self.assertLess(poll["count"], self.MESSAGES)
        self.assertIsNotNone(service["rate_per_minute"])
        self.assertGreaterEqual(service["rate_per_minute"], 200, self.harness.summarize(self.report))
        self.assertLess(self.report["messages"]["latency_s"]["send_to_deliver"]["max"], 30)

    def test_summary_names_the_numbers(self):
        text = self.harness.summarize(self.report)
        for fragment in ("poll_device", "lease size max", "delivery rate at the service", "latency send->deliver"):
            self.assertIn(fragment, text)
        self.assertNotIn("measure-bridge-token", json.dumps(self.report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
