"""Offline continuation safety: fake Docker/systemd, no credentials or providers."""
from contextlib import ExitStack, nullcontext, redirect_stdout
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_platform_release import module, release
from test_operator_host_release import host
from test_operator_handoff import handoff, ledger_rows

with patch.dict(sys.modules, {
    'release': release, 'operator_capacity': host, 'operator_handoff': handoff,
}):
    continuation = module('operator_handoff_continuation_test', 'operator_handoff_continuation.py')


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events = []
        self.mock(host, 'ROOT', new=self.root)
        self.mock(handoff, 'locked', side_effect=lambda: nullcontext())
        self.mock(release, '_protected_json', side_effect=lambda path, **kw: self.read(path))
        self.mock(host, 'atomic', side_effect=self.write)
        self.mock(continuation.time, 'time', return_value=600)
        self.old = {
            'schema_version': 1, 'commit': 'a'*40, 'image_id': 'old-image',
            'runtime_config_sha256': 'd'*64, 'files': {'runtime': 'e'*64},
        }
        self.prepared = {**self.old, 'commit': 'b'*40, 'image_id': 'new-image'}
        self.old_pin = {
            **host.pin_for(self.old), 'state': 'running', 'admission': 'open',
            'controller_id': 'original-controller',
        }
        self.pin = host.pin_for(self.prepared)
        self.runtime = {'existing_configuration': 'retained'}
        self.environment = {'SIXNINE_IMAGE': 'sixnine-platform:'+'b'*40}
        self.original_unit = {
            'MainPID': '0', 'ExecMainPID': '321', 'ActiveState': 'failed',
            'ExecStart': {'path': '/usr/bin/python3', 'argv': 'original command'},
            'unit_files': {'original.service': 'original-unit-hash'},
        }
        self.failed_unit = {
            'MainPID': '0', 'ExecMainPID': '421', 'ActiveState': 'failed',
            'ExecStart': {'path': '/usr/bin/python3', 'argv': 'handoff command'},
            'unit_files': {'failed.service': 'failed-unit-hash'},
        }
        self.rows = ledger_rows()
        self.ledger = handoff.ledger_summary(self.rows, {'binding': 'frozen'})
        self.record = {
            'schema_version': 1, 'phase': 'launch_intent', 'launch_intent_at': 300,
            'target_commit': self.prepared['commit'], 'target_image_id': 'new-image',
            'old_prepared': self.old, 'old_pin': self.old_pin,
            'old_overlay': {'original_owners': 'retained'},
            'supervisor_unit': 'sixnine-original.service',
            'supervisor': {**self.original_unit, 'MainPID': '321', 'ActiveState': 'active'},
            'ledger': self.ledger, 'successor_pin': self.pin,
        }
        self.write(handoff.record_path(), self.record)
        self.write(self.root/'prepared.json', self.prepared)
        self.write(self.root/'active.json', self.pin)
        self.write(self.root/'overlay.json', self.record['old_overlay'])
        self.original_bytes = {
            path.name: path.read_bytes() for path in (
                handoff.record_path(), self.root/'prepared.json', self.root/'active.json',
                self.root/'overlay.json',
            )
        }
        self.clean_state = {
            'Running': False, 'Restarting': False, 'Paused': False,
            'OOMKilled': False, 'Status': 'exited', 'ExitCode': 0,
        }
        self.containers = {
            self.old_pin['container_name']: self.container(self.old_pin, 'a'*64),
            self.pin['container_name']: self.container(self.pin, 'c'*64),
        }
        self.stopped_receipt = {
            'state': 'shutdown_complete', 'local_connections_released': True,
            'controller_id': 'failed-controller', 'observed_at': 500,
        }
        self.prepare = self.mock(host, 'prepared', return_value=(
            self.runtime, self.prepared, self.root, self.environment,
        ))
        self.mock(host, 'checked_pin', side_effect=lambda prepared: self.read(self.root/'active.json'))
        self.target = self.mock(handoff, 'current_target', return_value=(
            self.prepared['commit'], self.root, self.environment, self.prepared['image_id'],
        ))
        self.original_supervisor = self.mock(handoff, 'supervisor', return_value=self.original_unit)
        self.failed_supervisor = self.mock(continuation, 'failed_supervisor', return_value=self.failed_unit)
        self.inspect = self.mock(host, 'inspect_controller', side_effect=self.inspect_container)
        self.receipt = self.mock(host, 'receipt', side_effect=self.controller_receipt)
        self.competing = self.mock(host, 'no_competing_controller', side_effect=lambda env: self.events.append('ownership'))
        self.probe = self.mock(handoff, 'ledger_probe', side_effect=self.ledger_probe)
        self.command = self.mock(release, 'command', side_effect=self.docker)
        self.launch = self.mock(host, 'launch', side_effect=self.launch_controller)
        self.drain = self.mock(host, 'request_drain')
        self.mock(host, 'app_overlay', return_value={'services': {'app': {}}})
        self.mock(host, 'validate_app_rendered')
        self.mock(release, 'wait_ready', side_effect=lambda *args: self.events.append('app-ready'))
        self.restore = self.mock(host, 'restore', return_value={'state': 'cpu_restored'})
        self.events.clear()

    def mock(self, owner, name, **kwargs):
        return self.stack.enter_context(patch.object(owner, name, **kwargs))

    def read(self, path):
        return json.loads(Path(path).read_text())

    def write(self, path, value):
        path = Path(path)
        self.events.append(('write', path.name, value.get('phase', value.get('admission'))))
        path.write_text(json.dumps(value))

    def container(self, pin, container_id):
        return {
            'Id': container_id, 'Name': '/'+pin['container_name'], 'Image': pin['image_id'],
            'State': copy.deepcopy(self.clean_state),
            'Config': {'Labels': {
                'com.docker.compose.project': 'sixnine-platform',
                'com.docker.compose.service': host.SERVICE, host.LABEL: pin['prepared_hash'],
            }},
        }

    def inspect_container(self, environment, pin):
        self.events.append(('inspect', pin['container_name']))
        return copy.deepcopy(self.containers[pin['container_name']]['State'])

    def controller_receipt(self, pin, *, fresh=False):
        if fresh:
            return {'state': 'running', 'controller_id': 'resumed-controller'}
        return copy.deepcopy(self.stopped_receipt)

    def ledger_probe(self, directory, environment, **kwargs):
        self.events.append('ledger')
        self.assertEqual(kwargs, {'require_removal_cadence': True})
        return copy.deepcopy(self.ledger)

    def docker(self, arguments, **kwargs):
        if arguments[0] == 'inspect':
            return json.dumps([self.containers[arguments[1]]]).encode()
        if arguments[0] == 'rename':
            self.assertEqual(self.read(continuation.record_path())['phase'], 'archive_intent')
            self.assertEqual(kwargs, {'environment': self.environment, 'timeout': 20})
            self.events.append('rename')
            container_id, archived = arguments[1:]
            self.assertEqual(container_id, 'c'*64)
            old = next(name for name, row in self.containers.items() if row['Id'] == container_id)
            self.assertNotIn(archived, self.containers)
            row = self.containers.pop(old)
            row['Name'] = '/'+archived
            self.containers[archived] = row
            return b''
        if arguments[-2:] == ['version', '--short']:
            return b'2.38.2'
        if arguments[-3:] == ['config', '--format', 'json']:
            return b'{"services":{"app":{}}}'
        if arguments[-4:] == ['up', '-d', '--no-deps', 'app']:
            self.events.append('app-up')
            return b''
        self.fail('Unexpected fake Docker operation: '+repr(arguments))

    def launch_controller(self, directory, environment, runtime, pin):
        self.assertEqual(self.read(continuation.record_path())['phase'], 'launch_intent')
        self.assertEqual(self.read(self.root/'active.json')['admission'], 'closed')
        self.events.append('launch')
        self.containers[pin['container_name']] = self.container(pin, 'd'*64)
        self.containers[pin['container_name']]['State'].update(Running=True, Status='running')
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]
        return process

    def approve(self):
        return continuation.approve('sixnine-failed.service')

    def resume(self):
        return handoff.start(successor_factory=continuation.successor, clock=lambda: 0, sleep=Mock())

    def assert_originals_unchanged(self):
        for name, content in self.original_bytes.items():
            self.assertEqual((self.root/name).read_bytes(), content, name)
        self.assertEqual(self.rows, ledger_rows())

    def test_approval_records_evidence_without_rename_launch_or_original_writes(self):
        self.assertEqual(self.approve(), {
            'state': 'continuation_approved_not_started', 'pending_deletions': 1,
        })
        approval = self.read(continuation.record_path())
        self.assertEqual(approval['phase'], 'approved')
        self.assertEqual(approval['evidence']['handoff_hash'], release.canonical_hash(self.record))
        self.assertEqual(approval['evidence']['container']['id'], 'c'*64)
        self.assert_originals_unchanged()
        self.launch.assert_not_called()
        self.assertNotIn('rename', self.events)
        self.probe.assert_called_once_with(self.root, self.environment, require_removal_cadence=True)
        with self.assertRaisesRegex(release.ReleaseError, 'already_recorded'):
            self.approve()

    def test_only_original_launch_intent_can_be_approved(self):
        for phase in ('drain_requested', 'running', 'approved', 'archive_intent'):
            with self.subTest(phase=phase):
                self.write(handoff.record_path(), {**self.record, 'phase': phase})
                with self.assertRaisesRegex(release.ReleaseError, 'launch_intent_required'):
                    self.approve()
                self.assertFalse(continuation.record_path().exists())
        self.command.assert_not_called()
        self.launch.assert_not_called()

    def test_source_configuration_and_pin_changes_block_approval(self):
        for prepared in ({**self.prepared, 'files': {}}, {**self.prepared, 'runtime_config_sha256': 'f'*64}):
            with self.subTest(prepared=prepared):
                self.prepare.return_value = (self.runtime, prepared, self.root, self.environment)
                with self.assertRaisesRegex(release.ReleaseError, 'configuration_changed'):
                    self.approve()
        self.prepare.return_value = (self.runtime, self.prepared, self.root, self.environment)
        for change in ({'state': 'running'}, {'admission': 'open'}, {'controller_id': 'changed'}):
            with self.subTest(change=change):
                self.write(self.root/'active.json', {**self.pin, **change})
                with self.assertRaisesRegex(release.ReleaseError, 'pin_changed'):
                    self.approve()
        self.assertFalse(continuation.record_path().exists())
        self.launch.assert_not_called()

    def test_target_source_rejection_or_changed_image_blocks_approval(self):
        self.target.side_effect = release.ReleaseError('release_source_changed')
        with self.assertRaisesRegex(release.ReleaseError, 'source_changed'):
            self.approve()
        self.target.side_effect = None
        self.target.return_value = ('b'*40, self.root, self.environment, 'substituted-image')
        with self.assertRaisesRegex(release.ReleaseError, 'target_changed'):
            self.approve()
        self.assertFalse(continuation.record_path().exists())

    def test_original_supervisor_retirement_identity_must_still_match(self):
        for change in (
            {'MainPID': '321'}, {'ActiveState': 'active'}, {'ExecMainPID': '999'},
            {'ExecStart': 'changed'}, {'unit_files': {'original.service': 'changed'}},
        ):
            with self.subTest(change=change):
                self.original_supervisor.return_value = {**self.original_unit, **change}
                with self.assertRaisesRegex(release.ReleaseError, 'original_supervisor_not_retired'):
                    self.approve()
        self.assertFalse(continuation.record_path().exists())
        self.launch.assert_not_called()

    def test_both_original_and_failed_controller_need_clean_confirmed_exit(self):
        for pin in (self.old_pin, self.pin):
            row = self.containers[pin['container_name']]
            for change in (
                {'Running': True}, {'Restarting': True}, {'Paused': True}, {'OOMKilled': True},
                {'ExitCode': 137}, {'Status': 'dead'},
            ):
                with self.subTest(container=pin['container_name'], change=change):
                    row['State'] = {**self.clean_state, **change}
                    with self.assertRaisesRegex(release.ReleaseError, 'exit_unconfirmed'):
                        self.approve()
            row['State'] = copy.deepcopy(self.clean_state)
        self.assertFalse(continuation.record_path().exists())
        self.launch.assert_not_called()

    def test_stopped_container_identity_and_second_inspection_must_match(self):
        row = self.containers[self.pin['container_name']]
        original = copy.deepcopy(row)
        for change in (
            {'Id': 'short'}, {'Name': '/other'}, {'Image': 'other'},
            {'Config': {'Labels': {}}}, {'State': {**self.clean_state, 'Pid': 9}},
        ):
            with self.subTest(change=change):
                row.clear(); row.update({**original, **change})
                # The first inspection remains clean, so the second Docker identity check is exercised.
                self.inspect.return_value = self.clean_state
                self.inspect.side_effect = None
                with self.assertRaises(release.ReleaseError):
                    continuation.stopped_container(self.environment, self.pin)
        self.launch.assert_not_called()

    def test_stopped_receipt_requires_released_ownership_of_current_successor(self):
        original = copy.deepcopy(self.stopped_receipt)
        for change in (
            {'state': 'shutdown_waiting'}, {'local_connections_released': False},
            {'controller_id': self.old_pin['controller_id']}, {'controller_id': None},
            {'controller_id': ''}, {'observed_at': 299},
        ):
            with self.subTest(change=change):
                self.stopped_receipt = {**original, **change}
                with self.assertRaisesRegex(release.ReleaseError, 'local_ownership_unconfirmed'):
                    self.approve()
        self.assertFalse(continuation.record_path().exists())
        self.launch.assert_not_called()

    def test_competing_controller_and_unconfirmed_receipt_keep_approval_absent(self):
        self.competing.side_effect = release.ReleaseError('operator_competing_controller')
        with self.assertRaisesRegex(release.ReleaseError, 'competing_controller'):
            self.approve()
        self.competing.side_effect = None
        self.receipt.side_effect = release.ReleaseError('operator_status_identity_invalid')
        with self.assertRaisesRegex(release.ReleaseError, 'status_identity_invalid'):
            self.approve()
        self.assertFalse(continuation.record_path().exists())

    def test_ledger_identity_or_new_pending_obligation_blocks_approval(self):
        original = copy.deepcopy(self.ledger)
        for change in ({'immutable_hash': 'f'*64}, {'pending_ids': ['intent', 'new-obligation']}):
            with self.subTest(change=change):
                self.ledger = {**original, **change}
                with self.assertRaisesRegex(release.ReleaseError, 'ledger_changed'):
                    self.approve()
        self.assertFalse(continuation.record_path().exists())
        self.launch.assert_not_called()

    def test_legitimate_terminal_ledger_progress_before_approval_is_retained(self):
        self.ledger = {**self.ledger, 'pending_ids': [], 'accounting_hash': 'f'*64}
        self.assertEqual(self.approve()['pending_deletions'], 0)
        self.assertEqual(self.read(continuation.record_path())['evidence']['ledger'], self.ledger)
        self.assert_originals_unchanged()

    def test_changes_after_approval_require_new_evidence_before_any_rename(self):
        self.approve()
        mutations = (
            ('failed_unit', {**self.failed_unit, 'unit_files': {'failed.service': 'changed'}}),
            ('ledger', {**self.ledger, 'accounting_hash': 'f'*64}),
            ('receipt', {**self.stopped_receipt, 'observed_at': 501}),
            ('handoff', {**self.record, 'new_metadata': 'changed'}),
        )
        for kind, value in mutations:
            with self.subTest(kind=kind):
                self.failed_supervisor.return_value = self.failed_unit
                self.ledger = copy.deepcopy(self.record['ledger'])
                self.stopped_receipt = {
                    'state': 'shutdown_complete', 'local_connections_released': True,
                    'controller_id': 'failed-controller', 'observed_at': 500,
                }
                self.write(handoff.record_path(), self.record)
                if kind == 'failed_unit': self.failed_supervisor.return_value = value
                if kind == 'ledger': self.ledger = value
                if kind == 'receipt': self.stopped_receipt = value
                if kind == 'handoff': self.write(handoff.record_path(), value)
                with self.assertRaisesRegex(release.ReleaseError, 'evidence_changed'):
                    self.resume()
                self.assertEqual(self.read(continuation.record_path())['phase'], 'approved')
        self.assertNotIn('rename', self.events)
        self.launch.assert_not_called()

    def test_unknown_rename_consumes_intent_without_retry_or_launch(self):
        self.approve()
        operations = self.docker
        def uncertain(arguments, **kwargs):
            if arguments[0] == 'rename':
                operations(arguments, **kwargs)  # Rename happened; acknowledgement was lost.
                raise TimeoutError('synthetic lost rename response')
            return operations(arguments, **kwargs)
        self.command.side_effect = uncertain
        with self.assertRaises(TimeoutError): self.resume()
        self.assertEqual(self.read(continuation.record_path())['phase'], 'archive_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'not_approved_or_consumed'):
            self.resume()
        self.assertEqual(self.events.count('rename'), 1)
        self.launch.assert_not_called()
        self.drain.assert_not_called()
        self.assert_originals_unchanged()

    def test_changed_archived_identity_prevents_launch_and_cannot_replay(self):
        self.approve()
        operations = self.docker
        def changed_archive(arguments, **kwargs):
            result = operations(arguments, **kwargs)
            if arguments[0] == 'rename':
                self.containers[arguments[2]]['Id'] = 'e'*64
            return result
        self.command.side_effect = changed_archive
        with self.assertRaisesRegex(release.ReleaseError, 'archive_changed'):
            self.resume()
        self.assertEqual(self.read(continuation.record_path())['phase'], 'archive_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'not_approved_or_consumed'):
            self.resume()
        self.assertEqual(self.events.count('rename'), 1)
        self.launch.assert_not_called()
        self.assert_originals_unchanged()

    def test_ledger_change_between_archive_and_launch_retains_consumed_intent(self):
        self.approve()
        self.probe.side_effect = [self.ledger, {**self.ledger, 'accounting_hash': 'f'*64}]
        with self.assertRaisesRegex(release.ReleaseError, 'ledger_changed_before_launch'):
            self.resume()
        self.assertEqual(self.read(continuation.record_path())['phase'], 'archive_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'not_approved_or_consumed'):
            self.resume()
        self.assertEqual(self.events.count('rename'), 1)
        self.launch.assert_not_called()
        self.assert_originals_unchanged()

    def test_unknown_credential_launch_is_not_replayed(self):
        self.approve()
        self.launch.side_effect = TimeoutError('synthetic unknown credential delivery')
        with self.assertRaises(TimeoutError): self.resume()
        self.assertEqual(self.read(continuation.record_path())['phase'], 'launch_intent')
        with self.assertRaisesRegex(release.ReleaseError, 'not_approved_or_consumed'):
            self.resume()
        self.launch.assert_called_once()
        self.drain.assert_called_once()
        self.assertEqual(self.events.count('rename'), 1)
        self.assert_originals_unchanged()

    def test_healthy_resume_archives_exact_container_then_launches_once_before_admission(self):
        self.approve()
        self.events.clear()
        with patch.object(handoff, 'successor') as original_factory:
            self.assertEqual(self.resume(), {'state': 'cpu_restored'})
        original_factory.assert_not_called()
        self.launch.assert_called_once_with(self.root, self.environment, self.runtime, self.pin)
        self.restore.assert_called_once_with(self.root, self.environment, self.prepared)
        self.drain.assert_not_called()
        approval = self.read(continuation.record_path())
        archived = self.pin['container_name']+'-failed-'+('c'*12)
        self.assertEqual(approval['archived_container_name'], archived)
        self.assertEqual(approval['phase'], 'launch_intent')
        self.assertEqual(self.containers[archived]['Id'], 'c'*64)
        self.assertEqual(self.containers[archived]['State'], self.clean_state)
        ordered = [
            ('write', 'handoff-continuation.json', 'archive_intent'), 'rename',
            ('write', 'handoff-continuation.json', 'launch_intent'), 'launch',
            'app-up', 'app-ready', ('write', 'active.json', 'open'),
        ]
        positions = [self.events.index(event) for event in ordered]
        self.assertEqual(positions, sorted(positions))
        original = self.read(handoff.record_path())
        self.assertEqual(original, {**self.record, 'phase': 'running', 'controller_id': 'resumed-controller'})
        self.assertEqual((self.root/'prepared.json').read_bytes(), self.original_bytes['prepared.json'])
        self.assertEqual((self.root/'overlay.json').read_bytes(), self.original_bytes['overlay.json'])
        self.assertEqual(self.rows, ledger_rows())
        self.assertEqual(self.read(self.root/'active.json')['admission'], 'open')
        with self.assertRaisesRegex(release.ReleaseError, 'not_approved_or_consumed'):
            self.resume()
        self.launch.assert_called_once()

    def test_cli_resume_uses_existing_handoff_supervision_and_rejects_unit_override(self):
        with patch.object(release, 'check_host') as check_host, \
                patch.object(handoff, 'start', return_value={'state': 'cpu_restored'}) as start:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(continuation.main(['resume']), 0)
            self.assertEqual(json.loads(output.getvalue()), {'state': 'cpu_restored'})
            start.assert_called_once_with(successor_factory=continuation.successor)
            check_host.assert_called_once_with(release.ROOT)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(continuation.main(['resume', '--unit', 'sixnine-other.service']), 1)
            start.assert_called_once()


class FailedSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.unit = 'sixnine-failed.service'
        self.command = '/usr/bin/python3 /opt/sixnine-release/operator_handoff.py start'
        self.values = {
            'MainPID': '0', 'ExecMainPID': '421', 'Restart': 'no', 'KillMode': 'process',
            'ActiveState': 'failed', 'User': 'root', 'TimeoutStopUSec': 'infinity',
            'SendSIGKILL': 'no', 'FragmentPath': '/etc/systemd/system/'+self.unit,
            'DropInPaths': '/etc/systemd/system/'+self.unit+'.d/override.conf',
            'ExecStart': self.exec_start(),
        }
        self.run = self.stack.enter_context(patch.object(continuation.subprocess, 'run'))
        self.protected = self.stack.enter_context(patch.object(host, 'protected_file', side_effect=lambda path: path))
        self.checksum = self.stack.enter_context(patch.object(release, 'checksum', side_effect=lambda path: 'hash:'+str(path)))
        self.output()

    def exec_start(self, metadata='pid=421 ; code=exited ; status=1 ;'):
        return '{ path=/usr/bin/python3 ; argv[]='+self.command+' ; ignore_errors=no ; '+metadata+' }'

    def output(self, **changes):
        value = {**self.values, **changes}
        self.run.return_value = Mock(stdout='\n'.join(key+'='+item for key, item in value.items()).encode())

    def test_exact_failed_supervisor_is_normalized_and_all_unit_files_checked(self):
        before = continuation.failed_supervisor(self.unit)
        self.output(ExecStart=self.exec_start('pid=421 ; code=(null) ; status=0 ;'))
        after = continuation.failed_supervisor(self.unit)
        self.assertEqual(before, after)
        self.assertEqual(before['ExecStart'], {'path': '/usr/bin/python3', 'argv': self.command})
        self.assertEqual(set(before['unit_files']), {
            self.values['FragmentPath'], self.values['DropInPaths'],
        })
        arguments, kwargs = self.run.call_args
        self.assertEqual(arguments[0][:3], ['/usr/bin/systemctl', 'show', self.unit])
        self.assertIn('TimeoutStopUSec', arguments[0][3])
        self.assertIn('SendSIGKILL', arguments[0][3])
        self.assertEqual(kwargs, {
            'env': {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}, 'check': True,
            'capture_output': True, 'timeout': 20,
        })

    def test_alive_restarting_force_kill_or_finite_stop_timeout_is_rejected(self):
        for key, value in (
            ('MainPID', '421'), ('ExecMainPID', '0'), ('ExecMainPID', 'invalid'),
            ('ActiveState', 'active'), ('Restart', 'on-failure'), ('KillMode', 'control-group'),
            ('TimeoutStopUSec', '1min 30s'), ('TimeoutStopUSec', ''),
            ('SendSIGKILL', 'yes'), ('User', 'service-user'),
        ):
            with self.subTest(key=key, value=value):
                self.output(**{key: value})
                with self.assertRaisesRegex(release.ReleaseError, 'supervisor_not_retired'):
                    continuation.failed_supervisor(self.unit)
        self.protected.assert_not_called()

    def test_only_exact_handoff_start_command_is_allowed(self):
        original = self.command
        for command in (
            original+' --unit other', original.replace('operator_handoff.py', 'operator_capacity.py'),
            original.replace('/usr/bin/python3', '/usr/local/bin/python3'),
            '/bin/sh -c '+original, original.replace(' start', ' prepare'),
        ):
            with self.subTest(command=command):
                self.command = command
                self.output(ExecStart=self.exec_start())
                with self.assertRaisesRegex(release.ReleaseError, 'supervisor_command_changed'):
                    continuation.failed_supervisor(self.unit)
        self.protected.assert_not_called()

    def test_untrusted_fragment_or_dropin_paths_are_rejected(self):
        for change in (
            {'FragmentPath': '/tmp/'+self.unit}, {'FragmentPath': ''},
            {'DropInPaths': '/tmp/override.conf'},
            {'FragmentPath': '/etc/systemd/system/has space.service'},
        ):
            with self.subTest(change=change):
                self.output(**change)
                with self.assertRaisesRegex(release.ReleaseError, 'unit_path_invalid'):
                    continuation.failed_supervisor(self.unit)
        self.protected.assert_not_called()

    def test_invalid_units_and_unknown_systemd_outcomes_fail_closed(self):
        for unit in (None, 'unrelated.service', 'sixnine-x.service --all', '../sixnine-x.service'):
            with self.subTest(unit=unit):
                with self.assertRaisesRegex(release.ReleaseError, 'unit_invalid'):
                    continuation.failed_supervisor(unit)
        self.run.assert_not_called()
        for error in (OSError('synthetic'), subprocess.TimeoutExpired('systemctl', 20)):
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = error
                with self.assertRaisesRegex(release.ReleaseError, 'supervisor_unknown'):
                    continuation.failed_supervisor(self.unit)
        self.protected.assert_not_called()


if __name__ == '__main__':
    unittest.main()
