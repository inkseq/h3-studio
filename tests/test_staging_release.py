"""Synthetic environment/isolation and artifact-promotion contracts; no AWS/GPU."""

import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from deploy.platform import deployment_target as target, release_candidate as candidate
from deploy.platform import migration_secrets_aws as secrets
from tools import (
    ci_changes,
    publish_staging_candidate as publisher,
    deploy_aws_release as runner,
)
from test_migration_secrets_aws import initialized, r2_storage

DIRECTORY = Path(__file__).resolve().parents[1] / 'deploy/platform'
sys.path.insert(0, str(DIRECTORY))
import environment_release as host
import offline_rehearsal

COMMIT = 'a' * 40
INSTANCE = 'i-0123456789abcdef0'
COMMAND = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def publication(kind='platform'):
    return {
        'version': 1,
        'commit': COMMIT,
        'kind': kind,
        'run_id': 42,
        'run_attempt': 1,
        'manifests': {
            name: candidate.digest(candidate.encode({'commit': COMMIT, 'name': name}))
            for name in (
                ('platform', 'migration', 'frontend')
                if kind == 'platform'
                else ('frontend',)
            )
        },
    }


def proof(value):
    return {
        'version': 1,
        'state': 'accepted',
        'commit': COMMIT,
        'candidate_sha256': candidate.digest(candidate.encode(value)),
        'staging_instance_id': INSTANCE,
        'checks': {
            name: 'passed'
            for name in ('browser_agent', 'isolation', 'migration_rehearsal')
        },
        'evidence': 'https://github.com/inkseq/h3-studio/issues/195#issuecomment-123',
    }


