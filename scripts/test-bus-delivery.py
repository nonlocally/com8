#!/usr/bin/env python3
"""The delivery-cadence harness measures a real worker against an isolated hub.

Runs scripts/measure-bus-delivery.py with a few messages and checks that its
numbers are the worker's own: every sent message reaches the fake service
exactly once, every bridge call and hub operation is accounted for, and the
cadence it reports is the loop's (one device poll per iteration, at most one
lease per service target). Isolated temporary state only; no network.
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


class DeliveryCadence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = load_harness()
        cls.state = tempfile.TemporaryDirectory(prefix="bus-delivery-test-")
        cls.report = cls.harness.measure(messages=6, window=1.5, mode="poll", timeout=180, state=cls.state.name)

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

    def test_hub_operations_are_the_workers_round_trips(self):
        report, hub = self.report, self.report["hub"]
        count = report["messages"]["sent"]
        # One ack and one reply per message; every lease was acknowledged as queued.
        self.assertEqual(hub["ack"]["count"], count)
        self.assertEqual(hub["ack"]["statuses"], {"queued": count})
        self.assertEqual(hub["reply"]["count"], count)
        leased = [mid for event in report["timeline"]["hub"] if event["op"] == "poll_device" for mid in event["ids"]]
        self.assertEqual(sorted(leased), sorted(item["id"] for item in report["timeline"]["sends"]))
        self.assertGreaterEqual(hub["operations_per_message"], 2)

    def test_measured_cadence_is_the_loops_poll_and_single_lease(self):
        poll = self.report["hub"]["poll_device"]
        self.assertGreaterEqual(poll["count"], self.report["messages"]["sent"])
        # The worker polls once per loop iteration, which waits two seconds, and
        # the hub leases at most one message per target per poll.
        self.assertGreaterEqual(poll["interval_s"]["p50"], 2.0)
        self.assertEqual(poll["lease_size"]["max"], 1)
        self.assertIsNotNone(self.report["service"]["rate_per_minute"])
        self.assertLess(self.report["service"]["rate_per_minute"], 60)

    def test_summary_names_the_numbers(self):
        text = self.harness.summarize(self.report)
        for fragment in ("poll_device", "lease size max 1", "delivery rate at the service", "latency send->deliver"):
            self.assertIn(fragment, text)
        self.assertNotIn("measure-bridge-token", json.dumps(self.report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
