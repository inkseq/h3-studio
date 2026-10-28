"""Webhook counters are bounded aggregates, behind the original loopback guard."""

import asyncio
import json
from types import SimpleNamespace
import unittest

from studio_platform.alert_http import AlertCountersMiddleware, HTTPAlertCounters
from studio_platform.payment_http import PATH, PaymentAlertCounters, receive_webhook
from studio_platform.stripe_client import StripeGatewayError


class PaymentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.counters = PaymentAlertCounters(clock=lambda: self.now)

    def test_counter_window_rollback_and_closed_categories(self):
        self.counters.record("signature_failures")
        self.now += 300
        self.counters.record("processing_failures")
        self.assertEqual(self.counters.snapshot()["signature_failures"], 0)
        self.assertEqual(
            self.counters.snapshot(window_seconds=600)["signature_failures"], 1
        )
        self.now -= 100
        self.assertEqual(self.counters.snapshot()["processing_failures"], 0)
        for window in (True, 0, 59, 3601):
            with self.assertRaises(ValueError):
                self.counters.snapshot(window_seconds=window)
        with self.assertRaises(ValueError):
            self.counters.record("PRIVATE signature")
        self.assertNotIn("PRIVATE", repr(vars(self.counters)))

    def test_signature_failure_does_not_call_original_core_or_retain_request(self):
        def verify(raw, signature):
            raise StripeGatewayError("billing_signature_invalid", 400)

        def forbidden(event):
            self.fail("unverified receipt must not reach the payment core")

        service = SimpleNamespace(
            gateway=SimpleNamespace(verify_event=verify),
            core=SimpleNamespace(receive_event=forbidden),
            payment_alert_counters=self.counters,
        )
        with self.assertRaises(StripeGatewayError):
            receive_webhook(service, b"PRIVATE body", "PRIVATE signature")
        self.assertEqual(self.counters.snapshot()["signature_failures"], 1)
        self.assertNotIn("PRIVATE", repr(vars(self.counters)))

    def test_verified_receipt_success_and_processing_failure_keep_original_behavior(
        self,
    ):
        events = []
        service = SimpleNamespace(
            gateway=SimpleNamespace(verify_event=lambda *_: {"id": "evt_original"}),
            core=SimpleNamespace(receive_event=events.append),
            payment_alert_counters=self.counters,
        )
        self.assertEqual(receive_webhook(service, b"raw", "sig"), {"received": True})
        self.assertEqual(events, [{"id": "evt_original"}])

        def failed(event):
            raise RuntimeError("PRIVATE processing error")

        service.core.receive_event = failed
        with self.assertRaises(RuntimeError):
            receive_webhook(service, b"PRIVATE body", "PRIVATE signature")
        self.assertEqual(self.counters.snapshot()["processing_failures"], 1)
        self.assertNotIn("PRIVATE", repr(vars(self.counters)))

    def test_unexpected_verification_failure_is_processing_without_raw_error(self):
        def failed(*args):
            raise RuntimeError("PRIVATE provider verification error")

        service = SimpleNamespace(
            gateway=SimpleNamespace(verify_event=failed),
            core=SimpleNamespace(
                receive_event=lambda event: self.fail('unverified receipt')
            ),
            payment_alert_counters=self.counters,
        )
        with self.assertRaises(RuntimeError):
            receive_webhook(service, b"PRIVATE body", "PRIVATE signature")
        self.assertEqual(self.counters.snapshot()["processing_failures"], 1)
        self.assertNotIn("PRIVATE", repr(vars(self.counters)))

    def request(
        self, *, peer="127.0.0.1", headers=(), method="GET", query=b"", available=True
    ):
        async def app(scope, receive, send):
            self.fail("internal counters must not reach application routing")

        counts = HTTPAlertCounters(clock=lambda: self.now)
        middleware = AlertCountersMiddleware(app, counters=counts)
        payments = (
            SimpleNamespace(payment_alert_counters=self.counters) if available else None
        )
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"PRIVATE body"}

        scope = {
            "type": "http",
            "path": PATH,
            "client": (peer, 1),
            "headers": headers,
            "method": method,
            "query_string": query,
            "app": SimpleNamespace(state=SimpleNamespace(payments=payments)),
        }
        asyncio.run(middleware(scope, receive, send))
        self.assertEqual(counts.snapshot()["requests"], 0)
        return sent[0]["status"], json.loads(sent[1]["body"])

    def test_separate_loopback_contract_and_legacy_counters_unchanged(self):
        self.counters.record("signature_failures")
        status, value = self.request(query=b"window_seconds=600")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(value),
            {"epoch", "window_seconds", "signature_failures", "processing_failures"},
        )
        self.assertEqual(value["window_seconds"], 600)
        self.assertEqual(value["signature_failures"], 1)
        for peer in ("203.0.113.1", "localhost"):
            self.assertEqual(self.request(peer=peer)[0], 403)
        for header in (b"forwarded", b"x-forwarded-for", b"x-real-ip"):
            self.assertEqual(self.request(headers=[(header, b"127.0.0.1")])[0], 403)
        self.assertEqual(self.request(method="POST")[0], 405)
        self.assertEqual(self.request(available=False)[0], 503)
        for query in (
            b"window_seconds=0",
            b"window_seconds=3601",
            b"window_seconds=300&x=1",
            b"PRIVATE",
        ):
            self.assertEqual(self.request(query=query)[0], 503)


if __name__ == "__main__":
    unittest.main()
