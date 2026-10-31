"""Payment alarms use original evidence age, and unknown never means resolved."""

import copy
import unittest
import uuid

from studio_platform.alert_conditions import evaluate


class PaymentConditionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.identity = str(uuid.uuid4())
        self.state = None
        self.payment = {
            "complete": True,
            "fulfillment": [],
            "processing": [],
            "window_seconds": 300,
            "activity": {
                name: {"count": 0, "since": None}
                for name in ("payment_failed", "payment_refunded", "payment_disputed")
            },
        }

    def run_tick(self, *, payment=True, host=None, thresholds=None):
        snapshot = {
            "version": 1,
            "observed_at": self.now,
            "complete": True,
            "gpus": [],
            "jobs": [],
            "failures": [],
            "test_spend": None,
        }
        if payment:
            snapshot["payments"] = copy.deepcopy(self.payment)
        result = evaluate(
            snapshot,
            self.state,
            environment="staging",
            now=self.now,
            host=host,
            thresholds=thresholds,
        )
        self.state = result["state"]
        return result["notifications"]

    def test_fulfillment_and_processing_default_five_minutes_and_one_transition(self):
        self.payment['fulfillment'] = [{"id": self.identity, "since": 701}]
        self.assertEqual(self.run_tick(), [])
        self.now += 1
        fired = self.run_tick()
        self.assertEqual(
            [(r['code'], r['resource_id']) for r in fired],
            [('payment_fulfillment_stalled', self.identity)],
        )
        self.now += 60
        self.assertEqual(self.run_tick(), [])
        self.payment['fulfillment'] = []
        self.payment['processing'] = [{"id": self.identity, "since": 701}]
        result = self.run_tick()
        self.assertEqual(
            {(r['code'], r['transition']) for r in result},
            {
                ('payment_fulfillment_stalled', 'resolved'),
                ('payment_processing_stalled', 'firing'),
            },
        )

    def test_unknown_or_old_reader_does_not_resolve_purchase_incident(self):
        self.payment['fulfillment'] = [{"id": self.identity, "since": 1}]
        self.run_tick()
        self.now += 60
        self.assertEqual(self.run_tick(payment=False), [])
        self.payment['complete'] = False
        result = self.run_tick()
        self.assertEqual([r['code'] for r in result], ['payment_monitor_unavailable'])
        self.now += 60
        self.assertEqual(self.run_tick(), [])
        self.payment['complete'] = True
        self.payment['fulfillment'] = []
        result = self.run_tick()
        self.assertEqual(
            {(r['code'], r['transition']) for r in result},
            {
                ('payment_monitor_unavailable', 'resolved'),
                ('payment_fulfillment_stalled', 'resolved'),
            },
        )

    def test_custom_grace_and_activity_count_window_are_bounded(self):
        self.payment['fulfillment'] = [{"id": self.identity, "since": self.now - 60}]
        self.payment['activity']['payment_refunded'] = {
            'count': 2,
            'since': self.now - 5,
        }
        result = self.run_tick(
            thresholds={'payment_fulfillment_seconds': 60, 'payment_failure_count': 2}
        )
        self.assertEqual(
            {r['code'] for r in result},
            {'payment_fulfillment_stalled', 'payment_refunded'},
        )
        for value in (0, 59, 3601, True, 60.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_tick(thresholds={'payment_window_seconds': value})

    def test_signature_and_receipt_counts_unknown_preserve_active_then_recover(self):
        result = self.run_tick(
            host={'payment_signature_failures': 1, 'payment_receipt_failures': 1}
        )
        self.assertEqual(
            {r['code'] for r in result},
            {'payment_webhook_signature_failed', 'payment_webhook_receipt_failed'},
        )
        self.now += 60
        self.assertEqual(self.run_tick(host={}), [])
        result = self.run_tick(
            host={'payment_signature_failures': 0, 'payment_receipt_failures': 0}
        )
        self.assertEqual({r['transition'] for r in result}, {'resolved'})

    def test_old_snapshot_contract_still_evaluates_legacy_backup_and_gpu(self):
        result = self.run_tick(payment=False, host={'business_backup_failed': True})
        self.assertEqual([r['code'] for r in result], ['business_backup_failed'])
        self.assertNotIn('payment', repr(result))


if __name__ == '__main__':
    unittest.main()
