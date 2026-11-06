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
        # The harness keeps every distinct error the worker published during the run. A transient
        # one (a hub request that timed out or was refused, a retry scheduled) is not a defect: the
        # distinct counts and the absence of duplicate deliveries above are the correctness gates.
        transient = ("hub unreachable", "hub rejected request (HTTP 503)", "service endpoint unavailable",
                     "service reply unavailable; pending")
        unexpected = [item for item in report["worker"]["errors"] if not str(item.get("error", "")).startswith(transient)]
        self.assertEqual(unexpected, [])

    def test_hub_operations_are_the_workers_round_trips_and_each_lease_is_used_once(self):
        report, hub = self.report, self.report["hub"]
        sent = sorted(item["id"] for item in report["timeline"]["sends"])
        # Every message was acknowledged as queued and answered once; a retried acknowledgment or a reply
        # replayed by request_id after a slow hub is one more operation, not another delivery or answer.
        self.assertEqual(hub["ack"]["distinct"], len(sent))
        self.assertEqual(set(hub["ack"]["statuses"]), {"queued"})
        self.assertGreaterEqual(hub["ack"]["count"], len(sent))
        self.assertEqual(hub["reply"]["distinct"], len(sent))
        self.assertGreaterEqual(hub["reply"]["count"], len(sent))
        leased = sorted({mid for event in report["timeline"]["hub"] if event["op"] == "poll_device" for mid in event["ids"]})
        self.assertEqual(leased, sent)
        # At most once per lease: whatever the hub re-offered, the service received each message once.
        self.assertEqual(report["service"]["duplicate_delivers"], 0)

    def test_service_messages_are_leased_in_batches_well_above_the_old_rate(self):
        poll, service = self.report["hub"]["poll_device"], self.report["service"]
        self.assertTrue(poll["batch_requested"])
        self.assertGreaterEqual(poll["lease_size"]["max"], 4)
        self.assertLess(poll["count"], self.MESSAGES)
        self.assertIsNotNone(service["rate_per_minute"])
        # The number is a measurement (hundreds a minute on this loopback hub, see the summary); the bound
        # only guards against falling back to one lease per two-second poll, which was 29.5 a minute.
        self.assertGreaterEqual(service["rate_per_minute"], 60, self.harness.summarize(self.report))
        print("\n" + self.harness.summarize(self.report))

    def test_summary_names_the_numbers(self):
        text = self.harness.summarize(self.report)
        for fragment in ("poll_device", "lease size max", "delivery rate at the service", "latency send->deliver"):
            self.assertIn(fragment, text)
        self.assertNotIn("measure-bridge-token", json.dumps(self.report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
