"""Finite monitor tests: no AWS, Docker, provider or business database writes."""

import copy
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import EndpointConnectionError
from deploy.platform import alert_monitor as monitor
from studio_platform.alert_conditions import CODES, DEFAULTS, evaluate


SETTINGS = {
    'version': 1,
    'environment': 'staging',
    'instance_id': 'i-07805df7bd6984bd9',
    'topic_arn': 'arn:aws:sns:ap-southeast-1:829135631045:sixnine-staging-operator-alerts',
    'thresholds': {},
}


class SnapshotObservationTests(unittest.TestCase):
    def observe(
        self,
        *,
        output=b'{}',
        error=b'',
        returncode=0,
        failure=None,
        stream=None,
        elapsed=0.125,
    ):
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=returncode, stdout=output, stderr=error
            ),
            side_effect=failure,
        )
        errors = stream if stream is not None else io.StringIO()
        with (
            patch.object(monitor.time, 'monotonic', side_effect=[10, 10 + elapsed]),
            redirect_stderr(errors),
        ):
            value = monitor.observe_snapshot('PRIVATE_CONTAINER', runner=runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(
            runner.call_args.args[0],
            [
                '/usr/bin/docker',
                'exec',
                'PRIVATE_CONTAINER',
                'python',
                '-B',
                '-m',
                'studio_platform.alert_snapshot',
            ],
        )
        self.assertEqual(
            runner.call_args.kwargs,
            {
                'stdin': subprocess.DEVNULL,
                'capture_output': True,
                'timeout': 10,
                'env': {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'},
            },
        )
        return value, errors

    def test_transport_failures_emit_bounded_outcome_without_child_content(self):
        for options, outcome in (
            (
                {
                    'failure': subprocess.TimeoutExpired(
                        'PRIVATE_COMMAND',
                        10,
                        output=b'PRIVATE_STDOUT',
                        stderr=b'PRIVATE_STDERR',
                    ),
                    'elapsed': 10,
                },
                'timeout',
            ),
            ({'failure': OSError('PRIVATE_PATH')}, 'process_failed'),
            (
                {
                    'returncode': 1,
                    'output': b'PRIVATE_STDOUT',
                    'error': b'PRIVATE_STDERR',
                },
                'process_failed',
            ),
            (
                {'output': b'PRIVATE_INVALID_JSON', 'error': b'PRIVATE_STDERR'},
                'invalid_output',
            ),
            ({'output': b'\xff'}, 'invalid_output'),
            ({'output': b'X' * (monitor.MAX_BYTES + 1)}, 'invalid_output'),
        ):
            with self.subTest(outcome=outcome, options=list(options)):
                value, errors = self.observe(**options)
                self.assertIsNone(value)
                self.assertEqual(
                    json.loads(errors.getvalue()),
                    {
                        'event': 'alert_snapshot_observation',
                        'outcome': outcome,
                        'category': 'unknown',
                        'snapshot_elapsed_ms': None,
                        'phase': 'unreported',
                        'phase_started_ms': None,
                        'elapsed_ms': 10000 if outcome == 'timeout' else 125,
                    },
                )
                self.assertNotIn('PRIVATE', errors.getvalue())
                self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_incomplete_snapshot_preserved_and_safe_failure_category_forwarded(self):
        original = {'version': 1, 'complete': False, 'jobs': [], 'gpus': []}
        value, errors = self.observe(
            output=json.dumps(original).encode(),
            error=b'{"category":"database","elapsed_ms":100}\n',
        )
        self.assertEqual(value, original)
        self.assertEqual(
            json.loads(errors.getvalue()),
            {
                'event': 'alert_snapshot_observation',
                'outcome': 'incomplete',
                'category': 'database',
                'snapshot_elapsed_ms': 100,
                'phase': 'unreported',
                'phase_started_ms': None,
                'elapsed_ms': 125,
            },
        )

    def test_old_images_and_untrusted_diagnostics_are_unknown_without_changing_result(
        self,
    ):
        original = {'version': 1, 'complete': False}
        for error in (
            b'',
            b'PRIVATE_ERROR',
            b'\xff',
            b'PRIVATE' * 100,
            b'{"category":"database","elapsed_ms":1}\nPRIVATE_EXTRA',
            b'{"category":"database","elapsed_ms":1,"PRIVATE":"SECRET"}',
            b'{"category":"PRIVATE_CATEGORY","elapsed_ms":1}',
            b'{"category":[],"elapsed_ms":1}',
            b'{"category":"database","elapsed_ms":true}',
            b'{"category":"database","elapsed_ms":-1}',
            b'{"category":"database","elapsed_ms":60001}',
            b'{"category":"database","elapsed_ms":1.5}',
            b'[]',
            b'null',
        ):
            with self.subTest(error=error[:60]):
                value, errors = self.observe(
                    output=json.dumps(original).encode(), error=error
                )
                self.assertEqual(value, original)
                diagnostic = json.loads(errors.getvalue())
                self.assertEqual(
                    (diagnostic['category'], diagnostic['snapshot_elapsed_ms']),
                    ('unknown', None),
                )
                self.assertNotIn('PRIVATE', errors.getvalue())

    def test_diagnostic_does_not_add_fields_or_override_evaluator_input(self):
        for original, outcome in (
            ({'version': 1, 'complete': True}, 'ok'),
            ({'complete': 'PRIVATE'}, 'invalid_output'),
            ([], 'invalid_output'),
        ):
            with self.subTest(outcome=outcome):
                value, errors = self.observe(output=json.dumps(original).encode())
                self.assertEqual(value, original)
                self.assertEqual(json.loads(errors.getvalue())['outcome'], outcome)

    def test_diagnostic_write_failure_does_not_discard_snapshot(self):
        stream = Mock()
        stream.write.side_effect = OSError('PRIVATE_STDERR_FAILURE')
        original = {'version': 1, 'complete': True}
        value, _ = self.observe(output=json.dumps(original).encode(), stream=stream)
        self.assertEqual(value, original)

    def test_timeout_retains_captured_closed_phase_without_stdout_or_private_text(self):
        records = b'{"phase":"imports","elapsed_ms":0}\n{"phase":"connect","elapsed_ms":40}\n{"phase":"jobs","elapsed_ms":75}\n'
        value, errors = self.observe(
            failure=subprocess.TimeoutExpired(
                'PRIVATE_COMMAND', 10, output=b'PRIVATE_STDOUT', stderr=records
            ),
            elapsed=10,
        )
        self.assertIsNone(value)
        self.assertEqual(
            json.loads(errors.getvalue()),
            {
                'event': 'alert_snapshot_observation',
                'outcome': 'timeout',
                'elapsed_ms': 10000,
                'category': 'unknown',
                'snapshot_elapsed_ms': None,
                'phase': 'jobs',
                'phase_started_ms': 75,
            },
        )
        self.assertNotIn('PRIVATE', errors.getvalue())

    def test_failure_record_follows_latest_phase_cumulative_time(self):
        original = {'complete': False, 'jobs': []}
        records = b'{"phase":"close","elapsed_ms":2500}\n{"category":"database","elapsed_ms":2600}\n'
        value, errors = self.observe(
            output=json.dumps(original).encode(), error=records
        )
        self.assertEqual(value, original)
        observed = json.loads(errors.getvalue())
        self.assertEqual(
            (observed['category'], observed['snapshot_elapsed_ms']), ('database', 2600)
        )
        self.assertEqual(
            (observed['phase'], observed['phase_started_ms']), ('close', 2500)
        )

    def test_complete_fourteen_phase_stream_preserves_success(self):
        phases = (
            'imports',
            'settings_import',
            'settings',
            'repository',
            'connect',
            'jobs',
            'failures',
            'guard_rows',
            'gpu_activity',
            'terminal_holds',
            'budget',
            'connection_close',
            'serialize',
            'close',
        )
        records = ''.join(
            json.dumps({'phase': phase, 'elapsed_ms': index * 10}) + '\n'
            for index, phase in enumerate(phases)
        ).encode('utf-8')
        original = {'version': 1, 'complete': True, 'jobs': []}
        value, errors = self.observe(
            output=json.dumps(original).encode(),
            error=records,
            elapsed=1,
        )
        self.assertEqual(value, original)
        observed = json.loads(errors.getvalue())
        self.assertEqual(observed['outcome'], 'ok')
        self.assertEqual(
            (observed['phase'], observed['phase_started_ms']), ('close', 130)
        )
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_complete_nineteen_phase_stream_accepts_optional_final_failure(self):
        phases = (
            'imports',
            'settings_import',
            'settings',
            'repository',
            'connect',
            'jobs',
            'failures',
            'guard_rows',
            'gpu_activity',
            'terminal_holds',
            'budget',
            'budget_guard',
            'budget_settings_import',
            'budget_settings',
            'budget_rows',
            'budget_projection',
            'connection_close',
            'serialize',
            'close',
        )
        progress = ''.join(
            json.dumps({'phase': phase, 'elapsed_ms': index * 10}) + '\n'
            for index, phase in enumerate(phases)
        ).encode('utf-8')
        for failure in (False, True):
            with self.subTest(failure=failure):
                records = progress
                if failure:
                    records += b'{"category":"database","elapsed_ms":190}\n'
                self.assertEqual(len(records.splitlines()), 20 if failure else 19)
                original = {'version': 1, 'complete': not failure, 'jobs': []}
                value, errors = self.observe(
                    output=json.dumps(original).encode(), error=records, elapsed=1
                )
                self.assertEqual(value, original)
                observed = json.loads(errors.getvalue())
                self.assertEqual(observed['outcome'], 'incomplete' if failure else 'ok')
                self.assertEqual(
                    (observed['phase'], observed['phase_started_ms']), ('close', 180)
                )
                self.assertEqual(
                    (observed['category'], observed['snapshot_elapsed_ms']),
                    ('database', 190) if failure else ('unknown', None),
                )
                self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_timeout_retains_each_closed_budget_phase(self):
        records = b'{"phase":"budget","elapsed_ms":2377}\n'
        phases = (
            'budget_guard',
            'budget_settings_import',
            'budget_settings',
            'budget_rows',
            'budget_projection',
        )
        for index, phase in enumerate(phases, 1):
            at = 2377 + index * 10
            records += (json.dumps({'phase': phase, 'elapsed_ms': at}) + '\n').encode()
            with self.subTest(phase=phase):
                value, errors = self.observe(
                    failure=subprocess.TimeoutExpired(
                        'PRIVATE_COMMAND', 10, output=b'PRIVATE_STDOUT', stderr=records
                    ),
                    elapsed=10,
                )
                self.assertIsNone(value)
                self.assertEqual(
                    json.loads(errors.getvalue()),
                    {
                        'event': 'alert_snapshot_observation',
                        'outcome': 'timeout',
                        'elapsed_ms': 10000,
                        'category': 'unknown',
                        'snapshot_elapsed_ms': None,
                        'phase': phase,
                        'phase_started_ms': at,
                    },
                )
                self.assertNotIn('PRIVATE', errors.getvalue())

    def test_untrusted_or_partial_phase_stream_is_wholly_unknown(self):
        prefix = b'{"phase":"imports","elapsed_ms":0}\n'
        for raw in (
            None,
            'PRIVATE_TEXT',
            prefix + b'PRIVATE_STDERR',
            prefix + b'\xff',
            '{"phase":"connect","elapsed_ms":1}'.encode('utf-16'),
            prefix + b'{"phase":"connect",',
            prefix + b'{"phase":"PRIVATE","elapsed_ms":1}\n',
            prefix + b'{"phase":"connect","elapsed_ms":true}\n',
            prefix + b'{"phase":"connect","elapsed_ms":-1}\n',
            prefix + b'{"phase":"connect","elapsed_ms":60001}\n',
            prefix + b'{"phase":"connect","elapsed_ms":1,"PRIVATE":"SECRET"}\n',
            prefix + b'{"phase":"connect","phase":"jobs","elapsed_ms":1}\n',
            prefix + b'{"phase":"connect","elapsed_ms":1,"elapsed_ms":2}\n',
            prefix + b'{"category":"database","category":"internal","elapsed_ms":2}\n',
            prefix + prefix,
            b'{"phase":"connect","elapsed_ms":2}\n{"phase":"jobs","elapsed_ms":1}\n',
            b'{"phase":"jobs","elapsed_ms":2}\n{"phase":"connect","elapsed_ms":3}\n',
            b'{"phase":"close","elapsed_ms":2}\n{"category":"database","elapsed_ms":1}\n',
            b'{"phase":"budget_rows","elapsed_ms":2}\n{"phase":"budget_rows","elapsed_ms":3}\n',
            b'{"phase":"budget_settings_import","elapsed_ms":2}\n{"phase":"budget_guard","elapsed_ms":3}\n',
            b'{"phase":"budget","elapsed_ms":2}\n{"phase":"budget_projection","elapsed_ms":1}\n',
            b'{"phase":"budget_projection","elapsed_ms":2}\n{"category":"database","elapsed_ms":1}\n',
            b'{"phase":"budget_rows","phase":"budget_projection","elapsed_ms":2}\n',
            b'{"phase":"budget_rows","elapsed_ms":2,"PRIVATE_SQL":"SECRET"}\n',
            b'{"phase":"budget_PRIVATE","elapsed_ms":2}\n',
            b'{"category":"database","elapsed_ms":1}\n' + prefix,
            b' ' * 2049,
            b' ' * 257,
            prefix * 21,
        ):
            with self.subTest(raw=repr(raw)[:60]):
                value, errors = self.observe(
                    failure=subprocess.TimeoutExpired('PRIVATE', 10, stderr=raw)
                )
                self.assertIsNone(value)
                observed = json.loads(errors.getvalue())
                self.assertEqual(
                    (observed['phase'], observed['phase_started_ms']),
                    ('unreported', None),
                )
                self.assertEqual(
                    (observed['category'], observed['snapshot_elapsed_ms']),
                    ('unknown', None),
                )
                self.assertNotIn('PRIVATE', errors.getvalue())


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.sns = Mock()
        self.sns.publish.return_value = {'MessageId': 'synthetic-message-id'}
        self.cloud = Mock()
        self.state = monitor.initial_state('staging')
        self.saved = []

    def tick(self, now, **host):
        snapshot = {
            'version': 1,
            'observed_at': now,
            'complete': True,
            'gpus': [],
            'jobs': [],
            'failures': [],
            'test_spend': None,
        }

        def save(value):
            self.saved.append(copy.deepcopy(value))

        self.state = monitor.tick(
            SETTINGS,
            self.state,
            snapshot,
            {
                'health_ok': True,
                'processes_ok': True,
                'cpu_percent': 10,
                'memory_percent': 20,
                'http_requests': 10,
                'http_5xx': 0,
                **host,
            },
            None,
            evaluator=evaluate,
            sns=self.sns,
            cloudwatch=self.cloud,
            save=save,
            now=now,
        )
        return self.state

    def test_first_sustained_condition_clear_and_restart_are_deduplicated(self):
        for now in range(1000, 1901, 60):
            self.tick(now, cpu_percent=95)
        self.assertEqual(self.sns.publish.call_count, 1)
        alarm = json.loads(self.sns.publish.call_args.kwargs['Message'])
        self.assertEqual(
            (alarm['code'], alarm['transition']), ('host_cpu_sustained', 'firing')
        )
        self.state = json.loads(monitor.encode(self.state))  # process restart
        self.tick(1960, cpu_percent=95)
        self.assertEqual(self.sns.publish.call_count, 1)
        self.tick(2020, cpu_percent=10)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.assertEqual(
            json.loads(self.sns.publish.call_args.kwargs['Message'])['transition'],
            'resolved',
        )
        self.tick(2080)
        self.assertEqual(self.sns.publish.call_count, 2)

    def test_unknown_ack_persists_pending_before_publish_and_retry_after_restart(self):
        self.sns.publish.side_effect = EndpointConnectionError(
            endpoint_url='SYNTHETIC_PRIVATE'
        )
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

    def test_overflow_drains_original_pending_without_advancing_transitions_then_admits(
        self,
    ):
        message = {
            'code': 'job_stuck',
            'environment': 'staging',
            'resource_id': None,
            'duration_seconds': 1800,
            'console_link': 'https://staging.inkseq.com/freestyle',
            'transition': 'firing',
        }
        pending = [
            {
                'id': hashlib.sha256(f'original-{index}'.encode()).hexdigest(),
                'notification': {**message, 'duration_seconds': 1800 + index},
                'attempts': 0,
                'retry_at': 1000,
            }
            for index in range(40)
        ]
        original = {
            **self.state,
            'sequence': 40,
            'evaluator': {'checkpoint': 'original'},
            'pending': pending,
        }
        untouched = copy.deepcopy(original)
        proposals = [
            {**message, 'duration_seconds': 2000 + index} for index in range(117)
        ]
        evaluator = Mock(
            return_value={'state': {'checkpoint': 'next'}, 'notifications': proposals}
        )
        state = original
        for now in range(1000, 1010):
            before = self.sns.publish.call_count
            state = monitor.tick(
                SETTINGS,
                state,
                None,
                {},
                None,
                evaluator=evaluator,
                sns=self.sns,
                cloudwatch=self.cloud,
                save=lambda value: self.saved.append(copy.deepcopy(value)),
                now=now,
            )
            self.assertEqual(self.sns.publish.call_count - before, 3)
            self.assertEqual(
                (state['evaluator'], state['sequence']),
                ({'checkpoint': 'original'}, 40),
            )
        self.assertEqual(self.saved[0]['pending'], pending)
        self.assertEqual(original, untouched)
        self.assertEqual(len(state['pending']), 10)
        self.assertEqual(state['pending'], pending[30:])
        state = monitor.tick(
            SETTINGS,
            state,
            None,
            {},
            None,
            evaluator=evaluator,
            sns=self.sns,
            cloudwatch=self.cloud,
            save=lambda value: None,
            now=1010,
        )
        self.assertEqual(
            (state['evaluator'], state['sequence']), ({'checkpoint': 'next'}, 157)
        )
        self.assertEqual(
            len(state['pending']), 124
        )  # seven originals plus all117 new transitions
        self.assertEqual(
            [item['id'] for item in state['pending'][:7]],
            [item['id'] for item in pending[33:]],
        )
        self.assertEqual(
            [item['notification'] for item in state['pending'][7:]], proposals
        )

    def test_oversize_proposal_batch_is_deferred_and_revisited_without_false_delivery_health(
        self,
    ):
        message = {
            'code': 'health_down',
            'environment': 'staging',
            'resource_id': None,
            'duration_seconds': 120,
            'console_link': 'https://staging.inkseq.com/freestyle',
            'transition': 'firing',
        }
        original = {
            **self.state,
            'sequence': 1,
            'evaluator': {'checkpoint': 'original'},
            'pending': [
                {
                    'id': 'a' * 64,
                    'notification': message,
                    'attempts': 0,
                    'retry_at': 1000,
                }
            ],
        }
        proposals = [
            {**message, 'duration_seconds': 120 + index}
            for index in range(monitor.MAX_PENDING + 1)
        ]
        evaluator = Mock(
            return_value={'state': {'checkpoint': 'next'}, 'notifications': proposals}
        )
        state = original
        for now in (1000, 1060):
            state = monitor.tick(
                SETTINGS,
                state,
                None,
                {},
                None,
                evaluator=evaluator,
                sns=self.sns,
                cloudwatch=self.cloud,
                save=lambda value: self.saved.append(copy.deepcopy(value)),
                now=now,
            )
            self.assertEqual(
                (state['evaluator'], state['sequence']), ({'checkpoint': 'original'}, 1)
            )
            self.assertFalse(state['pending'])
            metrics = {
                item['MetricName']: item['Value']
                for item in self.cloud.put_metric_data.call_args.kwargs['MetricData']
            }
            self.assertEqual(metrics['DeliveryHealthy'], 0)
        self.assertEqual(self.saved[0]['pending'], original['pending'])
        self.assertEqual(self.sns.publish.call_count, 1)
        self.assertEqual(evaluator.call_count, 2)
        self.assertTrue(
            all(
                call.args[1] == {'checkpoint': 'original'}
                for call in evaluator.call_args_list
            )
        )

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
            monitor.tick(
                SETTINGS,
                self.state,
                None,
                {'http_requests': 10, 'http_5xx': 5},
                None,
                evaluator=evaluate,
                sns=self.sns,
                cloudwatch=self.cloud,
                save=Mock(side_effect=OSError('disk full')),
                now=1000,
            )
        self.sns.publish.assert_not_called()
        self.cloud.put_metric_data.assert_not_called()

    def test_corrupt_or_foreign_state_is_not_silently_reset(self):
        for state in (
            {},
            {'version': 1},
            {**self.state, 'environment': 'production'},
            {**self.state, 'pending': [{'notification': 'PRIVATE'}]},
        ):
            with self.assertRaises(ValueError):
                monitor.validate_state(state, 'staging')

    def test_sns_boundary_rejects_content_and_cross_environment_links(self):
        message = {
            'code': 'health_down',
            'environment': 'staging',
            'resource_id': None,
            'duration_seconds': 120,
            'console_link': 'https://staging.inkseq.com/freestyle',
            'transition': 'firing',
        }
        for code in CODES:
            monitor.notification({**message, 'code': code}, 'staging')
        for change in (
            {'prompt': 'PRIVATE'},
            {'code': 'arbitrary'},
            {'resource_id': 'PRIVATE'},
            {'console_link': 'https://inkseq.com/freestyle'},
            {'duration_seconds': float('nan')},
            {'environment': 'production'},
        ):
            with self.assertRaises(ValueError):
                monitor.notification({**message, **change}, 'staging')

    def test_destination_is_exact_scoped_topic_and_host(self):
        target = {'name': 'staging', 'instance_id': SETTINGS['instance_id']}
        monitor.config(SETTINGS, target)
        for change in (
            {'topic_arn': SETTINGS['topic_arn'].replace('staging', 'production')},
            {'instance_id': 'i-' + 'a' * 17},
            {'extra': True},
        ):
            with self.assertRaises(ValueError):
                monitor.config({**SETTINGS, **change}, target)

    def test_configuration_thresholds_closed_and_numeric(self):
        target = {'name': 'staging', 'instance_id': SETTINGS['instance_id']}
        self.assertEqual(monitor.THRESHOLD_FIELDS, set(DEFAULTS))
        monitor.config({**SETTINGS, 'thresholds': DEFAULTS}, target)
        for value in (
            {'unknown': 5},
            {'cpu_percent': True},
            {'memory_percent': 101},
            {'http_5xx_ratio': 2},
            {'stuck_seconds': float('inf')},
            {'health_down_seconds': 0},
        ):
            with self.assertRaisesRegex(ValueError, 'alert_thresholds_invalid'):
                monitor.config({**SETTINGS, 'thresholds': value}, target)

    def test_host_heartbeat_and_snapshot_health_are_distinct(self):
        monitor.tick(
            SETTINGS,
            self.state,
            None,
            {},
            None,
            evaluator=evaluate,
            sns=self.sns,
            cloudwatch=self.cloud,
            save=lambda value: None,
            now=1000,
        )
        metrics = {
            item['MetricName']: item['Value']
            for item in self.cloud.put_metric_data.call_args.kwargs['MetricData']
        }
        self.assertEqual(
            metrics, {'MonitorHeartbeat': 1, 'DeliveryHealthy': 1, 'SnapshotHealthy': 0}
        )

    def test_stale_snapshot_never_reports_healthy_snapshot(self):
        monitor.tick(
            SETTINGS,
            self.state,
            {'version': 1, 'complete': True, 'observed_at': 100},
            {},
            None,
            evaluator=evaluate,
            sns=self.sns,
            cloudwatch=self.cloud,
            save=lambda value: None,
            now=1000,
        )
        metrics = {
            item['MetricName']: item['Value']
            for item in self.cloud.put_metric_data.call_args.kwargs['MetricData']
        }
        self.assertEqual(metrics['SnapshotHealthy'], 0)

    def test_role_only_clients_exact_endpoints_and_bounded_retry(self):
        import boto3

        session = Mock()
        session.get_credentials.return_value.method = 'iam-role'
        with patch.object(boto3, 'Session', return_value=session):
            monitor.clients()
        calls = session.client.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['sns', 'cloudwatch'])
        self.assertEqual(
            [call.kwargs['endpoint_url'] for call in calls],
            [
                'https://sns.ap-southeast-1.amazonaws.com',
                'https://monitoring.ap-southeast-1.amazonaws.com',
            ],
        )
        for call in calls:
            options = call.kwargs['config']
            self.assertEqual((options.connect_timeout, options.read_timeout), (3, 5))
            self.assertEqual(options.retries['total_max_attempts'], 1)
        session.client.reset_mock()
        session.get_credentials.return_value.method = 'env'
        with (
            patch.object(boto3, 'Session', return_value=session),
            self.assertRaisesRegex(ValueError, 'instance_role_required'),
        ):
            monitor.clients()
        session.client.assert_not_called()

    def test_proc_memory_and_cpu_delta_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / 'stat').write_text('cpu 100 0 100 800 0 0 0 0 0 0\n')
            (proc / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\n')
            with (
                patch.object(
                    monitor,
                    'containers',
                    return_value={key: None for key in monitor.SERVICES},
                ),
                patch.object(monitor, 'retention_service_failed', return_value=None),
                patch.object(monitor, 'backup_observation', return_value={}),
            ):
                _, host, cpu = monitor.observe({'total': 500, 'idle': 400}, proc=proc)
                self.assertAlmostEqual(host['cpu_percent'], 20)
                self.assertEqual(host['memory_percent'], 60)
                self.assertFalse(host['health_ok'])
                self.assertFalse(host['processes_ok'])
                _, host, _ = monitor.observe({'total': 2000, 'idle': 800}, proc=proc)
                self.assertIsNone(host['cpu_percent'])

    def test_observer_accepts_healthless_optional_services_but_checks_core_health_and_running(
        self,
    ):
        identities = {
            service: format(index + 1, '064x')
            for index, service in enumerate(monitor.SERVICES)
        }
        states = {
            service: {
                'Running': True,
                'Restarting': False,
                **(
                    {'Health': {'Status': 'healthy'}}
                    if service in ('app', 'db', 'dstack')
                    else {}
                ),
            }
            for service in monitor.SERVICES
        }
        snapshot = {
            'version': 1,
            'observed_at': 1000,
            'complete': True,
            'gpus': [],
            'jobs': [],
            'failures': [],
            'test_spend': None,
        }

        def runner(args, **kwargs):
            if args[0] == '/usr/bin/systemctl':
                return SimpleNamespace(
                    returncode=0,
                    stdout=b'LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainCode=1\nExecMainStatus=0\n',
                )
            if args[1] == 'ps':
                service = next(
                    part.removeprefix('label=com.docker.compose.service=')
                    for part in args
                    if part.startswith('label=com.docker.compose.service=')
                )
                return SimpleNamespace(
                    returncode=0, stdout=(identities[service] + '\n').encode()
                )
            if args[1] == 'inspect':
                service = next(
                    name
                    for name, identity in identities.items()
                    if identity == args[-1]
                )
                value = states[service]
                # Docker's Go-template map lookup fails for an absent Health
                # field; caddy/controller need no healthcheck to be running.
                if 'Health' not in value and '.State.Health' in args[3]:
                    return SimpleNamespace(returncode=1, stdout=b'')
                row = {
                    'project': 'sixnine-platform',
                    'service': service,
                    'running': value['Running'],
                    'restarting': value['Restarting'],
                    'health': value.get('Health', {}).get('Status'),
                }
                return SimpleNamespace(returncode=0, stdout=json.dumps(row).encode())
            self.assertEqual(args[1:3], ['exec', identities['app']])
            value = (
                snapshot
                if '-m' in args
                else {'health_ok': True, 'http_requests': 10, 'http_5xx': 0}
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / 'stat').write_text('cpu 100 0 100 800 0 0 0 0 0 0\n')
            (proc / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\n')
            observed, host, _ = monitor.observe(None, runner=runner, proc=proc)
            self.assertEqual(observed, {**snapshot, 'payments': {'complete': False}})
            self.assertTrue(host['processes_ok'])
            for service in ('app', 'db', 'dstack'):
                with self.subTest(missing_required_health=service):
                    health = states[service].pop('Health')
                    self.assertFalse(
                        monitor.observe(None, runner=runner, proc=proc)[1][
                            'processes_ok'
                        ]
                    )
                    states[service]['Health'] = health
            states['controller']['Running'] = False
            self.assertFalse(
                monitor.observe(None, runner=runner, proc=proc)[1]['processes_ok']
            )

    def test_rds_observer_keeps_database_outage_actionable_without_local_db(self):
        target = {
            'database': {
                'mode': 'rds',
                'host': 'inkseq-stage.abc123.ap-southeast-1.rds.amazonaws.com',
            }
        }
        identities = {
            name: format(index + 1, '064x')
            for index, name in enumerate(monitor.SERVICES)
        }
        queried = []
        unavailable = False

        def runner(args, **kwargs):
            if args[1] == 'ps':
                name = args[-1].split('=')[-1]
                queried.append(name)
                return SimpleNamespace(returncode=0, stdout=identities[name].encode())
            if args[1] == 'inspect':
                name = next(
                    name
                    for name, identity in identities.items()
                    if identity == args[-1]
                )
                value = {
                    'project': 'sixnine-platform',
                    'service': name,
                    'running': True,
                    'restarting': False,
                    'health': 'healthy',
                }
            elif args[-1] == monitor.HTTP_PROBE:
                value = {
                    'health_ok': not unavailable,
                    'http_requests': 1,
                    'http_5xx': 0,
                }
            elif args[-1] == 'studio_platform.alert_snapshot':
                if unavailable:
                    return SimpleNamespace(returncode=1, stdout=b'')
                value = {
                    'version': 1,
                    'observed_at': 1000,
                    'complete': True,
                    'gpus': [],
                    'jobs': [],
                    'failures': [],
                    'test_spend': None,
                }
            else:
                value = {}
            return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

        with (
            patch.object(
                monitor,
                'proc_sample',
                return_value={'total': 100, 'idle': 90, 'memory_percent': 10},
            ),
            patch.object(monitor, 'disk_sample', return_value=10),
            patch.object(monitor, 'retention_service_failed', return_value=False),
            patch.object(monitor, 'backup_observation', return_value={}),
        ):
            _, host, _ = monitor.observe(None, runner=runner, target=target)
            self.assertTrue(host['processes_ok'])
            self.assertTrue(host['health_ok'])
            self.assertEqual(set(queried), set(monitor.SERVICES) - {'db'})
            unavailable = True
            snapshot, host, _ = monitor.observe(None, runner=runner, target=target)
            self.assertIsNone(snapshot)
            self.assertTrue(host['processes_ok'])
            self.assertFalse(host['health_ok'])
            state = {}
            for now in (1000, 1060, 1120):
                result = evaluate(
                    snapshot, state, environment='staging', now=now, host=host
                )
                state = result['state']
            self.assertIn(
                'health_down', {notice['code'] for notice in result['notifications']}
            )

    def test_main_passes_only_protected_host_database_selection_to_observer(self):
        target = {
            'name': 'staging',
            'instance_id': SETTINGS['instance_id'],
            'database': {
                'mode': 'rds',
                'host': 'inkseq-stage.abc123.ap-southeast-1.rds.amazonaws.com',
            },
        }
        with (
            patch.object(monitor.deployment_target, 'host', return_value=target),
            patch.object(monitor.os, 'geteuid', return_value=0),
            patch.object(monitor.sys, 'argv', ['alert_monitor.py']),
            patch.dict(
                monitor.sys.modules,
                {'alert_conditions': SimpleNamespace(evaluate=evaluate)},
            ),
            patch.object(monitor, 'read_protected', side_effect=[SETTINGS, None]),
            patch.object(monitor, 'locked', return_value=nullcontext()),
            patch.object(monitor, 'observe', return_value=(None, {}, None)) as observe,
            patch.object(monitor, 'clients', return_value=(self.sns, self.cloud)),
            patch.object(monitor, 'tick'),
        ):
            self.assertEqual(monitor.main(), 0)
        self.assertEqual(observe.call_args.kwargs['target'], target)

    def test_existing_app_health_probe_queries_database_and_reports_failure(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        from urllib.error import HTTPError

        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Settings(Path(temporary), auth_mode='local-test'))
            with TestClient(app, raise_server_exceptions=False) as client:

                def open_local(url, **kwargs):
                    response = client.get(url.removeprefix('http://127.0.0.1:8845'))
                    if response.status_code >= 400:
                        raise HTTPError(
                            url, response.status_code, 'synthetic failure', {}, None
                        )
                    stream = io.BytesIO(response.content)
                    stream.status = response.status_code
                    return stream

                with patch('urllib.request.urlopen', side_effect=open_local):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        exec(monitor.HTTP_PROBE, {})
                    self.assertTrue(json.loads(output.getvalue())['health_ok'])
                    with patch.object(
                        app.state.repository.engine,
                        'connect',
                        side_effect=RuntimeError('synthetic database outage'),
                    ) as connect:
                        output = io.StringIO()
                        with redirect_stdout(output):
                            exec(monitor.HTTP_PROBE, {})
                        self.assertFalse(json.loads(output.getvalue())['health_ok'])
                        self.assertGreater(connect.call_count, 0)

    def test_fixed_retention_unit_observation_does_not_read_logs_or_treat_unknown_as_recovery(
        self,
    ):
        success = {
            'LoadState': 'loaded',
            'ActiveState': 'inactive',
            'SubState': 'dead',
            'Result': 'success',
            'ExecMainCode': '1',
            'ExecMainStatus': '0',
        }

        def observe(values, returncode=0):
            runner = Mock(
                return_value=SimpleNamespace(
                    returncode=returncode,
                    stdout=''.join(
                        f'{key}={value}\n' for key, value in values.items()
                    ).encode(),
                )
            )
            result = monitor.retention_service_failed(runner=runner)
            args = runner.call_args.args[0]
            self.assertEqual(
                args[:3],
                ['/usr/bin/systemctl', 'show', 'inkseq-release-retention.service'],
            )
            self.assertNotIn('journal', ' '.join(args))
            self.assertLessEqual(runner.call_args.kwargs['timeout'], 5)
            return result

        self.assertFalse(observe(success))
        self.assertIsNone(observe({**success, 'ExecMainCode': '0'}))
        self.assertTrue(
            observe(
                {
                    **success,
                    'ActiveState': 'failed',
                    'SubState': 'failed',
                    'Result': 'exit-code',
                    'ExecMainStatus': '1',
                }
            )
        )
        self.assertIsNone(
            observe({**success, 'ActiveState': 'activating', 'SubState': 'start'})
        )
        self.assertIsNone(observe({**success, 'LoadState': 'not-found'}))
        self.assertIsNone(observe(success, returncode=1))
        self.assertIsNone(observe({**success, 'PrivateUnexpected': 'PRIVATE_VALUE'}))
        self.assertIsNone(observe({**success, 'Result': 'PRIVATE_VALUE'}))
        self.assertIsNone(observe({**success, 'ExecMainStatus': 'PRIVATE_VALUE'}))

    def test_retention_failed_notifies_once_and_only_successful_observation_resolves(
        self,
    ):
        self.tick(1000, retention_service_failed=True)
        self.assertEqual(self.sns.publish.call_count, 1)
        first = json.loads(self.sns.publish.call_args.kwargs['Message'])
        self.assertEqual(
            (first['code'], first['transition']), ('retention_service_failed', 'firing')
        )
        self.tick(1060, retention_service_failed=True)
        self.tick(1120, retention_service_failed=None)
        self.assertEqual(self.sns.publish.call_count, 1)
        self.state = json.loads(monitor.encode(self.state))
        self.tick(1180, retention_service_failed=False)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.assertEqual(
            json.loads(self.sns.publish.call_args.kwargs['Message'])['transition'],
            'resolved',
        )
        self.tick(1240, retention_service_failed=False)
        self.assertEqual(self.sns.publish.call_count, 2)

    def test_backup_failure_and_missing_copy_use_existing_sns_outbox(self):
        self.tick(1000, business_backup_failed=True, business_backup_missing=True)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.assertEqual(
            {
                json.loads(call.kwargs['Message'])['code']
                for call in self.sns.publish.call_args_list
            },
            {'business_backup_failed', 'business_backup_missing'},
        )
        self.state = json.loads(monitor.encode(self.state))
        self.tick(1060, business_backup_failed=True, business_backup_missing=True)
        self.tick(1120, business_backup_failed=None, business_backup_missing=None)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.tick(1180, business_backup_failed=False, business_backup_missing=False)
        self.assertEqual(self.sns.publish.call_count, 4)
        self.assertTrue(
            all(
                json.loads(call.kwargs['Message'])['transition'] == 'resolved'
                for call in self.sns.publish.call_args_list[2:]
            )
        )

    def test_disk_80_90_use_actual_filesystem_sample_metric_and_sns_dedup(self):
        statvfs = Mock(
            return_value=SimpleNamespace(
                f_blocks=1000, f_bfree=240, f_bavail=190, f_frsize=4096
            )
        )
        value = monitor.disk_sample(statvfs=statvfs)
        statvfs.assert_called_once_with('/srv/sixnine')
        self.assertEqual(
            value, 80
        )  # 760 used / (760 used +190 available), not total-block estimate.
        self.tick(1000, disk_used_percent=value)
        first = json.loads(self.sns.publish.call_args.kwargs['Message'])
        self.assertEqual(
            (first['code'], first['transition']), ('host_disk_80', 'firing')
        )
        self.tick(1060, disk_used_percent=90)
        self.assertEqual(
            json.loads(self.sns.publish.call_args.kwargs['Message'])['code'],
            'host_disk_90',
        )
        self.assertEqual(self.sns.publish.call_count, 2)
        self.tick(1120, disk_used_percent=None)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.tick(1180, disk_used_percent=90)
        self.assertEqual(self.sns.publish.call_count, 2)
        self.tick(1240, disk_used_percent=79)
        self.assertEqual(self.sns.publish.call_count, 4)
        metrics = {
            item['MetricName']: item
            for item in self.cloud.put_metric_data.call_args.kwargs['MetricData']
        }
        self.assertEqual(
            (metrics['DiskUsedPercent']['Value'], metrics['DiskUsedPercent']['Unit']),
            (79, 'Percent'),
        )
        self.assertTrue(
            all(
                'PRIVATE' not in call.kwargs['Message']
                for call in self.sns.publish.call_args_list
            )
        )

    def test_disk_sample_invalid_or_missing_never_invents_a_healthy_disk(self):
        for values in (
            (0, 0, 0, 4096),
            (100, 10, 20, 4096),
            (100, 110, 20, 4096),
            (100, 10, 1, 0),
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(ValueError, 'disk_sample_invalid'),
            ):
                monitor.disk_sample(
                    statvfs=lambda path: SimpleNamespace(
                        **dict(
                            zip(('f_blocks', 'f_bfree', 'f_bavail', 'f_frsize'), values)
                        )
                    )
                )


if __name__ == '__main__':
    unittest.main()