class TargetTests(unittest.TestCase):
    def test_stage_site_is_sealed_and_missing_record_never_becomes_production(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lstat = Path.lstat

            def fixture_lstat(path, *args, **kwargs):
                info = lstat(path, *args, **kwargs)
                if path == root:
                    fields = list(info)
                    fields[4] = 0
                    return os.stat_result(fields)
                return info

            with (
                patch.object(Path, 'lstat', fixture_lstat),
                self.assertRaisesRegex(ValueError, 'record_required'),
            ):
                target.site_environment(root, {'INKSEQ_DEPLOYMENT_TARGET': 'staging'})
            with patch.object(target, 'host', return_value=target.profile('staging')):
                value = target.site_environment(
                    root, {'INKSEQ_DEPLOYMENT_TARGET': 'staging'}
                )
                self.assertEqual(
                    value['INKSEQ_PUBLIC_ORIGIN'], 'https://staging.inkseq.com'
                )
                self.assertEqual(value['INKSEQ_PUBLIC_ORIGIN_ALIASES'], '')
                self.assertEqual(value['INKSEQ_CADDY_PROFILE'], 'Caddyfile.staging')
                with self.assertRaisesRegex(ValueError, 'site_mismatch'):
                    target.site_environment(
                        root, {'INKSEQ_DEPLOYMENT_TARGET': 'production'}
                    )

    def test_unprotected_host_parent_is_rejected_before_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lstat = Path.lstat
            for uid, mode in ((1000, 0o700), (0, 0o770)):

                def unprotected_parent(path, *args, **kwargs):
                    info = lstat(path, *args, **kwargs)
                    if path == root:
                        fields = list(info)
                        fields[4] = uid
                        fields[0] = info.st_mode & ~0o777 | mode
                        return os.stat_result(fields)
                    return info

                with (
                    self.subTest(uid=uid, mode=mode),
                    patch.object(target, 'os', types.SimpleNamespace(name='posix')),
                    patch.object(Path, 'lstat', unprotected_parent),
                    self.assertRaisesRegex(ValueError, 'parent_not_protected'),
                ):
                    target.host(root, legacy=True)

    def test_record_rejects_links_extra_fields_and_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / 'deployment-target.json'
            proxy = types.SimpleNamespace(
                name='nt',
                O_RDONLY=os.O_RDONLY,
                O_NOFOLLOW=getattr(os, 'O_NOFOLLOW', 0),
                open=os.open,
                fstat=os.fstat,
                read=os.read,
                close=os.close,
            )
            with patch.object(target, 'os', proxy):
                path.write_text(
                    json.dumps(
                        {'version': 1, 'target': 'staging', 'instance_id': INSTANCE}
                    )
                )
                self.assertEqual(target.host(root)['name'], 'staging')
                for raw in (
                    '{"version":1,"target":"staging","target":"production","instance_id":"'
                    + INSTANCE
                    + '"}',
                    json.dumps(
                        {
                            'version': 1,
                            'target': 'staging',
                            'instance_id': INSTANCE,
                            'secret_root': '/sixnine/platform',
                        }
                    ),
                ):
                    path.write_text(raw)
                    with self.assertRaises(ValueError):
                        target.host(root)

    def test_stage_shares_only_authorized_vast_profile_and_keeps_other_secrets_isolated(
        self,
    ):
        value = initialized()
        value['providers']['vastai']['profile_id'] = target.profile('staging')[
            'vast_profile'
        ]
        value['grafana_cloud'].update(
            enabled=False, endpoint='', authorization='', environment='staging'
        )
        value['r2_storage'] = dict(
            r2_storage(),
            profile=target.profile('staging')['r2_profile'],
            bucket='sixnine-staging-media',
        )
        secrets.validate(value, target='staging')
        self.assertEqual(
            target.profile('staging')['vast_profile'], 'vastai--sixnine-dstack-20261011'
        )
        self.assertEqual(
            target.profile('staging')['vast_profile'],
            target.profile('production')['vast_profile'],
        )
        output = secrets.outputs(value, target='staging')
        self.assertEqual(
            json.loads(output['dstack-server-config.yml'][0])['projects'][0]['name'],
            'sixnine-staging',
        )
        for mutate in (
            lambda v: v['r2_storage'].update(profile='cloudflare-r2--rig-root'),
            lambda v: v['r2_storage'].update(bucket='sixnine-media'),
            lambda v: v['providers']['vastai'].update(
                profile_id='vastai--sixnine-staging-20261015'
            ),
            lambda v: v['providers']['vastai'].update(profile_id='vastai--rig-root'),
            lambda v: v['providers']['vastai'].update(
                base_url='https://console.vast.ai/api/v0'
            ),
            lambda v: v['grafana_cloud'].update(enabled=True),
            lambda v: v['grafana_cloud'].update(
                authorization='Basic copied-production-config'
            ),
        ):
            changed = copy.deepcopy(value)
            mutate(changed)
            with self.assertRaises(ValueError):
                secrets.validate(changed, target='staging')
        with self.assertRaises(ValueError):
            secrets.validate(value)
        api = Mock()
        api.get_secret_value.return_value = {
            'SecretString': json.dumps(value),
            'VersionId': 'same',
        }
        secrets._managed(api, target='staging')
        api.get_secret_value.assert_called_once_with(
            SecretId='/sixnine/staging/migration'
        )

    def test_stage_proxy_has_no_production_domain_or_redirect(self):
        text = (DIRECTORY / 'Caddyfile.staging').read_text()
        self.assertIn('staging.inkseq.com {', text)
        self.assertNotIn('sixnine.art', text)
        self.assertNotIn('redir ', text)
        self.assertNotIn('INKSEQ_LEGACY_REDIRECT', text)


class CandidateTests(unittest.TestCase):
    def test_frontend_mismatch_never_writes_an_active_intent_or_calls_deploy(self):
        fake_lock = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=Mock())
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with (
                patch.dict(sys.modules, {'fcntl': fake_lock}),
                patch.object(
                    host.deployment_target,
                    'host',
                    return_value=target.profile('staging'),
                ),
                patch.object(host, 'publication', return_value=publication('frontend')),
                patch.object(host, 'approvals'),
                patch.object(
                    host,
                    'frontend_preflight',
                    side_effect=host.release.ReleaseError(
                        'frontend_requires_matching_deployed_api'
                    ),
                ),
                patch.object(host.deploy_frontend, 'deploy') as deploy,
                patch.object(host, 'retention_intent') as pin,
                self.assertRaisesRegex(
                    host.release.ReleaseError, 'matching_deployed_api'
                ),
            ):
                host.run('staging', COMMIT, root=root, api=Mock())
            deploy.assert_not_called()
            pin.assert_not_called()
            self.assertFalse((root / 'environment-release-intent.json').exists())

    def test_frontend_preflight_pins_manifest_and_checks_approved_current_contract(
        self,
    ):
        current = 'b' * 40
        value = publication('frontend')
        frontend = {
            'commit': COMMIT,
            'api_compatibility': 'c' * 64,
            'api_contract': 'sixnine-web-v1',
        }
        raw = candidate.encode(frontend)
        value['manifests']['frontend'] = candidate.digest(raw)
        backend = {
            'contracts': {
                'version': 1,
                'api_compatibility': 'd' * 64,
                'worker_compatibility': 'e' * 64,
                'frontend_contract': 'sixnine-web-v1',
            }
        }
        root = Path('synthetic-root')
        with (
            patch.object(host, 'read_object', return_value=raw),
            patch.object(
                host.release,
                '_protected_json',
                side_effect=[
                    {'current': current, 'pending': None, 'status': 'app_ready'},
                    backend,
                ],
            ),
            patch.object(host.release, 'approved_manifest') as approved,
            self.assertRaisesRegex(host.release.ReleaseError, 'matching_deployed_api'),
        ):
            host.frontend_preflight(root, value, Mock())
        approved.assert_called_once_with(root, root / 'releases' / current, current)
        backend['contracts']['api_compatibility'] = frontend['api_compatibility']
        with (
            patch.object(host, 'read_object', return_value=raw),
            patch.object(
                host.release,
                '_protected_json',
                side_effect=[
                    {'current': current, 'pending': None, 'status': 'app_ready'},
                    backend,
                ],
            ),
            patch.object(host.release, 'approved_manifest'),
        ):
            host.frontend_preflight(root, value, Mock())
        value['manifests']['frontend'] = 'f' * 64
        with (
            patch.object(host, 'read_object', return_value=raw),
            patch.object(host.release, '_protected_json') as read_state,
            self.assertRaisesRegex(
                host.release.ReleaseError, 'candidate_manifest_changed'
            ),
        ):
            host.frontend_preflight(root, value, Mock())
        read_state.assert_not_called()

    def test_cli_reports_closed_refusal_without_exposing_arbitrary_errors(self):
        for error, wanted in (
            (
                host.release.ReleaseError('frontend_requires_matching_deployed_api'),
                'frontend_requires_matching_deployed_api',
            ),
            (
                host.release.ReleaseError(
                    'another_environment_release_requires_reconciliation'
                ),
                'another_environment_release_requires_reconciliation',
            ),
            (BlockingIOError('private-path'), 'environment_release_lock_busy'),
            (RuntimeError('private-provider-payload'), 'environment_release_failed'),
        ):
            output = io.StringIO()
            with (
                patch.object(host, 'run', side_effect=error),
                patch('sys.stderr', output),
            ):
                self.assertEqual(host.main(['staging', COMMIT]), 1)
            self.assertIn(wanted, output.getvalue())
            self.assertNotIn('private-', output.getvalue())

    def test_acceptance_binds_complete_tuple_and_all_required_evidence(self):
        value = publication()
        accepted = proof(value)
        self.assertEqual(candidate.acceptance(accepted, value), accepted)
        for field in ('platform', 'migration', 'frontend'):
            changed = copy.deepcopy(value)
            changed['manifests'][field] = 'f' * 64
            with self.assertRaises(ValueError):
                candidate.acceptance(accepted, changed)
        changed = copy.deepcopy(accepted)
        changed['checks']['migration_rehearsal'] = 'not-tested'
        with self.assertRaises(ValueError):
            candidate.acceptance(changed, value)

    def test_stage_then_production_call_existing_deployers_with_identical_bytes(self):
        value = publication()
        api = Mock()

        def get(**kwargs):
            key = kwargs['Key']
            if key.startswith('staging-candidates/'):
                raw = candidate.encode(value)
            else:
                name = (
                    'migration'
                    if '/migration/' in key
                    else 'frontend'
                    if '/frontend/' in key
                    else 'platform'
                )
                raw = candidate.encode({'commit': COMMIT, 'name': name})
            return {'ContentLength': len(raw), 'Body': io.BytesIO(raw)}

        api.get_object.side_effect = get
        fake_lock = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=Mock())
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with (
                patch.dict(sys.modules, {'fcntl': fake_lock}),
                patch.object(
                    host.deployment_target,
                    'host',
                    return_value=dict(target.profile('staging'), instance_id=INSTANCE),
                ),
                patch.object(
                    host.frontend_release,
                    'protected_directory',
                    side_effect=lambda p, **kw: p.mkdir(exist_ok=True),
                ),
                patch.object(host.release, 'regular'),
                patch.object(host.deploy_approved, 'deploy') as platform,
                patch.object(host.deploy_frontend, 'deploy') as frontend,
            ):
                first = host.run('staging', COMMIT, root=root, api=api)
                platform.assert_called_once_with(COMMIT, root=root)
                frontend.assert_called_once_with(COMMIT, root=root, api=api)
                intent = json.loads(
                    (root / 'environment-release-intent.json').read_text()
                )
                self.assertEqual(
                    (intent['commit'], intent['state']), (COMMIT, 'completed')
                )
                accepted = proof(value)
                (root / 'promotion-candidates').mkdir()
                (root / 'approved-promotions').mkdir()
                receipt = root / 'promotion-candidates' / (COMMIT + '.json')
                receipt.write_bytes(candidate.encode(accepted))
                approval = root / 'approved-promotions' / (COMMIT + '.sha256')
                approval.write_text(host.release.checksum(receipt))
                with patch.object(
                    host.deployment_target, 'host', return_value=target.profile()
                ):
                    promoted = host.run('production', COMMIT, root=root, api=api)
                self.assertEqual(
                    first['candidate_sha256'], promoted['candidate_sha256']
                )
                self.assertEqual(platform.call_count, 2)
                self.assertEqual(frontend.call_count, 2)
                # Mutating the accepted artifact tuple cannot reuse the approval.
                accepted['evidence'] = (
                    'https://github.com/inkseq/h3-studio/issues/195#issuecomment-999'
                )
                receipt.write_bytes(candidate.encode(accepted))
                with (
                    patch.object(
                        host.deployment_target, 'host', return_value=target.profile()
                    ),
                    self.assertRaises(host.release.ReleaseError),
                ):
                    host.run('production', COMMIT, root=root, api=api)
                self.assertEqual(platform.call_count, 2)

    def test_full_tuple_retention_pin_resumes_only_original_unknown_operation(self):
        value = publication()
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(host.release, 'regular'),
        ):
            root = Path(name)
            host.retention_intent(root, value)
            before = (root / 'environment-release-intent.json').read_bytes()
            host.retention_intent(root, value)  # Same immutable tuple may resume.
            changed = copy.deepcopy(value)
            changed['commit'] = 'e' * 40
            with self.assertRaisesRegex(
                host.release.ReleaseError, 'requires_reconciliation'
            ):
                host.retention_intent(root, changed)
            self.assertEqual(
                (root / 'environment-release-intent.json').read_bytes(), before
            )
            host.retention_intent(root, value, completed=True)
            self.assertEqual(
                json.loads((root / 'environment-release-intent.json').read_text())[
                    'state'
                ],
                'completed',
            )
            host.retention_intent(
                root, changed
            )  # Original completed operation does not become a second authority.
            self.assertEqual(
                json.loads((root / 'environment-release-intent.json').read_text())[
                    'commit'
                ],
                'e' * 40,
            )

    def test_wrong_host_refuses_before_any_object_read_or_deployment(self):
        api = Mock()
        with (
            patch.object(host.deployment_target, 'host', return_value=target.profile()),
            self.assertRaises(host.release.ReleaseError),
        ):
            host.run('staging', COMMIT, api=api)
        api.get_object.assert_not_called()

    def test_new_ssm_targets_pin_version_one_and_preserve_old_production_version(self):
        api = Mock()
        api.send_command.return_value = {'Command': {'CommandId': COMMAND}}
        api.get_command_invocation.return_value = {
            'Status': 'Success',
            'DocumentName': 'Inkseq-DeployStagingRelease',
            'DocumentVersion': '1',
            'StandardOutputContent': json.dumps(
                {
                    'state': 'staging_release_healthy',
                    'commit': COMMIT,
                    'candidate_sha256': 'b' * 64,
                }
            ),
        }
        with (
            patch.object(runner, 'STAGING_INSTANCE', INSTANCE),
            patch('sys.stdout', io.StringIO()),
        ):
            runner.deploy(COMMIT, target='staging', api=api)
        self.assertEqual(api.send_command.call_args.kwargs['InstanceIds'], [INSTANCE])
        self.assertEqual(api.send_command.call_args.kwargs['DocumentVersion'], '1')
        with (
            patch.object(runner, 'STAGING_INSTANCE', None),
            self.assertRaises(ValueError),
        ):
            runner.deploy(COMMIT, target='staging', api=Mock())


