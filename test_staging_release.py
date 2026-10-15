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
from tools import ci_changes, publish_staging_candidate as publisher, deploy_aws_release as runner
from test_migration_secrets_aws import initialized, r2_storage

DIRECTORY = Path(__file__).resolve().parent/'deploy/platform'
sys.path.insert(0, str(DIRECTORY))
import environment_release as host
import offline_rehearsal

COMMIT = 'a'*40
INSTANCE = 'i-0123456789abcdef0'
COMMAND = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def publication(kind='platform'):
    return {'version': 1, 'commit': COMMIT, 'kind': kind, 'run_id': 42, 'run_attempt': 1,
        'manifests': {name: candidate.digest(candidate.encode({'commit': COMMIT, 'name': name}))
            for name in (('platform', 'migration', 'frontend') if kind == 'platform' else ('frontend',))}}


def proof(value):
    return {'version': 1, 'state': 'accepted', 'commit': COMMIT,
        'candidate_sha256': candidate.digest(candidate.encode(value)), 'staging_instance_id': INSTANCE,
        'checks': {name: 'passed' for name in ('browser_agent', 'isolation', 'migration_rehearsal')},
        'evidence': 'https://github.com/inkseq/h3-studio/issues/195#issuecomment-123'}


class TargetTests(unittest.TestCase):
    def test_stage_site_is_sealed_and_missing_record_never_becomes_production(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaisesRegex(ValueError, 'record_required'):
                target.site_environment(root, {'INKSEQ_DEPLOYMENT_TARGET': 'staging'})
            with patch.object(target, 'host', return_value=target.profile('staging')):
                value = target.site_environment(root, {'INKSEQ_DEPLOYMENT_TARGET': 'staging'})
                self.assertEqual(value['INKSEQ_PUBLIC_ORIGIN'], 'https://staging.inkseq.com')
                self.assertEqual(value['INKSEQ_PUBLIC_ORIGIN_ALIASES'], '')
                self.assertEqual(value['INKSEQ_CADDY_PROFILE'], 'Caddyfile.staging')
                with self.assertRaisesRegex(ValueError, 'site_mismatch'):
                    target.site_environment(root, {'INKSEQ_DEPLOYMENT_TARGET': 'production'})

    def test_record_rejects_links_extra_fields_and_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); path = root/'deployment-target.json'
            proxy = types.SimpleNamespace(name='nt', O_RDONLY=os.O_RDONLY, O_NOFOLLOW=getattr(os, 'O_NOFOLLOW', 0),
                open=os.open, fstat=os.fstat, read=os.read, close=os.close)
            with patch.object(target, 'os', proxy):
                path.write_text(json.dumps({'version': 1, 'target': 'staging', 'instance_id': INSTANCE}))
                self.assertEqual(target.host(root)['name'], 'staging')
                for raw in ('{"version":1,"target":"staging","target":"production","instance_id":"'+INSTANCE+'"}',
                    json.dumps({'version': 1, 'target': 'staging', 'instance_id': INSTANCE, 'secret_root': '/sixnine/platform'})):
                    path.write_text(raw)
                    with self.assertRaises(ValueError): target.host(root)

    def test_stage_credentials_are_exact_and_production_cannot_fallback(self):
        value = initialized()
        value['providers']['vastai']['profile_id'] = target.profile('staging')['vast_profile']
        value['grafana_cloud'].update(enabled=False, endpoint='', authorization='', environment='staging')
        value['r2_storage'] = dict(r2_storage(), profile=target.profile('staging')['r2_profile'], bucket='sixnine-staging-media')
        secrets.validate(value, target='staging')
        output = secrets.outputs(value, target='staging')
        self.assertEqual(json.loads(output['dstack-server-config.yml'][0])['projects'][0]['name'], 'sixnine-staging')
        for mutate in (
            lambda v: v['r2_storage'].update(profile='cloudflare-r2--rig-root'),
            lambda v: v['r2_storage'].update(bucket='sixnine-media'),
            lambda v: v['providers']['vastai'].update(profile_id='vastai--sixnine-dstack-20261011'),
            lambda v: v['grafana_cloud'].update(enabled=True),
            lambda v: v['grafana_cloud'].update(authorization='Basic copied-production-config'),
        ):
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): secrets.validate(changed, target='staging')
        with self.assertRaises(ValueError): secrets.validate(value)
        api = Mock(); api.get_secret_value.return_value = {'SecretString': json.dumps(value), 'VersionId': 'same'}
        secrets._managed(api, target='staging')
        api.get_secret_value.assert_called_once_with(SecretId='/sixnine/staging/migration')

    def test_stage_proxy_has_no_production_domain_or_redirect(self):
        text = (DIRECTORY/'Caddyfile.staging').read_text()
        self.assertIn('staging.inkseq.com {', text)
        self.assertNotIn('sixnine.art', text)
        self.assertNotIn('redir ', text)
        self.assertNotIn('INKSEQ_LEGACY_REDIRECT', text)


