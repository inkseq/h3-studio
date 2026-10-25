"""Exact default-profile maintenance with fake CPU services; no host or provider writes."""

import json
import io
import os
import sys
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch
import test_migration_release as fixture

maintenance, release, COMMIT, PROFILE = (
    fixture.maintenance,
    fixture.release,
    fixture.COMMIT,
    fixture.PROFILE,
)

NEW = 'h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7'


class DefaultProfileMaintenance(unittest.TestCase):
    def setUp(self):
        fixture.StoryboardMaintenance.setUp(self)
        self.real_idle = maintenance._default_idle
        self.real_probe = maintenance._default_policy_probe
        self.idle = self.stack.enter_context(patch.object(maintenance, '_default_idle'))
        self.probe = self.stack.enter_context(
            patch.object(maintenance, '_default_policy_probe', return_value='4' * 64)
        )
        self.digest = self.stack.enter_context(
            patch.object(maintenance, '_default_profiles_digest', return_value='4' * 64)
        )
        self.packaged = self.stack.enter_context(
            patch.object(maintenance, 'default_profile')
        )

    def update(self, old=PROFILE, paths=None):
        return maintenance.default_profile_only_locked(
            self.root,
            COMMIT,
            old,
            NEW,
            paths or release.checksum(self.paths),
            release.checksum(self.active),
        )

    def receipt(self):
        return maintenance.default_profile_receipt(self.root)

    def test_exact_setting_original_bytes_and_ordered_cpu_refresh(self):
        stop = maintenance._default_stop

        def checked_stop(context):
            receipt = self.receipt()
            self.assertEqual(receipt['status'], 'pending')
            saved, _, _ = maintenance._storyboard_backup(
                self.root,
                receipt,
                folder='default-profile-maintenance',
                flag=maintenance.DEFAULT_PROFILE_FLAG,
            )
            self.assertEqual(saved['before', 'paths'], self.before['paths'])
            return stop(context)

        with patch.object(maintenance, '_default_stop', side_effect=checked_stop):
            result = self.update()
        self.assertEqual(result['state'], 'approved_default_profile_updated')
        candidate = json.loads(self.paths.read_text())
        candidate['environment'][maintenance.DEFAULT_PROFILE_FLAG] = PROFILE
        self.assertEqual(candidate, self.config)
        self.assertEqual(self.release_state.read_bytes(), self.before['release'])
        self.assertEqual(
            json.loads(self.active.read_text())['paths_sha256'],
            release.checksum(self.paths),
        )
        self.assertEqual(
            [c[0] for c in self.calls],
            [
                ('stop', *maintenance.SHARED, 'app'),
                ('up', '-d', '--no-deps', '--force-recreate', 'app'),
                ('up', '-d', '--no-deps', '--force-recreate', *maintenance.SHARED),
            ],
        )
        self.assertEqual(self.idle.call_count, 3)
        self.assertEqual(self.probe.call_args_list[0].args[1:], (NEW, PROFILE))
        self.assertEqual(self.probe.call_args_list[-1].args[1:], (NEW, NEW))
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'completed')
        saved, _, _ = maintenance._storyboard_backup(
            self.root,
            receipt,
            folder='default-profile-maintenance',
            flag=maintenance.DEFAULT_PROFILE_FLAG,
        )
        self.assertEqual(saved['before', 'paths'], self.before['paths'])
        self.assertEqual(saved['before', 'active'], self.before['active'])
        maintenance.storyboard_maintenance_fence(self.root)
        with self.assertRaisesRegex(release.ReleaseError, 'original_binding_changed'):
            self.update(paths=receipt['before']['paths'])
        self.assertEqual(len(self.calls), 3)

    def test_cas_old_profile_and_unavailable_policy_refuse_before_intent(self):
        for kwargs in ({'old': 'other-profile'}, {'paths': 'f' * 64}):
            with self.assertRaisesRegex(
                release.ReleaseError, 'original_binding_changed'
            ):
                self.update(**kwargs)
        self.probe.side_effect = release.ReleaseError('policies unavailable')
        with self.assertRaises(release.ReleaseError):
            self.update()
        self.assertFalse(self.calls)
        self.assertIsNone(self.receipt())
        self.assertEqual(self.paths.read_bytes(), self.before['paths'])

    def test_busy_or_unpackaged_profile_never_stops_services(self):
        self.idle.side_effect = release.ReleaseError('busy')
        with self.assertRaises(release.ReleaseError):
            self.update()
        self.idle.side_effect = None
        self.packaged.side_effect = release.ReleaseError('not packaged')
        with self.assertRaises(release.ReleaseError):
            self.update()
        self.assertFalse(self.calls)
        self.assertIsNone(self.receipt())

    def test_new_job_after_precheck_resumes_unchanged_runtime_without_switch(self):
        self.idle.side_effect = [None, None, release.ReleaseError('new queued job')]
        with self.assertRaisesRegex(release.ReleaseError, 'failed_restored'):
            self.update()
        self.assertEqual(self.receipt()['status'], 'restored')
        self.assertEqual(self.paths.read_bytes(), self.before['paths'])
        self.assertEqual(self.active.read_bytes(), self.before['active'])
        self.assertTrue(
            all(
                env[maintenance.DEFAULT_PROFILE_FLAG] == PROFILE
                for _, env in self.calls
            )
        )

    def test_failed_candidate_restores_original_bytes_and_retains_receipt(self):
        with patch.object(
            maintenance, '_default_start', side_effect=[OSError('health'), None]
        ) as start:
            with self.assertRaisesRegex(release.ReleaseError, 'failed_restored'):
                self.update()
            self.assertEqual(
                start.call_args_list[0].args[0][2][maintenance.DEFAULT_PROFILE_FLAG],
                NEW,
            )
            self.assertEqual(
                start.call_args_list[1].args[0][2][maintenance.DEFAULT_PROFILE_FLAG],
                PROFILE,
            )
        self.assertEqual(self.receipt()['status'], 'restored')
        self.assertEqual(self.paths.read_bytes(), self.before['paths'])
        self.assertEqual(self.active.read_bytes(), self.before['active'])

    def test_unproven_restore_fences_deploys_and_does_not_replay(self):
        with patch.object(maintenance, '_default_start', side_effect=OSError('health')):
            with self.assertRaisesRegex(
                release.ReleaseError, 'reconciliation_required'
            ):
                self.update()
        self.assertEqual(self.receipt()['status'], 'failed')
        before = len(self.calls)
        with self.assertRaisesRegex(release.ReleaseError, 'pending_restore_required'):
            self.update()
        with self.assertRaisesRegex(release.ReleaseError, 'pending_restore_required'):
            maintenance.storyboard_maintenance_fence(self.root)
        self.assertEqual(len(self.calls), before)

    def test_restore_refuses_unrelated_config_changes_before_runtime_mutation(self):
        self.update()
        value = json.loads(self.paths.read_text())
        value['environment']['SIXNINE_OPERATOR_CAPACITY_OWNERS'] = 'superdan'
        self.paths.write_text(json.dumps(value))
        before = len(self.calls)
        with self.assertRaisesRegex(release.ReleaseError, 'unrelated_change'):
            maintenance._default_restore(self.root, COMMIT, self.receipt())
        self.assertEqual(len(self.calls), before)

    def test_zero_job_query_is_readonly_and_rejects_unknown_or_positive_counts(self):
        context = maintenance._storyboard_context(self.root, COMMIT, self.config)
        for raw in (b'BEGIN\n1\nCOMMIT\n', b'BEGIN\n\nCOMMIT\n', b'0\n'):
            with (
                patch.object(maintenance, 'compose', return_value=raw),
                self.assertRaises(release.ReleaseError),
            ):
                self.real_idle(context)
        with patch.object(
            maintenance, 'compose', return_value=b'BEGIN\n0\nCOMMIT\n'
        ) as command:
            self.real_idle(context)
            sql = command.call_args.args[-1]
            self.assertIn('BEGIN READ ONLY;', sql)
            self.assertIn("NOT IN ('succeeded','failed','cancelled')", sql)

    def test_partial_stop_with_unconfirmed_jobs_never_force_recreates_app(self):
        self.idle.side_effect = [
            None,
            release.ReleaseError('new job'),
            release.ReleaseError('busy'),
        ]
        with self.assertRaisesRegex(release.ReleaseError, 'reconciliation_required'):
            self.update()
        self.assertFalse(self.calls)
        self.assertEqual(self.receipt()['status'], 'failed')

    def test_explicit_restore_requires_original_digest_and_marks_failure_under_lock(
        self,
    ):
        import types
        from unittest.mock import Mock

        self.update()
        digest = release.checksum(
            self.root / 'migration' / maintenance.DEFAULT_PROFILE_RECEIPT
        )
        locked = Mock()
        fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=4, flock=locked)

        def fail_restore(*args):
            self.assertFalse(locked.call_args.args[0].closed)
            self.assertEqual(self.receipt()['status'], 'pending')
            raise OSError('synthetic failure')

        with (
            patch.dict(sys.modules, {'fcntl': fcntl}),
            patch.object(release, 'check_host'),
            patch.object(
                maintenance, '_default_restore', side_effect=fail_restore
            ) as restore,
        ):
            with self.assertRaisesRegex(
                release.ReleaseError, 'restore_binding_changed'
            ):
                maintenance.default_profile_maintenance(
                    self.root, COMMIT, receipt_sha256='0' * 64
                )
            restore.assert_not_called()
            with self.assertRaises(OSError):
                maintenance.default_profile_maintenance(
                    self.root, COMMIT, receipt_sha256=digest
                )
        self.assertEqual(self.receipt()['status'], 'failed')
        self.assertTrue(locked.call_args.args[0].closed)

    def test_policy_probe_requires_both_enabled_current_modes_and_exact_runtime_default(
        self,
    ):
        import hashlib

        path = self.root / 'policies.json'
        path.write_text('synthetic protected policies')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.digest.return_value = digest
        context = maintenance._storyboard_context(self.root, COMMIT, self.config)
        policies = {
            (NEW, recipe): {
                'enabled': True,
                'backend': 'wangp-worker',
                'qualification': {
                    'status': 'accepted',
                    'verified_at': 1,
                    'expires_at': None,
                },
                'reservation': {'expected_runtime_s': 600, 'expires_at': None},
            }
            for recipe in ('h3-base-fl2va-v1', 'h3-base-ref2va-v1')
        }

        def run_code(b, d, e, *args, **kwargs):
            code = args[args.index('-c') + 1]
            out = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        'SIXNINE_EXECUTION_PROFILES_FILE': str(path),
                        maintenance.DEFAULT_PROFILE_FLAG: PROFILE,
                    },
                ),
                patch.object(sys, 'argv', ['probe', NEW, PROFILE]),
                patch(
                    'studio_platform.execution_profiles.read_profiles',
                    return_value=policies,
                ),
                redirect_stdout(out),
            ):
                exec(compile(code, 'maintenance-policy-probe', 'exec'), {})
            return out.getvalue().encode()

        with patch.object(maintenance, 'compose', side_effect=run_code):
            self.assertEqual(self.real_probe(context, NEW, PROFILE), digest)
            ref = policies[(NEW, 'h3-base-ref2va-v1')]
            ref['enabled'] = False
            with self.assertRaises(AssertionError):
                self.real_probe(context, NEW, PROFILE)
            ref['enabled'] = True
            ref['reservation']['expires_at'] = 1
            with self.assertRaises(AssertionError):
                self.real_probe(context, NEW, PROFILE)

    def test_runtime_checks_default_on_every_business_service_and_hashes_other_environment(
        self,
    ):
        names = maintenance.STORYBOARD_SERVICES
        identities = {name: str(i) * 64 for i, name in enumerate(names, 1)}
        values = {
            identities[name]: {
                'Image': 'sha256:' + '1' * 64,
                'State': {'Running': True},
                'Mounts': [],
                'Config': {
                    'Labels': {
                        'com.docker.compose.project': 'sixnine-platform',
                        'com.docker.compose.service': name,
                    },
                    'Env': ['PRIVATE_CONFIG=unchanged']
                    + (
                        [maintenance.DEFAULT_PROFILE_FLAG + '=' + NEW]
                        if name in {'app', 'dispatch', 'controller'}
                        else []
                    ),
                },
            }
            for name in names
        }
        with (
            patch.object(
                maintenance,
                'compose',
                side_effect=lambda b, d, e, *a, **kw: identities[a[-1]].encode(),
            ),
            patch.object(
                release,
                'command',
                side_effect=lambda args, **kw: json.dumps([values[args[1]]]).encode(),
            ),
        ):
            result = self.original_runtime(
                self.root,
                self.root,
                {maintenance.DEFAULT_PROFILE_FLAG: NEW},
                flag=maintenance.DEFAULT_PROFILE_FLAG,
            )
            self.assertNotIn('PRIVATE_CONFIG', json.dumps(result))
            values[identities['controller']]['Config']['Env'][-1] = (
                maintenance.DEFAULT_PROFILE_FLAG + '=' + PROFILE
            )
            with self.assertRaisesRegex(release.ReleaseError, 'flag_mismatch'):
                self.original_runtime(
                    self.root,
                    self.root,
                    {maintenance.DEFAULT_PROFILE_FLAG: NEW},
                    flag=maintenance.DEFAULT_PROFILE_FLAG,
                )


if __name__ == '__main__':
    unittest.main()