class MainAndRehearsalTests(unittest.TestCase):
    def test_main_automatically_targets_stage_pr_and_docs_cannot_deploy(self):
        for path, action in (
            ('studio_platform/auth.py', 'stage-platform'),
            ('inkseq-web/src/App.jsx', 'stage-frontend'),
            ('README.md', 'check'),
        ):
            with (
                patch.object(ci_changes, 'git_changes', return_value=[('M', path)]),
                patch.object(
                    ci_changes,
                    'staging_platform_baseline',
                    return_value={'commit': 'b' * 40, 'run_id': 1, 'run_attempt': 1},
                ),
            ):
                result = ci_changes.plan(
                    'push', {'before': 'b' * 40}, COMMIT, 'refs/heads/main', Path('.')
                )
                self.assertEqual(result['action'], action)
                pr = ci_changes.plan(
                    'pull_request',
                    {'pull_request': {'base': {'sha': 'b' * 40}}},
                    COMMIT,
                    'refs/pull/9/merge',
                    Path('.'),
                )
                self.assertEqual(pr['action'], 'check')

    def test_rehearsal_copy_cannot_reach_network_credentials_or_public_services(self):
        plan = offline_rehearsal.commands(
            COMMIT, 'postgres@sha256:' + 'f' * 64, 'd' * 32
        )
        for name in ('database', 'migrate'):
            args = plan[name]
            self.assertEqual(args[args.index('--network') + 1], 'none')
            self.assertEqual(args[args.index('--pull') + 1], 'never')
            self.assertNotIn('--publish', args)
            self.assertNotIn('--privileged', args)
            self.assertFalse(
                any(
                    '/srv/sixnine' in value or '/run/sixnine-secrets' in value
                    for value in args
                )
            )
        self.assertEqual(
            plan['migrate'][-5:],
            [
                'sixnine-platform:' + COMMIT,
                'python',
                '-m',
                'studio_platform.manage',
                'init-db',
            ],
        )
        self.assertNotIn('controller', plan['migrate'])
        self.assertIn('--no-owner', plan['restore'])


