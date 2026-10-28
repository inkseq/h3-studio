"""Root probe compatibility with finite payment observations and old snapshots."""

import ast
import json
import subprocess
import unittest
from unittest.mock import patch

from deploy.platform import alert_monitor as monitor
from studio_platform.alert_conditions import CODES


class PaymentMonitorReaderTests(unittest.TestCase):
    def observe(self, *, unavailable=False, payment_error=None):
        snapshot = {
            "version": 1,
            "complete": True,
            "observed_at": 1000,
            "gpus": [{'id': 'original-gpu-obligation'}],
            "jobs": [{'id': 'original-job-obligation'}],
            "failures": [],
            "test_spend": None,
        }
        calls = []
        payments = {
            'complete': True,
            'fulfillment': [],
            'processing': [],
            'activity': {},
            'window_seconds': 600,
        }

        def docker(args, **kwargs):
            calls.append(args)
            if monitor.PAYMENT_HTTP_PROBE in args:
                self.assertEqual(args[-1], '600')
                if unavailable:
                    raise ValueError('PRIVATE unavailable response')
                return json.dumps(
                    {'payment_signature_failures': 2, 'payment_receipt_failures': 1}
                )
            if monitor.HTTP_PROBE in args:
                return json.dumps(
                    {'health_ok': True, 'http_requests': 9, 'http_5xx': 0}
                )
            if 'studio_platform.payment_alerts' in args:
                self.assertEqual(args[-2:], ['--window-seconds', '600'])
                if payment_error is not None:
                    raise payment_error
                return json.dumps(payments)
            self.assertEqual(args[-1], 'studio_platform.alert_snapshot')
            return json.dumps(snapshot)

        with (
            patch.object(
                monitor,
                'proc_sample',
                return_value={'total': 1000, 'idle': 900, 'memory_percent': 20},
            ),
            patch.object(monitor, 'retention_service_failed', return_value=False),
            patch.object(monitor, 'backup_observation', return_value={}),
            patch.object(monitor, 'disk_sample', return_value=30),
            patch.object(
                monitor,
                'containers',
                return_value={'app': {'id': 'fixed-app', 'ok': True}},
            ),
            patch.object(monitor, 'docker', side_effect=docker),
        ):
            result, host, _ = monitor.observe(None, payment_window_seconds=600)
        self.assertEqual(
            result,
            {
                **snapshot,
                'payments': payments if payment_error is None else {'complete': False},
            },
        )
        self.last_snapshot = result
        self.assertTrue(host['health_ok'])
        self.assertTrue(host['processes_ok'])
        self.assertEqual(host['http_requests'], 9)
        self.assertEqual(len(calls), 4)
        return host

    def test_payment_probe_is_separate_and_window_reaches_sql_and_counter_readers(self):
        ast.parse(monitor.PAYMENT_HTTP_PROBE)
        host = self.observe()
        self.assertEqual(host['payment_signature_failures'], 2)
        self.assertEqual(host['payment_receipt_failures'], 1)

    def test_unknown_payment_probe_does_not_hide_gpu_or_legacy_host_health(self):
        host = self.observe(unavailable=True)
        self.assertIsNone(host.get('payment_signature_failures'))
        self.assertIsNone(host.get('payment_receipt_failures'))
        self.assertNotIn('PRIVATE', json.dumps(host))

    def test_slow_or_failed_payment_sql_cannot_discard_original_gpu_job_snapshot(self):
        for error in (
            subprocess.TimeoutExpired('optional-payment-probe', 10),
            ValueError('PRIVATE malformed payment response'),
        ):
            with self.subTest(error=type(error).__name__):
                host = self.observe(payment_error=error)
                self.assertTrue(self.last_snapshot['complete'])
                self.assertEqual(
                    self.last_snapshot['gpus'], [{'id': 'original-gpu-obligation'}]
                )
                self.assertEqual(
                    self.last_snapshot['jobs'], [{'id': 'original-job-obligation'}]
                )
                self.assertEqual(self.last_snapshot['payments'], {'complete': False})
                self.assertTrue(host['health_ok'])
                self.assertEqual(host['payment_signature_failures'], 2)
                self.assertNotIn('PRIVATE', json.dumps(self.last_snapshot))

    def test_root_notification_guard_accepts_only_the_evaluator_closed_codes(self):
        for code in CODES:
            result = monitor.notification(
                {
                    'code': code,
                    'environment': 'staging',
                    'resource_id': None,
                    'duration_seconds': 300,
                    'console_link': 'https://staging.inkseq.com/freestyle',
                    'transition': 'firing',
                },
                'staging',
            )
            self.assertEqual(result['code'], code)


if __name__ == '__main__':
    unittest.main()
