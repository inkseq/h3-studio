"""Host recovery validation is offline; mocks never rent or launch."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_platform_gpu_scaler import scaler, handoff, release, load, on_demand_configuration

with patch.dict(sys.modules, {'release': release, 'gpu_scaler': scaler, 'gpu_handoff': handoff}):
    recovery = load('test_preparation_host', 'gpu_preparation_recovery.py')


class PreparationHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'operator').mkdir()
        (self.root/'control').mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(scaler, 'ROOT', self.root))
        # CI intentionally runs as an unprivileged user. File ownership gates
        # are covered by the release tests; this fixture tests recovery state.
        self.stack.enter_context(patch.object(scaler, 'read_json',
            side_effect=lambda filename, maximum=65536: json.loads(Path(filename).read_text())))
        self.config = on_demand_configuration()
        self.config['capacity_approval_id'] = 'synthetic-approval'
        self.commit, self.old_commit = 'b'*40, 'a'*40
        self.state = {'version': 1, 'config_hash': scaler.fingerprint(self.config),
            'sequence': 3, 'created_at': self.config['created_at'], 'transfer_from': 'cycle-002'}
        self.receipt = {'version': 1, 'phase': 'staged', 'target_commit': self.commit,
            'old_commit': self.old_commit, 'config_hash': scaler.fingerprint(self.config),
            'supervisor': {'unit': 'sixnine-synthetic.service', 'pid': 0, 'active_state': 'inactive'},
            'next_service_state': self.state,
            'old_service_state': {**self.state, 'sequence': 2},
            'ledger': {'host_stage_confirmed': True, 'previous_sequence': 2, 'next_sequence': 3,
                'previous_approval_id': 'synthetic-approval-002', 'next_approval_id': 'synthetic-approval-003',
                'target_runtime_revision': self.commit, 'created_at': self.config['created_at'],
                'hard_deadline': self.config['hard_deadline'],
                'old_config_hash': scaler.fingerprint(self.config), 'target_config_hash': scaler.fingerprint(self.config),
                'restored_job_hashes': {'synthetic-job': 'd'*64}, 'jobs': [{'job_id': 'synthetic-job'}]}}
        proof = {'sequence': 2, 'job_ids': ['synthetic-job']}
        self.receipt['proof_sha256'] = self.receipt['ledger']['evidence_sha256'] = scaler.fingerprint(proof)
        scaler.atomic(recovery.path(recovery.PROOF), proof)
        scaler.atomic(recovery.path(recovery.RECEIPT), self.receipt)
        scaler.atomic(recovery.path(recovery.OLD), self.config)
        self.stack.enter_context(patch.object(recovery, 'runtime_json', return_value=self.state))
        self.stack.enter_context(patch.object(handoff, 'supervisor', return_value=self.receipt['supervisor']))
        self.no_controller = self.stack.enter_context(patch.object(scaler, 'require_new_controller'))
        self.stack.enter_context(patch.object(scaler, 'verify_marker', return_value={'active': False, 'admission': 'closed'}))
        self.core = self.stack.enter_context(patch.object(recovery, 'core', return_value={'verified': True}))

    def test_exact_staged_identity_releases_once_and_consumes_before_start(self):
        result = recovery.activate_resume(self.config, self.commit, {})
        self.assertEqual(result['phase'], 'activated')
        self.assertEqual(self.core.call_args.args[:2], (self.commit, 'release-leader'))
        self.assertEqual(self.core.call_args.kwargs, {'apply': True})
        with self.assertRaises(release.ReleaseError):
            recovery.activate_resume(self.config, self.commit, {})
        self.assertEqual(self.core.call_count, 2)  # verify + one release, no repeats.

    def test_other_commit_account_budget_or_state_cannot_resume(self):
        with self.assertRaises(release.ReleaseError):
            recovery.verify_resume(self.config, 'c'*40, {})
        for key, value in (('hard_deadline', self.config['hard_deadline']+1),
                           ('secret_version_id', 'different-profile-version'),
                           ('max_cycles', 7)):
            with self.subTest(key=key), self.assertRaises(release.ReleaseError):
                recovery.verify_resume({**self.config, key: value}, self.commit, {})
        with patch.object(recovery, 'runtime_json', return_value={**self.state, 'sequence': 1}):
            with self.assertRaises(release.ReleaseError):
                recovery.verify_resume(self.config, self.commit, {})
        self.core.assert_not_called()

    def test_active_supervisor_open_admission_or_drain_flag_blocks(self):
        with patch.object(handoff, 'supervisor', return_value={'pid': 22}):
            with self.assertRaises(release.ReleaseError):
                recovery.verify_resume(self.config, self.commit, {})
        with patch.object(scaler, 'verify_marker', return_value={'active': True, 'admission': 'open'}):
            with self.assertRaises(release.ReleaseError):
                recovery.verify_resume(self.config, self.commit, {})
        (self.root/'control'/'drain.flag').touch()
        with self.assertRaises(release.ReleaseError):
            recovery.verify_resume(self.config, self.commit, {})
        self.core.assert_not_called()

    def test_lost_release_result_never_launches_or_consumes_activation(self):
        self.core.side_effect = [ {'verified': True}, release.ReleaseError('synthetic_failure') ]
        with self.assertRaises(release.ReleaseError):
            recovery.activate_resume(self.config, self.commit, {})
        self.assertEqual(scaler.read_json(recovery.path(recovery.RECEIPT))['phase'], 'staged')

    def test_skipped_cycle_changed_approval_or_dropped_job_cannot_stage(self):
        for change in ({'next_sequence': 4}, {'previous_approval_id': 'other'},
                       {'restored_job_hashes': {}}, {'jobs': []}):
            with self.subTest(change=change):
                changed = copy.deepcopy(self.receipt)
                changed['ledger'].update(change)
                with self.assertRaises(release.ReleaseError):
                    recovery.validate_ledger_shape(changed, self.config)


class CollectedSupervisorTests(unittest.TestCase):
    """Synthetic copies of the exact observed GC'd-unit metadata; no host calls."""
    def setUp(self):
        self.config = on_demand_configuration()
        self.unit, self.old_commit = 'sixnine-ondemand-recovery-20261005.service', 'a'*40
        self.invocation, self.boot = 'd66e9d61647f4a41a3ed98c442e066d8', 'e'*32
        self.anchor = {'phase': 'activated', 'target_commit': self.old_commit,
            'new_config_hash': scaler.fingerprint(self.config), 'activated_at': 1791127994.7920716,
            # This earlier supervisor must never be treated as the new root.
            'supervisor': {'unit': 'sixnine-prior.service', 'pid': 187028}}
        common = {'_UID': '0', '_BOOT_ID': self.boot}
        systemd = {**common, '_PID': '1', 'UNIT': self.unit, 'INVOCATION_ID': self.invocation}
        self.rows = [
            {**systemd, 'MESSAGE_ID': recovery.UNIT_STARTED, 'JOB_TYPE': 'start', 'JOB_RESULT': 'done',
                '__REALTIME_TIMESTAMP': '1791127956089913'},
            {**common, '_PID': '264160', '_SYSTEMD_INVOCATION_ID': self.invocation,
                '_SYSTEMD_UNIT': self.unit, '_SYSTEMD_CGROUP': '/system.slice/'+self.unit,
                '_CMDLINE': '/usr/bin/python3 /opt/sixnine-release/gpu_scaler.py resume-handoff',
                '_EXE': '/usr/bin/python3.12', '__REALTIME_TIMESTAMP': '1791129522963029',
                'MESSAGE': json.dumps({'state': 'finite_cycle_complete_cpu_restored',
                    'billing_pending': 0, 'instance_count': 2})},
            {**systemd, 'MESSAGE_ID': recovery.UNIT_SUCCEEDED, '__REALTIME_TIMESTAMP': '1791129523069917'},
            {**systemd, 'MESSAGE_ID': recovery.UNIT_RESOURCES, '__REALTIME_TIMESTAMP': '1791129523070714'},
        ]
        self.fields = {'LoadState': 'not-found', 'MainPID': '0', 'Restart': 'no',
            'KillMode': 'control-group', 'ActiveState': 'inactive', 'SubState': 'dead',
            'ExecMainStatus': '0', 'InvocationID': '', 'User': '', 'Group': ''}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(recovery.time, 'time', return_value=1791129600))

    def verify(self, rows=None, anchor=None):
        return recovery.verify_completion_journal(self.rows if rows is None else rows,
            self.unit, self.old_commit, self.config, self.anchor if anchor is None else anchor)

    def fallback(self):
        self.stack.enter_context(patch.object(handoff, 'supervisor',
            side_effect=release.ReleaseError('handoff_supervisor_restart_policy_unsafe')))
        self.inspect = self.stack.enter_context(patch.object(recovery, '_systemd_fields', return_value=self.fields))
        self.journal = self.stack.enter_context(patch.object(recovery, 'completion_journal', return_value=self.rows))
        self.stack.enter_context(patch.object(scaler, 'read_json', return_value=self.anchor))

    def test_observed_shape_binds_unique_new_root_and_persists_hashes_only(self):
        host = self.verify()
        self.assertEqual(host['history']['main_pid'], 264160)
        self.assertEqual(host['history']['invocation_id'], self.invocation)
        self.assertEqual(host['history']['instance_count'], 2)
        self.assertEqual(host['history']['journal_sha256'], scaler.fingerprint(self.rows))
        self.assertNotIn('MESSAGE', json.dumps(host))
        self.assertNotIn('_CMDLINE', json.dumps(host))
        self.assertEqual(self.verify(self.rows[:3])['history']['main_pid'], 264160)

    def test_foreign_restarted_incomplete_or_unsuccessful_history_is_rejected(self):
        changes = [
            (1, '_UID', '10001'), (1, '_PID', '1'), (1, '_SYSTEMD_INVOCATION_ID', 'f'*32),
            (1, '_SYSTEMD_CGROUP', '/system.slice/other.service'),
            (1, '_CMDLINE', '/usr/bin/python3 /opt/sixnine-release/gpu_scaler.py start'),
            (1, '_EXE', '/tmp/python3'), (1, '_BOOT_ID', 'f'*32),
            (0, 'UNIT', 'sixnine-other.service'), (0, 'JOB_RESULT', 'failed'),
            (2, 'MESSAGE_ID', 'f'*32), (2, '__REALTIME_TIMESTAMP', '1791129522963028'),
        ]
        for index, key, value in changes:
            with self.subTest(index=index, key=key):
                rows = copy.deepcopy(self.rows)
                rows[index][key] = value
                with self.assertRaises(release.ReleaseError):
                    self.verify(rows)
        for rows in (self.rows[1:], self.rows[:2], [*self.rows[:3], self.rows[1]], [*self.rows, self.rows[1]]):
            with self.subTest(rows=len(rows)), self.assertRaises(release.ReleaseError):
                self.verify(rows)
        for change in ({'billing_pending': 1}, {'billing_pending': False},
                       {'instance_count': 0}, {'instance_count': 9}, {'commit': self.old_commit}):
            rows = copy.deepcopy(self.rows)
            message = json.loads(rows[1]['MESSAGE'])
            message.update(change)
            rows[1]['MESSAGE'] = json.dumps(message)
            with self.subTest(message=change), self.assertRaises(release.ReleaseError):
                self.verify(rows)

    def test_original_handoff_commit_configuration_and_activation_window_are_required(self):
        for change in ({'phase': 'staged'}, {'target_commit': 'b'*40}, {'new_config_hash': 'f'*64},
                       {'activated_at': 1791127955}, {'activated_at': 1791129524},
                       {'activated_at': float('nan')}):
            with self.subTest(change=change), self.assertRaises(release.ReleaseError):
                self.verify(anchor={**self.anchor, **change})

    def test_journal_capture_rejects_truncation_bad_exit_and_oversize(self):
        raw = b'\n'.join(json.dumps(row).encode() for row in self.rows)+b'\n'
        with patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=raw)) as run:
            self.assertEqual(recovery.completion_journal(self.unit), self.rows)
            command = run.call_args.args[0]
            self.assertIn('--boot=0', command)
            self.assertIn('--lines=129', command)
            self.assertIn('--unit='+self.unit, command)
        for result in (SimpleNamespace(returncode=1, stdout=raw),
                       SimpleNamespace(returncode=0, stdout=b'{}\n'*129),
                       SimpleNamespace(returncode=0, stdout=b'x'*(recovery.JOURNAL_BYTES+1)),
                       SimpleNamespace(returncode=0, stdout=b'{invalid}\n')):
            with patch.object(recovery.subprocess, 'run', return_value=result), self.assertRaises(release.ReleaseError):
                recovery.completion_journal(self.unit)

    def test_collected_unit_must_stay_absent_with_unchanged_history_and_anchor(self):
        self.fallback()
        host = recovery.retired_supervisor(self.config, self.unit, self.old_commit)
        self.assertEqual(recovery.retired_supervisor(self.config, self.unit, self.old_commit, expected=host), host)
        self.rows[3]['__REALTIME_TIMESTAMP'] = '1791129523070715'
        with self.assertRaisesRegex(release.ReleaseError, 'history_changed'):
            recovery.retired_supervisor(self.config, self.unit, self.old_commit, expected=host)
        self.rows[3]['__REALTIME_TIMESTAMP'] = '1791129523070714'
        self.anchor['activated_at'] += 1
        with self.assertRaisesRegex(release.ReleaseError, 'history_changed'):
            recovery.retired_supervisor(self.config, self.unit, self.old_commit, expected=host)
        with patch.object(handoff, 'supervisor', return_value={'unit': self.unit, 'pid': 0, 'active_state': 'inactive'}):
            with self.assertRaisesRegex(release.ReleaseError, 'history_changed'):
                recovery.retired_supervisor(self.config, self.unit, self.old_commit, expected=host)

    def test_collected_stub_can_omit_execstart_but_not_positive_absence_fields(self):
        raw = '\n'.join(key+'='+value for key, value in self.fields.items()).encode()
        with patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=raw)) as run:
            self.assertEqual(recovery._systemd_fields(self.unit), self.fields)
            self.assertIn('--all', run.call_args.args[0])
        self.fallback()
        self.assertEqual(recovery.retired_supervisor(self.config, self.unit, self.old_commit)['history']['main_pid'], 264160)
        self.journal.reset_mock()
        missing = dict(self.fields)
        del missing['LoadState']
        with patch.object(recovery, '_systemd_fields', return_value=missing), self.assertRaises(release.ReleaseError):
            recovery.retired_supervisor(self.config, self.unit, self.old_commit)
        self.journal.assert_not_called()

    def test_loaded_failed_running_or_unknown_inspection_never_uses_history(self):
        self.fallback()
        for key, value in (('LoadState', 'loaded'), ('MainPID', '264160'), ('ActiveState', 'failed'),
                           ('SubState', 'running'), ('Restart', 'always'), ('ExecMainStatus', '1'),
                           ('InvocationID', self.invocation), ('ExecStart', 'some-command'), ('User', 'root')):
            with self.subTest(key=key), patch.object(recovery, '_systemd_fields', return_value={**self.fields, key:value}):
                with self.assertRaises(release.ReleaseError):
                    recovery.retired_supervisor(self.config, self.unit, self.old_commit)
        with patch.object(handoff, 'supervisor', side_effect=release.ReleaseError('handoff_systemd_operation_failed')):
            with self.assertRaises(release.ReleaseError):
                recovery.retired_supervisor(self.config, self.unit, self.old_commit)
        self.journal.assert_not_called()

    def test_collected_root_count_must_match_fresh_settled_old_ledger(self):
        self.fallback()
        state = {'Running': False, 'Restarting': False, 'OOMKilled': False, 'ExitCode': 0, 'Status': 'exited'}
        status = {'billing_pending': 0, 'instances': [{}, {}]}
        with patch.object(scaler, 'checked_release', return_value=(None, Path('/synthetic'), {})), \
             patch.object(scaler, 'inspect_controller', return_value=state), \
             patch.object(scaler, 'controller_control', return_value=status), \
             patch.object(scaler, 'fresh_drained', return_value=True):
            self.assertTrue(recovery.retired(self.config, self.unit, self.old_commit)[0]['controller_exited'])
            status['instances'] = [{}]
            with self.assertRaisesRegex(release.ReleaseError, 'ledger_count_mismatch'):
                recovery.retired(self.config, self.unit, self.old_commit)


if __name__ == '__main__':
    unittest.main()