class RehearsalReadinessTests(unittest.TestCase):
    def plan(self):
        return offline_rehearsal.commands(
            COMMIT, 'postgres@sha256:' + 'f' * 64, 'd' * 32
        )

    def test_bootstrap_socket_cannot_admit_restore_before_final_tcp(self):
        plan = self.plan()
        events = []

        def command(args, **kwargs):
            self.assertEqual(args[args.index('-h') + 1], '127.0.0.1')
            self.assertEqual(args[args.index('-p') + 1], '5432')
            events.append('tcp_probe')
            if len(events) < 3:
                # A socket-only bootstrap server is ready, but TCP is not.
                raise offline_rehearsal.release.ReleaseError('not_ready')

        def restore(args, **kwargs):
            self.assertEqual(events, ['tcp_probe'] * 3)
            tail = args[args.index('pg_restore') :]
            self.assertEqual(tail[tail.index('--host') + 1], '127.0.0.1')
            self.assertEqual(tail[tail.index('--port') + 1], '5432')
            self.assertEqual(kwargs['stdin'].read(), b'original-dump')
            events.append('restore')

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'copy'
            source.write_bytes(b'original-dump')
            with (
                patch.object(offline_rehearsal.release, 'command', side_effect=command),
                patch.object(offline_rehearsal.subprocess, 'run', side_effect=restore),
                patch('time.sleep') as sleep,
            ):
                offline_rehearsal.restore_copy(plan, source, {})
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(source.read_bytes(), b'original-dump')
        self.assertEqual(events[-1], 'restore')
        self.assertIn(
            'SIXNINE_DATABASE_URL=postgresql+psycopg://postgres@/rehearsal?host=/var/run/postgresql',
            plan['migrate'],
        )

    def test_exhaustion_never_opens_dump_or_restores(self):
        source = Mock()
        with (
            patch.object(
                offline_rehearsal.release,
                'command',
                side_effect=offline_rehearsal.release.ReleaseError('not_ready'),
            ) as command,
            patch.object(offline_rehearsal.subprocess, 'run') as restore,
            patch('time.sleep'),
        ):
            with self.assertRaisesRegex(
                offline_rehearsal.release.ReleaseError, '^rehearsal_database_not_ready$'
            ):
                offline_rehearsal.restore_copy(self.plan(), source, {})
        self.assertEqual(command.call_count, 40)
        source.open.assert_not_called()
        restore.assert_not_called()

    def test_restore_failure_is_closed_and_never_retried(self):
        for error, code in (
            (
                offline_rehearsal.subprocess.CalledProcessError(
                    1, ['pg_restore'], stderr=b'private SQL'
                ),
                'rehearsal_restore_failed_retained',
            ),
            (
                offline_rehearsal.subprocess.TimeoutExpired(['pg_restore'], 600),
                'rehearsal_restore_timeout_retained',
            ),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'copy'
                source.write_bytes(b'original-dump')
                with (
                    patch.object(offline_rehearsal.release, 'command') as command,
                    patch.object(
                        offline_rehearsal.subprocess, 'run', side_effect=error
                    ) as restore,
                ):
                    with self.assertRaisesRegex(
                        offline_rehearsal.release.ReleaseError, '^' + code + '$'
                    ):
                        offline_rehearsal.restore_copy(self.plan(), source, {})
                self.assertEqual(command.call_count, 1)
                self.assertEqual(restore.call_count, 1)
                self.assertEqual(source.read_bytes(), b'original-dump')


class PaymentLiveRehearsalTests(unittest.TestCase):
    def plan(self):
        return offline_rehearsal.commands(
            COMMIT, 'postgres@sha256:' + 'f' * 64, 'd' * 32
        )

    def schema(self, revision):
        return {
            'matches': True,
            'recorded_revision': revision,
            'schema_fingerprint': ('5' if revision.endswith('0005') else '6') * 64,
        }

    def test_payment_commands_keep_the_copy_offline_and_use_immutable_images(self):
        for snapshot in (False, True):
            args = offline_rehearsal.payment_command(
                self.plan(), COMMIT, 'sha256:' + 'b' * 64, 'check', snapshot=snapshot
            )
            self.assertEqual(args[args.index('--network') + 1], 'none')
            self.assertEqual(args[args.index('--pull') + 1], 'never')
            self.assertIn('INKSEQ_STRIPE_MODE=off', args)
            self.assertIn('sha256:' + 'b' * 64, args)
            self.assertNotIn('sixnine-platform:' + COMMIT, args)
            self.assertNotIn('init-db', args)
            self.assertNotIn('--publish', args)
            self.assertFalse(any('/run/sixnine-secrets' in s for s in args))
            self.assertTrue(
                all('readonly' in s for s in args if s.startswith('type=volume'))
            )
        self.assertIn('SET TRANSACTION READ ONLY', offline_rehearsal.PAYMENT_ROWS)
        compile(offline_rehearsal.PAYMENT_ROWS, '<copy-row-digests>', 'exec')

    def exercise(self, *, changed_rows=False, reader_mismatch=False):
        before, after = self.schema('20261020_0005'), self.schema('20261021_0006')
        rows = {
            'tenant_id': 'sixnine',
            'charging_enabled': False,
            'tables': {
                'platform_payments': {'rows': 3, 'sha256': 'a' * 64},
                'platform_documents': {'rows': 5, 'sha256': 'b' * 64},
            },
        }
        readers = {'b' * 40: 'sha256:' + 'b' * 64, 'c' * 40: 'sha256:' + 'c' * 64}
        final = copy.deepcopy(rows)
        if changed_rows:
            final['tables']['platform_payments']['sha256'] = 'f' * 64
        checks = [before, after, after, {**after, 'matches': not reader_mismatch}]
        responses = [
            checks[0],
            rows,
            {'state': 'adopted_payment_live'},
            checks[1],
            {'state': 'already_adopted'},
            checks[2],
            checks[3],
            final,
        ]
        command = Mock(side_effect=[json.dumps(v) for v in responses])
        with patch.object(offline_rehearsal.release, 'command', command):
            result = offline_rehearsal.payment_copy(
                self.plan(), COMMIT, 'sha256:' + 'a' * 64, readers, {}
            )
        return result, command

    def test_exact_adoption_noop_and_both_readers_preserve_original_rows(self):
        result, command = self.exercise()
        self.assertEqual(result['after_revision'], '20261021_0006')
        self.assertEqual(result['repeat'], 'already_adopted')
        self.assertEqual(
            result['business_rows']['tables']['platform_payments']['rows'], 3
        )
        calls = [call.args[0] for call in command.call_args_list]
        self.assertEqual(
            calls[5][-5:],
            [
                'sha256:' + 'b' * 64,
                'python',
                '-m',
                'studio_platform.schema_baseline',
                'check',
            ],
        )
        self.assertEqual(
            calls[6][-5:],
            [
                'sha256:' + 'c' * 64,
                'python',
                '-m',
                'studio_platform.schema_baseline',
                'check',
            ],
        )
        self.assertEqual(calls[2][-1], '5' * 64)
        self.assertEqual(calls[4][-1], '6' * 64)

    def test_changed_business_rows_or_incompatible_reader_refuse(self):
        for kwargs, reason in (
            ({'changed_rows': True}, 'rehearsal_payment_original_rows_changed'),
            ({'reader_mismatch': True}, 'rehearsal_payment_schema_unrecognized'),
        ):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaisesRegex(
                    offline_rehearsal.release.ReleaseError, '^' + reason + '$'
                ),
            ):
                self.exercise(**kwargs)

    def test_already_006_copy_cannot_replace_the_original_upgrade_proof(self):
        with patch.object(
            offline_rehearsal.release,
            'command',
            return_value=json.dumps(self.schema('20261021_0006')),
        ) as command:
            with self.assertRaisesRegex(
                offline_rehearsal.release.ReleaseError,
                '^rehearsal_payment_requires_original_005_copy$',
            ):
                offline_rehearsal.payment_copy(
                    self.plan(),
                    COMMIT,
                    'sha256:' + 'a' * 64,
                    {'b' * 40: 'sha256:' + 'b' * 64, 'c' * 40: 'sha256:' + 'c' * 64},
                    {},
                )
        self.assertEqual(command.call_count, 1)

    @unittest.skipIf(
        os.name == 'nt', 'Unix protected metadata is exercised in Linux CI'
    )
    def test_failed_payment_copy_retains_intent_dump_and_exact_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'rehearsal/incoming/production.dump'
            source.parent.mkdir(parents=True, mode=0o700)
            source.parent.parent.chmod(0o700)
            source.write_bytes(b'original-private-copy')
            source.chmod(0o600)
            image = 'sha256:' + 'a' * 64
            calls = []
            regular = offline_rehearsal.release.regular

            def command(args, **kwargs):
                calls.append(args)
                return (
                    json.dumps([{'Id': image}])
                    if args[:2] == ['image', 'inspect']
                    else ''
                )

            with (
                # This orchestration fixture is owned by the test runner, not
                # root; file/permission guard behavior has separate tests.
                patch.object(offline_rehearsal.release, 'protected_directory'),
                patch.object(
                    offline_rehearsal.release,
                    'regular',
                    side_effect=lambda path, **kw: regular(
                        path, maximum=kw.get('maximum')
                    ),
                ),
                patch.object(
                    offline_rehearsal.deployment_target,
                    'host',
                    return_value={'name': 'staging'},
                ),
                patch.object(
                    offline_rehearsal.release, 'approved_manifest'
                ) as approved,
                patch.object(
                    offline_rehearsal.release,
                    'manifest',
                    side_effect=lambda _p, c: {'image': 'sixnine-platform:' + c},
                ),
                patch.object(
                    offline_rehearsal.release,
                    'validate_image_archive',
                    return_value=(image,),
                ),
                patch.object(
                    offline_rehearsal.release,
                    'deployment_environment',
                    return_value={
                        'SIXNINE_POSTGRES_IMAGE': 'postgres@sha256:' + 'f' * 64
                    },
                ),
                patch.object(offline_rehearsal.release, 'command', side_effect=command),
                patch.object(offline_rehearsal.release, 'sync_directory'),
                patch.object(offline_rehearsal, 'restore_copy') as restore,
                patch.object(
                    offline_rehearsal,
                    'payment_copy',
                    side_effect=offline_rehearsal.release.ReleaseError('copy_failed'),
                ),
                self.assertRaisesRegex(
                    offline_rehearsal.release.ReleaseError, '^copy_failed$'
                ),
            ):
                offline_rehearsal._run(
                    COMMIT, root=root, payment_readers=['b' * 40, 'c' * 40]
                )
            intent = json.loads(
                (root / ('rehearsal/payment-live-' + COMMIT + '.json')).read_text()
            )
            self.assertEqual(source.read_bytes(), b'original-private-copy')
            self.assertEqual(set(intent['payment_reader_images']), {'b' * 40, 'c' * 40})
            self.assertEqual(approved.call_count, 3)
            restore.assert_called_once()
            self.assertFalse(any(args[0] in {'rm', 'volume'} for args in calls))
            self.assertFalse(any('init-db' in args for args in calls))


if __name__ == '__main__':
    unittest.main()
