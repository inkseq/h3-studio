"""Finite monitor tests: no AWS, Docker, provider or business database writes."""
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import EndpointConnectionError
from deploy.platform import alert_monitor as monitor
from studio_platform.alert_conditions import CODES, DEFAULTS, evaluate


SETTINGS = {'version': 1, 'environment': 'staging', 'instance_id': 'i-07805df7bd6984bd9',
    'topic_arn': 'arn:aws:sns:ap-southeast-1:829135631045:sixnine-staging-operator-alerts', 'thresholds': {}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.sns = Mock()
        self.sns.publish.return_value = {'MessageId': 'synthetic-message-id'}
        self.cloud = Mock()
        self.state = monitor.initial_state('staging')
        self.saved = []

    def tick(self, now, **host):
        snapshot = {'version': 1, 'observed_at': now, 'complete': True,
            'gpus': [], 'jobs': [], 'failures': [], 'test_spend': None}
        def save(value):
            self.saved.append(copy.deepcopy(value))
        self.state = monitor.tick(SETTINGS, self.state, snapshot,
            {'health_ok': True, 'processes_ok': True, 'cpu_percent': 10, 'memory_percent': 20,
             'http_requests': 10, 'http_5xx': 0, **host}, None, evaluator=evaluate, sns=self.sns,
            cloudwatch=self.cloud, save=save, now=now)
        return self.state

    def test_first_sustained_condition_clear_and_restart_are_deduplicated(self):
        for now in range(1000, 1901, 60):
            self.tick(now, cpu_percent=95)
        self.assertEqual(self.sns.publish.call_count, 1)
        alarm = json.loads(self.sns.publish.call_args.kwargs['Message'])
        self.assertEqual((alarm['code'], alarm['transition']), ('host_cpu_sustained', 'firing'))
        self.state = json.loads(monitor.encode(self.state))  # process restart
        self.tick(1960, cpu_percent=95)
        self.assertEqual(self.sns.publish.call_count, 1)
        self.tick(2020, cpu_percent=10)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.assertEqual(json.loads(self.sns.publish.call_args.kwargs['Message'])['transition'], 'resolved')
        self.tick(2080)
        self.assertEqual(self.sns.publish.call_count, 2)

    def test_unknown_ack_persists_pending_before_publish_and_retry_after_restart(self):
        self.sns.publish.side_effect = EndpointConnectionError(endpoint_url='SYNTHETIC_PRIVATE')
        self.tick(1000, http_requests=10, http_5xx=5)
        self.assertEqual(len(self.state['pending']), 1)
        self.assertEqual(self.state['pending'][0]['attempts'], 1)
        pending = self.saved[-1]
        self.assertEqual(len(pending['pending']), 1)
        event_id = pending['pending'][0]['id']
        self.state = json.loads(monitor.encode(pending))
        self.tick(1030, http_requests=10, http_5xx=5)
        self.assertEqual(self.sns.publish.call_count, 1)
        self.sns.publish.side_effect = None
        self.tick(1060, http_requests=10, http_5xx=5)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.assertFalse(self.state['pending'])
        self.assertEqual(self.saved[-2]['pending'][0]['id'], event_id)
        self.tick(1120, http_requests=10, http_5xx=5)
        self.assertEqual(self.sns.publish.call_count, 2)

    def test_unknown_observation_does_not_clear_active_alarm(self):
        self.tick(1000, health_ok=False)
        self.tick(1060, health_ok=False)
        self.tick(1120, health_ok=False)
        self.assertEqual(self.sns.publish.call_count, 1)
        self.tick(1180, health_ok=None)
        self.assertEqual(self.sns.publish.call_count, 1)
        self.tick(1240, health_ok=True)
        self.assertEqual(self.sns.publish.call_count, 2)

    def test_disk_failure_precedes_publish(self):
        with self.assertRaises(OSError):
            monitor.tick(SETTINGS, self.state, None, {'http_requests': 10, 'http_5xx': 5}, None,
                evaluator=evaluate, sns=self.sns, cloudwatch=self.cloud,
                save=Mock(side_effect=OSError('disk full')), now=1000)
        self.sns.publish.assert_not_called()
        self.cloud.put_metric_data.assert_not_called()

    def test_corrupt_or_foreign_state_is_not_silently_reset(self):
        for state in ({}, {'version': 1}, {**self.state, 'environment': 'production'},
                      {**self.state, 'pending': [{'notification': 'PRIVATE'}]}):
            with self.assertRaises(ValueError):
                monitor.validate_state(state, 'staging')

    def test_sns_boundary_rejects_content_and_cross_environment_links(self):
        message = {'code': 'health_down', 'environment': 'staging', 'resource_id': None,
            'duration_seconds': 120, 'console_link': 'https://staging.inkseq.com/freestyle', 'transition': 'firing'}
        for code in CODES:
            monitor.notification({**message, 'code': code}, 'staging')
        for change in ({'prompt': 'PRIVATE'}, {'code': 'arbitrary'}, {'resource_id': 'PRIVATE'},
                       {'console_link': 'https://inkseq.com/freestyle'}, {'duration_seconds': float('nan')},
                       {'environment': 'production'}):
            with self.assertRaises(ValueError):
                monitor.notification({**message, **change}, 'staging')

    def test_destination_is_exact_scoped_topic_and_host(self):
        target = {'name': 'staging', 'instance_id': SETTINGS['instance_id']}
        monitor.config(SETTINGS, target)
        for change in ({'topic_arn': SETTINGS['topic_arn'].replace('staging', 'production')},
                       {'instance_id': 'i-' + 'a' * 17}, {'extra': True}):
            with self.assertRaises(ValueError):
                monitor.config({**SETTINGS, **change}, target)

    def test_configuration_thresholds_closed_and_numeric(self):
        target = {'name': 'staging', 'instance_id': SETTINGS['instance_id']}
        self.assertEqual(monitor.THRESHOLD_FIELDS, set(DEFAULTS))
        monitor.config({**SETTINGS, 'thresholds': DEFAULTS}, target)
        for value in ({'unknown': 5}, {'cpu_percent': True}, {'memory_percent': 101},
                      {'http_5xx_ratio': 2}, {'stuck_seconds': float('inf')}, {'health_down_seconds': 0}):
            with self.assertRaisesRegex(ValueError, 'alert_thresholds_invalid'):
                monitor.config({**SETTINGS, 'thresholds': value}, target)

    def test_host_heartbeat_and_snapshot_health_are_distinct(self):
        monitor.tick(SETTINGS, self.state, None, {}, None, evaluator=evaluate, sns=self.sns,
            cloudwatch=self.cloud, save=lambda value: None, now=1000)
        metrics = {item['MetricName']: item['Value'] for item in self.cloud.put_metric_data.call_args.kwargs['MetricData']}
        self.assertEqual(metrics, {'MonitorHeartbeat': 1, 'DeliveryHealthy': 1, 'SnapshotHealthy': 0})

    def test_stale_snapshot_never_reports_healthy_snapshot(self):
        monitor.tick(SETTINGS, self.state, {'version': 1, 'complete': True, 'observed_at': 100}, {}, None,
            evaluator=evaluate, sns=self.sns, cloudwatch=self.cloud, save=lambda value: None, now=1000)
        metrics = {item['MetricName']: item['Value'] for item in self.cloud.put_metric_data.call_args.kwargs['MetricData']}
        self.assertEqual(metrics['SnapshotHealthy'], 0)

    def test_role_only_clients_exact_endpoints_and_bounded_retry(self):
        import boto3
        session = Mock()
        session.get_credentials.return_value.method = 'iam-role'
        with patch.object(boto3, 'Session', return_value=session):
            monitor.clients()
        calls = session.client.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['sns', 'cloudwatch'])
        self.assertEqual([call.kwargs['endpoint_url'] for call in calls],
            ['https://sns.ap-southeast-1.amazonaws.com', 'https://monitoring.ap-southeast-1.amazonaws.com'])
        for call in calls:
            options = call.kwargs['config']
            self.assertEqual((options.connect_timeout, options.read_timeout), (3, 5))
            self.assertEqual(options.retries['total_max_attempts'], 1)
        session.client.reset_mock()
        session.get_credentials.return_value.method = 'env'
        with patch.object(boto3, 'Session', return_value=session), self.assertRaisesRegex(ValueError, 'instance_role_required'):
            monitor.clients()
        session.client.assert_not_called()

    def test_proc_memory_and_cpu_delta_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / 'stat').write_text('cpu 100 0 100 800 0 0 0 0 0 0\n')
            (proc / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\n')
            with patch.object(monitor, 'containers', return_value={key: None for key in monitor.SERVICES}):
                _, host, cpu = monitor.observe({'total': 500, 'idle': 400}, proc=proc)
                self.assertAlmostEqual(host['cpu_percent'], 20)
                self.assertEqual(host['memory_percent'], 60)
                self.assertFalse(host['health_ok'])
                self.assertFalse(host['processes_ok'])
                _, host, _ = monitor.observe({'total': 2000, 'idle': 800}, proc=proc)
                self.assertIsNone(host['cpu_percent'])




if __name__ == '__main__':
    unittest.main()
