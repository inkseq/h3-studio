"""Inert host-boundary tests: fake processes/commands, temporary public sources."""
from contextlib import ExitStack
import copy
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_platform_gpu_scaler import scaler, handoff, release, load, on_demand_configuration

with patch.dict(sys.modules, {'release': release, 'gpu_scaler': scaler, 'gpu_handoff': handoff}):
    host = load('test_preparation_hold_host_module', 'preparation_hold_recovery.py')


class PreparationHoldHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.operation = self.root/'operation'
        self.operation.mkdir()
        (self.operation/'sources').mkdir()
        self.sources = self.root/'public-source'
        self.sources.mkdir()
        (self.root/'operator').mkdir()
        (self.root/'control').mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(scaler, 'ROOT', self.root))
        self.stack.enter_context(patch.object(scaler, 'SOURCE', self.sources))
        self.stack.enter_context(patch.object(scaler, 'CONFIG_SOURCE', self.root/'operator'/'scaler.json'))
        self.stack.enter_context(patch.object(scaler, 'read_json', side_effect=lambda p, *a: json.loads(Path(p).read_text())))
        self.stack.enter_context(patch.object(host, 'secure_directory'))
        def regular(path, **kwargs):
            observed = Path(path).stat()
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=observed.st_size, st_uid=0)
        self.stack.enter_context(patch.object(release, 'regular', side_effect=regular))
        self.stack.enter_context(patch.object(release, 'sync_directory'))
        self.stack.enter_context(patch.object(host, 'runtime_json', side_effect=lambda p: json.loads(Path(p).read_text())))
        if not hasattr(os, 'chown'):
            self.stack.enter_context(patch.object(host.os, 'chown', create=True))
        else:
            self.stack.enter_context(patch.object(host.os, 'chown'))
        self.old = {**on_demand_configuration(), 'source_sha256': {'bootstrap_cloud.py': self.digest(b'old')},
                    'capacity_approval_id': 'synthetic-approval'}
        self.target = {**self.old, 'source_sha256': {'bootstrap_cloud.py': self.digest(b'new')}}
        (self.sources/'bootstrap_cloud.py').write_bytes(b'old')
        (self.operation/'sources'/'bootstrap_cloud.py').write_bytes(b'new')
        self.old_commit, self.commit = '1'*40, '2'*40
        self.state = {'version': 1, 'config_hash': scaler.fingerprint(self.old), 'sequence': 1,
            'created_at': self.old['created_at'], 'transfer_from': None}
        self.receipt = {'version': 1, 'phase': 'retired', 'old_commit': self.old_commit, 'target_commit': self.commit,
            'old_config_hash': scaler.fingerprint(self.old), 'target_config_hash': scaler.fingerprint(self.target),
            'sequence': 1, 'intent_id': 'fake-intent', 'original_service_state': self.state,
            'ledger': {'transfer_from': 'synthetic-approval-001', 'snapshot': {'job_hashes': {'fake-job': 'a'*64}}},
            'supervisor': {'unit': 'sixnine-synthetic.service', 'pid': 123, 'active_state': 'active'},
            'frozen': {'container_id': 'container-one', 'image_id': 'sha256:'+'a'*64, 'frozen_at': 100}}
        self.write('previous.json', self.old); self.write('target.json', self.target); self.write('receipt.json', self.receipt)
        scaler.atomic(scaler.CONFIG_SOURCE, self.old)
        scaler.atomic(self.root/'control'/'service-state.json', self.state)
        self.core = self.stack.enter_context(patch.object(host, 'core', return_value={'verified': True}))
        self.protected = self.stack.enter_context(patch.object(scaler, 'protected_inputs',
            side_effect=lambda **k: scaler.read_json(scaler.CONFIG_SOURCE)))
        self.retired = self.stack.enter_context(patch.object(host, 'require_retired', return_value=(self.old, self.target, {})))
        self.commands = self.stack.enter_context(patch.object(release, 'command', return_value=b''))
        self.launch = self.stack.enter_context(patch.object(scaler, 'launch'))

    @staticmethod
    def digest(value):
        return hashlib.sha256(value).hexdigest()

    def write(self, name, value):
        scaler.atomic(self.operation/name, value)

    def test_source_or_contract_mismatch_fails_before_any_mutation(self):
        (self.operation/'sources'/'bootstrap_cloud.py').write_bytes(b'wrong')
        with self.assertRaises(release.ReleaseError):
            host.stage(self.operation)
        self.assertEqual((self.sources/'bootstrap_cloud.py').read_bytes(), b'old')
        self.assertEqual(scaler.read_json(scaler.CONFIG_SOURCE), self.old)
        self.assertEqual(scaler.read_json(self.operation/'receipt.json')['phase'], 'retired')
        self.launch.assert_not_called()
        self.commands.assert_not_called()

    def test_stage_backups_old_source_and_preserves_cycle_evidence(self):
        result = host.stage(self.operation)
        self.assertEqual(result, {'phase': 'staged', 'next_sequence': 2})
        self.assertEqual((self.operation/'before'/'bootstrap_cloud.py').read_bytes(), b'old')
        self.assertEqual((self.sources/'bootstrap_cloud.py').read_bytes(), b'new')
        self.assertEqual(scaler.read_json(scaler.CONFIG_SOURCE), self.target)
        self.assertEqual(scaler.read_json(self.root/'control'/'service-state.json')['transfer_from'], 'synthetic-approval-001')
        self.assertEqual(self.core.call_args.args[2], 'verify')
        self.assertTrue(self.core.call_args.args[3]['host_stage_confirmed'])
        self.commands.assert_not_called()
        self.launch.assert_not_called()

    def test_failed_post_publish_validation_holds_started_phase_and_never_launches(self):
        self.protected.side_effect = [self.old, release.ReleaseError('synthetic')]
        with self.assertRaises(release.ReleaseError):
            host.stage(self.operation)
        self.assertEqual(scaler.read_json(self.operation/'receipt.json')['phase'], 'stage_started')
        self.assertEqual((self.operation/'before'/'bootstrap_cloud.py').read_bytes(), b'old')
        with self.assertRaises(release.ReleaseError):
            host.stage(self.operation)
        self.launch.assert_not_called()

    def test_prepare_marks_uncertainty_before_mutating_ledger(self):
        self.receipt.update(phase='frozen', reviewed_source_files=['bootstrap_cloud.py'], runtime_source_binding={})
        self.write('receipt.json', self.receipt)
        with patch.object(host, 'frozen', return_value=(self.old, self.target, {})):
            def call(directory, commit, action, value, *, apply=False):
                if apply:
                    self.assertEqual(scaler.read_json(directory/'receipt.json')['phase'], 'prepare_started')
                    raise release.ReleaseError('lost_response')
                return {'phase': 'dry_run'}
            self.core.side_effect = call
            with self.assertRaises(release.ReleaseError):
                host.prepare(self.operation, ['fake-job'])
            with self.assertRaises(release.ReleaseError):
                host.prepare(self.operation, ['fake-job'])
        self.assertEqual(self.core.call_count, 2)
        self.commands.assert_not_called()

    def test_supervisor_must_own_this_exact_controller_docker_client(self):
        unit = {'unit': 'sixnine-synthetic.service', 'pid': 100, 'active_state': 'active'}
        main = ['/usr/bin/python3', str(Path(scaler.__file__).resolve()), 'start']
        child = [release.DOCKER, 'compose', 'run', '--name', scaler.container_name(self.old),
            '-m', scaler.ENTRY_MODULE, '--credential-stdin']
        with patch.object(handoff, 'supervisor', return_value=unit), \
                patch.object(handoff, 'command', return_value=b'InvocationID='+b'a'*32+b'\n'), \
                patch.object(Path, 'read_text', return_value='101'), \
                patch.object(host, 'process_identity', side_effect=[(main, 99), (child, 101)]):
            bound = host.bound_supervisor(unit['unit'], self.old)
        self.assertEqual(bound['docker_pid'], 101)
        child[child.index('--name')+1] = 'some-other-controller'
        with patch.object(handoff, 'supervisor', return_value=unit), \
                patch.object(handoff, 'command', return_value=b'InvocationID='+b'a'*32+b'\n'), \
                patch.object(Path, 'read_text', return_value='101'), \
                patch.object(host, 'process_identity', side_effect=[(main, 99), (child, 101)]), \
                self.assertRaises(release.ReleaseError):
            host.bound_supervisor(unit['unit'], self.old)

    def test_retire_fences_before_exact_kills_and_never_terms_or_unpauses(self):
        self.receipt['phase'] = 'fenced'
        self.write('receipt.json', self.receipt)
        killed = []
        def main_kill(*args):
            self.assertEqual(scaler.read_json(self.operation/'receipt.json')['phase'], 'retire_started')
            killed.append(('main', args))
        self.commands.side_effect = lambda args, **kwargs: killed.append(('docker', args)) or b''
        with patch.object(host, 'frozen', return_value=(self.old, self.target, {})), \
                patch.object(handoff, 'command', side_effect=main_kill), \
                patch.object(host, 'supervisor_exited', return_value=True), \
                patch.object(scaler, 'marker'), \
                patch.object(handoff, 'inspect', return_value={'Id': 'container-one', 'Image': 'sha256:'+'a'*64,
                    'RestartCount': 0, 'State': {'Running': False, 'Paused': False, 'Restarting': False,
                        'OOMKilled': False, 'ExitCode': 137, 'Status': 'exited'}}):
            self.assertEqual(host.retire(self.operation, sleep=lambda _: None)['phase'], 'retired')
        self.assertEqual(killed[0][0], 'main')
        self.assertEqual(killed[1][1], ['kill', '--signal=SIGKILL', scaler.container_name(self.old)])
        self.assertEqual(self.core.call_count, 2)
        self.launch.assert_not_called()

    def test_retirement_waits_for_original_client_but_allows_reaped_or_zombie_exit(self):
        saved = {'unit': 'sixnine-synthetic.service', 'pid': 100, 'start_ticks': 50,
            'docker_pid': 101, 'docker_start_ticks': 60, 'invocation_id': 'a'*32}
        def proc(state, ticks):
            return '101 (docker) '+state+' '+' '.join(['0']*18)+' '+str(ticks)
        with patch.object(handoff, 'command', return_value=b'MainPID=0\nLoadState=not-found\n'), \
                patch.object(Path, 'read_text', side_effect=[FileNotFoundError(), proc('S', 60)]):
            self.assertFalse(host.supervisor_exited(saved, include_client=True))
        with patch.object(handoff, 'command', return_value=b'MainPID=0\nLoadState=not-found\n'), \
                patch.object(Path, 'read_text', side_effect=[FileNotFoundError(), proc('Z', 60)]):
            self.assertTrue(host.supervisor_exited(saved, include_client=True))

    def configure_resume(self):
        host.stage(self.operation)
        self.stack.enter_context(patch.object(scaler, 'checked_release', return_value=(self.commit, self.root/'release', {'SIXNINE_IMAGE':'synthetic'})))
        self.stack.enter_context(patch.object(scaler, 'verify_marker', return_value={'active': False, 'admission':'closed'}))
        self.stack.enter_context(patch.object(scaler, 'controller_control', return_value={'config_valid': True,
            'provider_calls_enabled': False, 'config_hash': scaler.fingerprint(self.target)}))
        self.stack.enter_context(patch.object(scaler, 'require_new_controller'))
        self.stack.enter_context(patch.object(scaler, 'marker'))
        self.stack.enter_context(patch.object(scaler, 'wait_until_ready'))
        self.stack.enter_context(patch.object(scaler, 'enable_admission'))
        self.stack.enter_context(patch.object(release, 'app_admission_overlay', return_value={}))

    def test_resume_never_launches_after_unknown_release_fence_outcome(self):
        self.configure_resume()
        def call(directory, commit, action, value, **kwargs):
            if action == 'release-leader':
                self.assertEqual(scaler.read_json(directory/'receipt.json')['phase'], 'resume_started')
                raise release.ReleaseError('lost_response')
            return {'verified': True}
        self.core.side_effect = call
        with self.assertRaises(release.ReleaseError):
            host.resume(self.operation)
        self.launch.assert_not_called()
        with self.assertRaises(release.ReleaseError):
            host.resume(self.operation)

    def test_failed_readiness_retains_started_phase_and_closes_admission_only(self):
        self.configure_resume()
        with patch.object(scaler, 'wait_until_ready', side_effect=release.ReleaseError('not_ready')), \
                patch.object(scaler, 'close_admission') as close:
            with self.assertRaises(release.ReleaseError):
                host.resume(self.operation)
            close.assert_called_once()
        self.launch.assert_called_once()
        self.assertEqual(scaler.read_json(self.operation/'receipt.json')['phase'], 'resume_started')
        self.assertFalse(any(call.args[0][0] in ('kill','stop','pause','unpause') for call in self.commands.call_args_list))

    def test_cli_validation_errors_are_static_and_other_exception_text_is_hidden(self):
        fake_fcntl = SimpleNamespace(flock=lambda *a: None, LOCK_EX=2, LOCK_NB=4)
        for error, expected in ((release.ReleaseError('repair_static_failure'), 'repair_static_failure'),
                (RuntimeError('do-not-echo-private-input'), 'preparation_repair_incomplete_reconcile_receipt'),
                (release.ReleaseError('do-not-echo-private-input'), 'preparation_repair_incomplete_reconcile_receipt')):
            with self.subTest(error=type(error).__name__), patch.dict(sys.modules, {'fcntl': fake_fcntl}), \
                    patch.object(release, 'check_host', side_effect=error), patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(host.main(['--operation', '00000000-0000-4000-8000-000000000000', '--action', 'stage']), 1)
                self.assertEqual(json.loads(output.getvalue())['safe_error'], expected)
                self.assertNotIn('private-input', output.getvalue())


if __name__ == '__main__':
    unittest.main()
