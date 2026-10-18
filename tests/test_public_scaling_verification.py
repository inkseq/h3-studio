"""Six-job real API contracts with synthetic queue completion; no cloud/GPU."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform.queue import TaskQueue
import test_public_generation_verification as generation_tests
from test_public_api_verification import load, verifier as public

single = generation_tests.verifier
with patch.dict(sys.modules, {'verify_public_api': public, 'verify_public_generation': single}):
    verifier = load('verify_public_scaling')


class PublicScalingTests(unittest.TestCase):
    def setUp(self):
        generation_tests.PublicGenerationVerificationTests.setUp(self)
        # Existing counters are preserved; this is only the synthetic local
        # test budget, never a cloud-rental authorization.
        self.repo.configure_budget('test-tenant:sixnine', tenant_id='sixnine', limit_microusd=4000000)
        self.repo.configure_budget('test-owner:sixnine:superdan', tenant_id='sixnine', owner_id='superdan',
            limit_microusd=4000000)

    def submit(self, state=None):
        state = verifier.initial_state() if state is None else state
        with verifier.authenticated(self.root, state, prepare=True) as (_, agent, headers, _):
            return verifier.submit(self.root, state, agent, headers)

    def receipt(self):
        return json.loads((self.root/'receipt.json').read_text(encoding='utf-8'))

    def assert_keys_revoked_and_no_secrets(self):
        raw = (self.root/'receipt.json').read_text(encoding='utf-8')
        for value in [*self.passwords.values(), 'Bearer ', 'sxp_']:
            self.assertNotIn(value, raw)
        keys = self.app.state.auth.list_keys('superdan')
        self.assertTrue(keys and all(k['revoked_at'] is not None for k in keys))
        self.assertTrue(all(not k['all_projects'] and k['project_ids'] == [verifier.PROJECT] for k in keys))

    def test_default_and_nonroot_do_not_authenticate(self):
        with patch.object(verifier, 'authenticated', side_effect=AssertionError('must not authenticate')):
            self.assertEqual(verifier.run(''), {'state': 'disabled', 'generation_submitted': False})
            with patch.object(verifier.os, 'geteuid', return_value=1000, create=True):
                with self.assertRaisesRegex(RuntimeError, 'Operator root'):
                    verifier.run('submit-authorized', self.root)

    def test_docker_decode_limits_scaling_paths_and_keeps_isolated_image(self):
        commit, image_id = 'a'*40, 'sha256:'+'b'*64
        calls = []
        def command(arguments, **options):
            calls.append((arguments, options))
            return (image_id+'\n').encode() if arguments[0] == 'image' else b''
        release = SimpleNamespace(ROOT=Path('/srv/sixnine'), DOCKER='/usr/bin/docker',
            regular=lambda *a, **k: None, approved_manifest=lambda *a: None,
            validate_image_archive=lambda *a: (image_id,), command=command)
        def read_text(path, **kwargs):
            return json.dumps({'current': commit, 'status': 'app_ready'} if path.name == 'release-state.json'
                else {'commit': commit, 'image': 'sixnine-platform:'+commit, 'image_id': image_id})
        with patch.object(single, 'trusted_release', return_value=release), patch.object(single, 'check_file'), \
                patch.object(Path, 'read_text', read_text):
            for shot in verifier.SHOTS:
                media = verifier.ROOT/shot/'generated.mp4'
                single.docker_decode(media, 'video')
                argv = calls[-1][0]
                self.assertEqual(argv[argv.index('--network')+1], 'none')
                self.assertEqual(argv[argv.index('--mount')+1], 'type=bind,src='+str(media)+',dst=/input.mp4,readonly')
                self.assertIn(image_id, argv)
            before = len(calls)
            for path in [verifier.ROOT/'shot-7'/'generated.mp4', verifier.ROOT/'generated.mp4',
                    verifier.ROOT/'shot-1'/'other.mp4', Path('/tmp/shot-1/generated.mp4')]:
                with self.assertRaises(AssertionError):
                    single.docker_decode(path, 'video')
            self.assertEqual(len(calls), before)

    def test_six_separate_plans_jobs_and_scoped_revoked_keys(self):
        state = self.submit()
        self.assertEqual(self.posts, 6)
        self.assertEqual(len({x['job_id'] for x in state['shots']}), 6)
        self.assertEqual(len({x['plan_id'] for x in state['shots']}), 6)
        self.assertEqual(self.repo.get_budget('test-owner:sixnine:superdan')['reserved_microusd'], 3000000)
        self.assertEqual(len(self.repo.list_jobs_for_owner('sixnine', 'superdan', project_id=verifier.PROJECT)), 6)
        self.assert_keys_revoked_and_no_secrets()
        self.submit(self.receipt())
        self.assertEqual(self.posts, 6)
        self.assert_keys_revoked_and_no_secrets()

    def test_prepare_creates_story_without_generation_or_health_read(self):
        original = public.request
        calls = []
        def guarded(opener, method, path, *args, **kwargs):
            calls.append((method, path))
            self.assertNotEqual(path, '/healthz')
            self.assertNotIn(path, ('/v1/generation-plans', '/v1/jobs'))
            return original(opener, method, path, *args, **kwargs)
        with patch.object(public, 'request', side_effect=guarded):
            result = verifier.prepare(self.root, verifier.initial_state())
        self.assertEqual(result['state'], 'prepared')
        self.assertFalse(result['generation_submitted'])
        self.assertEqual(result['submitted'], 0)
        self.assertEqual(self.posts, 0)
        self.assertEqual(len(self.repo.list_jobs_for_owner('sixnine', 'superdan', project_id=verifier.PROJECT)), 0)
        self.assertIn(('POST', '/v1/projects'), calls)
        self.assert_keys_revoked_and_no_secrets()

    def test_lost_paid_response_new_pat_recovers_original_plan_then_completes_six(self):
        self.fail_after_post = RuntimeError('synthetic process interruption')
        with self.assertRaises(RuntimeError):
            self.submit()
        original = self.receipt()
        self.assertTrue(original['shots'][0]['submission_started_at'])
        self.assertNotIn('job_id', original['shots'][0])
        state = self.submit(original)
        self.assertEqual(self.posts, 6)
        self.assertEqual(state['shots'][0]['recovery'], 'recovered_by_original_plan')
        self.assertEqual(len({x['job_id'] for x in state['shots']}), 6)
        self.assert_keys_revoked_and_no_secrets()

    def test_no_matching_original_plan_never_replays_or_advances_to_new_paid_jobs(self):
        self.block_before_post = True
        with self.assertRaises(RuntimeError):
            self.submit()
        self.block_before_post = False
        state = self.submit(self.receipt())
        self.assertEqual(self.posts, 1)
        self.assertEqual(state['shots'][0]['status'], 'submission_unknown')
        self.assertFalse(any(x.get('job_id') for x in state['shots']))
        self.assertEqual(self.repo.list_jobs_for_owner('sixnine', 'superdan'), [])

    def test_same_live_pat_timeout_retry_is_idempotent(self):
        self.fail_after_post = TimeoutError('synthetic transport loss')
        state = self.submit()
        self.assertEqual(self.posts, 7)
        self.assertEqual(len({x['job_id'] for x in state['shots']}), 6)
        self.assert_keys_revoked_and_no_secrets()

    def test_plan_creation_intent_survives_lost_response_without_generation(self):
        original = public.request
        plan_posts = []
        def interrupted(opener, method, path, *args, **kwargs):
            result = original(opener, method, path, *args, **kwargs)
            if method == 'POST' and path == '/v1/generation-plans':
                plan_posts.append(path)
                raise RuntimeError('synthetic lost plan response')
            return result
        with patch.object(public, 'request', side_effect=interrupted), self.assertRaises(RuntimeError):
            self.submit()
        state = self.receipt()
        self.assertTrue(state['shots'][0]['plan_creation_started_at'])
        self.assertNotIn('plan_id', state['shots'][0])
        state = self.submit(state)
        self.assertEqual(state['shots'][0]['status'], 'plan_creation_unknown_no_resubmit')
        self.assertEqual((len(plan_posts), self.posts), (1, 0))
        self.assert_keys_revoked_and_no_secrets()

    def test_lost_key_response_is_recovered_and_revoked(self):
        self.fail_after_key = True
        with self.assertRaises(RuntimeError):
            self.submit()
        self.assertEqual(self.posts, 0)
        self.assert_keys_revoked_and_no_secrets()

    def test_ambiguous_original_plan_match_never_resubmits(self):
        state = self.submit()
        state['shots'][0].pop('job_id')
        original = public.request
        def duplicated(opener, method, path, *args, **kwargs):
            result = original(opener, method, path, *args, **kwargs)
            if path.startswith('/v1/jobs?'):
                result['jobs'] *= 2
            return result
        with patch.object(public, 'request', side_effect=duplicated):
            state = self.submit(state)
        self.assertEqual(state['shots'][0]['status'], 'submission_unknown')
        self.assertEqual(self.posts, 6)

    def test_cold_waiting_capacity_is_valid_job_status(self):
        original = public.request
        def cold(opener, method, path, *args, **kwargs):
            result = original(opener, method, path, *args, **kwargs)
            if method == 'POST' and path == '/v1/jobs':
                result['status'] = 'waiting_capacity'
            return result
        with patch.object(public, 'request', side_effect=cold):
            state = self.submit()
        self.assertTrue(all(x['status'] == 'waiting_capacity' for x in state['shots']))
        self.assertEqual(self.posts, 6)

    def test_actual_zero_worker_approval_admits_six_waiters_without_provider(self):
        from sqlalchemy import select
        from studio_platform.autoscale import ScalePolicy
        from studio_platform.repository import Scope, capacity_waiters, request_hash
        from studio_platform.scaler import LaunchSpec
        policy = json.loads((self.root/'policy.json').read_text())
        self.control.drain('synthetic-worker')
        self.control.retire('synthetic-worker', upstream_idle_confirmed=True)
        self.repo.configure_pool('synthetic-pool', max_instances=1, max_physical_gpus=1)
        self.repo.configure_budget('synthetic-cold-capacity', tenant_id='sixnine',
            owner_id='superdan', project_id=verifier.PROJECT, limit_microusd=1000000)
        clock = self.repo.clock()
        self.repo.approve_capacity('synthetic-public-six-cold', tenant_id='sixnine',
            pool=policy['pool'], model_id=policy['model_id'], configuration_id=policy['configuration_id'],
            recipe_ids=policy['recipe_ids'], policy_hash=request_hash(policy),
            qualification_evidence_id=policy['qualification']['evidence_id'],
            qualification_expires_at=policy['qualification']['expires_at'],
            quote_expires_at=policy['reservation']['expires_at'], expires_at=clock+1800,
            launch=LaunchSpec('test-only', policy['configuration_id'], policy['model_id']),
            scale_policy=ScalePolicy(dry_run=False, max_instances=1, max_physical_gpus=1,
                cold_start_s=30, approved_remaining_microusd=1000000,
                instance_reservation_microusd=200000, hard_deadline=clock+2400),
            budget_scope=Scope('sixnine', 'superdan', verifier.PROJECT),
            budget_account_ids=['synthetic-cold-capacity'], enabled=True)
        original, plans = public.request, []
        def observe(opener, method, path, *args, **kwargs):
            result = original(opener, method, path, *args, **kwargs)
            if method == 'POST' and path == '/v1/generation-plans':
                plans.append(result)
            return result
        with patch.object(public, 'request', side_effect=observe):
            state = self.submit()
        self.assertEqual(len(plans), 6)
        self.assertTrue(all(p['status'] == 'ready' and not p['blockers']
            and p['execution']['admission_state'] == 'waiting_capacity' for p in plans))
        self.assertTrue(all(item['status'] == 'waiting_capacity' for item in state['shots']))
        with self.repo.engine.connect() as conn:
            waiters = list(conn.execute(select(capacity_waiters)).mappings())
        self.assertEqual({w['job_id'] for w in waiters}, {s['job_id'] for s in state['shots']})
        self.assertTrue(all(w['state'] == 'waiting_capacity' and w['intent_id'] is None for w in waiters))
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.assertEqual(self.repo.get_budget('synthetic-cold-capacity')['reserved_microusd'], 0)
        self.assertEqual(self.repo.get_budget('test-owner:sixnine:superdan')['reserved_microusd'], 3000000)
        self.submit(self.receipt())
        self.assertEqual(self.posts, 6)
        self.assert_keys_revoked_and_no_secrets()

    def test_changed_story_prompt_fails_before_any_paid_post(self):
        state = verifier.initial_state()
        with verifier.authenticated(self.root, state, prepare=True) as (browser, _, _, _):
            project = public.request(browser, 'GET', '/v1/projects/'+verifier.PROJECT)
            public.request(browser, 'POST', '/v1/projects/'+verifier.PROJECT+'/actions',
                {'expected_version': project['version'], 'actions': [{'op': 'entity.update', 'entity_id': 'shot-1',
                    'patch': {'data': {'seconds': 5, 'prompt': 'User edited this shot'}}}]})
        with self.assertRaises(AssertionError):
            self.submit(state)
        self.assertEqual(self.posts, 0)

    def finish_six_synthetic(self):
        video, audio = self.root/'fixture.mp4', self.root/'fixture.flac'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=navy:s=64x64:r=24:d=5', '-f', 'lavfi', '-i', 'anullsrc=r=32000:cl=stereo',
            '-t', '5', '-c:v', 'libx264', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(video)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'anullsrc=r=32000:cl=stereo', '-t', '5', str(audio)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        queue = TaskQueue(self.repo)
        for number in range(6):
            claim = self.control.claim('synthetic-worker', 'synthetic-pool')
            self.assertIsNotNone(claim)
            lease = claim.lease
            queue.begin_submission(lease)
            queue.record_submitted(lease, 'synthetic-upstream-'+str(number))
            queue.begin_collection(lease)
            specs = []
            for kind, source, mime in [('video', video, 'video/mp4'), ('audio', audio, 'audio/flac')]:
                data = source.read_bytes()
                key = 'owners/superdan/assets/synthetic-'+str(number)+'-'+kind+'/output'+source.suffix
                self.app.state.storage.put(key, io.BytesIO(data), content_type=mime)
                specs.append(dict(kind=kind, object_key=key, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                    validated=True, content_type=mime, duration_s=5, **({'width': 64, 'height': 64, 'fps': 24, 'has_audio': True}
                    if kind == 'video' else {})))
            queue.complete(lease, specs, actual_cost_microusd=0)
            self.control.observe('synthetic-worker', claim.job['id'])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for bounded full decode')
    def test_all_six_download_adoption_tracks_browser_and_cross_owner_isolation(self):
        state = self.submit()
        self.finish_six_synthetic()
        with verifier.authenticated(self.root, state) as (browser, agent, headers, outsider):
            result = verifier.collect(self.root, state, browser, agent, headers, outsider, decoder=single.local_decode)
            self.assertTrue(all(x['adopted'] and x['browser_api_same_document'] and x['cross_owner_denied'] for x in result['shots']))
            self.assertTrue(all(len(x['outputs']) == 2 and all(o['full_decode'] for o in x['outputs']) for x in result['shots']))
            project = public.request(browser, 'GET', '/v1/projects/'+verifier.PROJECT)
            self.assertEqual(len(project['project']['journey']['soundTracks']['chapter']), 6)
            self.assertEqual(project['project']['journey']['sound']['mode'], 'mixed')
            version = project['version']
            with patch.object(single, 'local_decode', side_effect=AssertionError('same bytes must not decode again')):
                verifier.collect(self.root, result, browser, agent, headers, outsider, decoder=single.local_decode)
            self.assertEqual(public.request(browser, 'GET', '/v1/projects/'+verifier.PROJECT)['version'], version)
            (self.root/'shot-1'/'generated.mp4').write_bytes(b'changed bytes')
            with self.assertRaises(AssertionError):
                verifier.collect(self.root, result, browser, agent, headers, outsider, decoder=single.local_decode)
        self.assert_keys_revoked_and_no_secrets()


if __name__ == '__main__':
    unittest.main()