class CandidateTests(unittest.TestCase):
    def test_acceptance_binds_complete_tuple_and_all_required_evidence(self):
        value = publication(); accepted = proof(value)
        self.assertEqual(candidate.acceptance(accepted, value), accepted)
        for field in ('platform', 'migration', 'frontend'):
            changed = copy.deepcopy(value); changed['manifests'][field] = 'f'*64
            with self.assertRaises(ValueError): candidate.acceptance(accepted, changed)
        changed = copy.deepcopy(accepted); changed['checks']['migration_rehearsal'] = 'not-tested'
        with self.assertRaises(ValueError): candidate.acceptance(changed, value)

    def test_stage_then_production_call_existing_deployers_with_identical_bytes(self):
        value = publication(); api = Mock()
        def get(**kwargs):
            key = kwargs['Key']
            if key.startswith('staging-candidates/'):
                raw = candidate.encode(value)
            else:
                name = 'migration' if '/migration/' in key else 'frontend' if '/frontend/' in key else 'platform'
                raw = candidate.encode({'commit': COMMIT, 'name': name})
            return {'ContentLength': len(raw), 'Body': io.BytesIO(raw)}
        api.get_object.side_effect = get
        fake_lock = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=Mock())
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch.dict(sys.modules, {'fcntl': fake_lock}), \
                    patch.object(host.deployment_target, 'host', return_value=dict(target.profile('staging'), instance_id=INSTANCE)), \
                    patch.object(host.frontend_release, 'protected_directory', side_effect=lambda p, **kw: p.mkdir(exist_ok=True)), \
                    patch.object(host.release, 'regular'), \
                    patch.object(host.deploy_approved, 'deploy') as platform, patch.object(host.deploy_frontend, 'deploy') as frontend:
                first = host.run('staging', COMMIT, root=root, api=api)
                platform.assert_called_once_with(COMMIT, root=root)
                frontend.assert_called_once_with(COMMIT, root=root, api=api)
                accepted = proof(value)
                (root/'promotion-candidates').mkdir(); (root/'approved-promotions').mkdir()
                receipt = root/'promotion-candidates'/(COMMIT+'.json'); receipt.write_bytes(candidate.encode(accepted))
                approval = root/'approved-promotions'/(COMMIT+'.sha256'); approval.write_text(host.release.checksum(receipt))
                with patch.object(host.deployment_target, 'host', return_value=target.profile()):
                    promoted = host.run('production', COMMIT, root=root, api=api)
                self.assertEqual(first['candidate_sha256'], promoted['candidate_sha256'])
                self.assertEqual(platform.call_count, 2)
                self.assertEqual(frontend.call_count, 2)
                # Mutating the accepted artifact tuple cannot reuse the approval.
                accepted['evidence'] = 'https://github.com/inkseq/h3-studio/issues/195#issuecomment-999'
                receipt.write_bytes(candidate.encode(accepted))
                with patch.object(host.deployment_target, 'host', return_value=target.profile()), self.assertRaises(host.release.ReleaseError):
                    host.run('production', COMMIT, root=root, api=api)
                self.assertEqual(platform.call_count, 2)

    def test_wrong_host_refuses_before_any_object_read_or_deployment(self):
        api = Mock()
        with patch.object(host.deployment_target, 'host', return_value=target.profile()), self.assertRaises(host.release.ReleaseError):
            host.run('staging', COMMIT, api=api)
        api.get_object.assert_not_called()

    def test_new_ssm_targets_pin_version_one_and_preserve_old_production_version(self):
        api = Mock(); api.send_command.return_value = {'Command': {'CommandId': COMMAND}}
        api.get_command_invocation.return_value = {'Status': 'Success', 'DocumentName': 'Inkseq-DeployStagingRelease',
            'DocumentVersion': '1', 'StandardOutputContent': json.dumps({'state': 'staging_release_healthy',
                'commit': COMMIT, 'candidate_sha256': 'b'*64})}
        with patch.object(runner, 'STAGING_INSTANCE', INSTANCE), patch('sys.stdout', io.StringIO()):
            runner.deploy(COMMIT, target='staging', api=api)
        self.assertEqual(api.send_command.call_args.kwargs['InstanceIds'], [INSTANCE])
        self.assertEqual(api.send_command.call_args.kwargs['DocumentVersion'], '1')
        with patch.object(runner, 'STAGING_INSTANCE', None), self.assertRaises(ValueError):
            runner.deploy(COMMIT, target='staging', api=Mock())


class MainAndRehearsalTests(unittest.TestCase):
    def test_main_automatically_targets_stage_pr_and_docs_cannot_deploy(self):
        for path, action in (('studio_platform/auth.py', 'stage-platform'), ('yingxu/src/App.jsx', 'stage-frontend'), ('README.md', 'check')):
            with patch.object(ci_changes, 'git_changes', return_value=[('M', path)]):
                result = ci_changes.plan('push', {'before': 'b'*40}, COMMIT, 'refs/heads/main', Path('.'))
                self.assertEqual(result['action'], action)
                pr = ci_changes.plan('pull_request', {'pull_request': {'base': {'sha': 'b'*40}}}, COMMIT, 'refs/pull/9/merge', Path('.'))
                self.assertEqual(pr['action'], 'check')

    def test_rehearsal_copy_cannot_reach_network_credentials_or_public_services(self):
        plan = offline_rehearsal.commands(COMMIT, 'postgres@sha256:'+'f'*64, 'd'*32)
        for name in ('database', 'migrate'):
            args = plan[name]
            self.assertEqual(args[args.index('--network')+1], 'none')
            self.assertNotIn('--publish', args)
            self.assertNotIn('--privileged', args)
            self.assertFalse(any('/srv/sixnine' in value or '/run/sixnine-secrets' in value for value in args))
        self.assertEqual(plan['migrate'][-5:], ['sixnine-platform:'+COMMIT, 'python', '-m', 'studio_platform.manage', 'init-db'])
        self.assertNotIn('controller', plan['migrate'])
        self.assertIn('--no-owner', plan['restore'])


if __name__ == '__main__':
    unittest.main()
