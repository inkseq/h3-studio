"""Public acceptance contract through TestClient; no GPU, provider or AWS call."""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from studio_platform.api import create_app
from studio_platform.capabilities import MODEL
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.queue import TaskQueue
from studio_platform.settings import Settings
from test_platform_execution_policy import policy
from test_public_api_verification import load, runtime, verifier as public

with patch.dict(sys.modules, {'verify_public_api': public}):
    verifier = load('verify_public_generation')


class PublicGenerationVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        policy_path = self.root/'policy.json'
        import time
        policy_path.write_text(json.dumps(policy(time.time())), encoding='utf-8')
        policy_path.chmod(0o600)
        self.app = create_app(Settings(data_dir=self.root/'data', public_origin=public.ORIGIN,
            auth_mode='password', generation_enabled=True, execution_backend='comfy-worker',
            execution_policy_file=policy_path))
        self.repo = self.app.state.repository
        self.addCleanup(self.repo.close)
        self.passwords = {name: secrets.token_urlsafe(30) for name in ('superdan', 'supervan')}
        for name, password in self.passwords.items():
            self.app.state.auth.set_password(name, password)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.control = WorkerControl(self.repo)
        self.control.register(WorkerSpec('synthetic-worker', 'synthetic-pool', 'synthetic-provider',
            'synthetic-instance', ('synthetic-gpu',), (verifier.RECIPE,), MODEL, 'synthetic-config'))
        self.control.mark_ready('synthetic-worker', upstream_idle_confirmed=True)
        self.repo.configure_budget('test-tenant:sixnine', tenant_id='sixnine', limit_microusd=1000000)
        self.repo.configure_budget('test-owner:sixnine:superdan', tenant_id='sixnine', owner_id='superdan',
            limit_microusd=1000000)
        self.posts, self.fail_after_post, self.block_before_post = 0, None, False
        self.fail_after_key = False
        test = self

        class Adapter:
            def __init__(self):
                self.session = TestClient(test.app, base_url=public.ORIGIN)
                test.addCleanup(self.session.close)

            def open(self, request, timeout):
                job_post = request.get_method() == 'POST' and request.full_url == public.ORIGIN+'/v1/jobs'
                if job_post:
                    test.posts += 1
                    if test.block_before_post:
                        raise RuntimeError('synthetic process interruption before request')
                response = self.session.request(request.get_method(), request.full_url,
                    headers=dict(request.header_items()), content=request.data, follow_redirects=False)
                if test.fail_after_key and request.get_method() == 'POST' and request.full_url.endswith('/v1/api-keys'):
                    test.fail_after_key = False
                    raise RuntimeError('synthetic lost key response')
                if job_post and test.fail_after_post:
                    error, test.fail_after_post = test.fail_after_post, None
                    raise error
                result = io.BytesIO(response.content)
                result.code = response.status_code
                return result

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(public, 'client', Adapter))
        stack.enter_context(patch.object(runtime, 'client', return_value=object()))
        stack.enter_context(patch.object(runtime, 'value', side_effect=lambda *_: dict(self.passwords)))

    def submit(self, state=None):
        state = {} if state is None else state
        with verifier.authenticated(prepare=not state) as (_, agent, headers):
            return verifier.submit(self.root, state, agent, headers)

    def receipt(self):
        return json.loads((self.root/'receipt.json').read_text(encoding='utf-8'))

    def assert_one_job_and_no_secret(self, state):
        rows = self.repo.list_jobs_for_owner('sixnine', 'superdan', project_id=verifier.PROJECT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], state['job_id'])
        self.assertTrue(rows[0]['actor_id'].startswith('key:'))
        self.assertEqual(rows[0]['plan_id'], state['plan_id'])
        self.assertEqual(self.repo.get_budget('test-owner:sixnine:superdan')['reserved_microusd'], 500000)
        text = (self.root/'receipt.json').read_text(encoding='utf-8')
        for value in [*self.passwords.values(), 'Bearer ', 'sxp_']:
            self.assertNotIn(value, text)
        keys = self.app.state.auth.list_keys('superdan')
        self.assertTrue(keys and all(k['revoked_at'] is not None for k in keys))
        self.assertTrue(all(not k['all_projects'] and k['project_ids'] == [verifier.PROJECT] for k in keys))

    def test_disabled_and_nonroot_never_authenticate(self):
        with patch.object(verifier, 'authenticated', side_effect=AssertionError('must not authenticate')):
            self.assertEqual(verifier.run(''), {'state': 'disabled', 'generation_submitted': False})
            with patch.object(verifier.os, 'geteuid', return_value=1000, create=True):
                with self.assertRaisesRegex(RuntimeError, 'Operator root required'):
                    verifier.run('submit-authorized', self.root)

    def test_same_pat_transport_timeout_replays_original_plan_without_duplicate(self):
        self.fail_after_post = TimeoutError('synthetic dropped response')
        state = self.submit()
        self.assertEqual(self.posts, 2)
        self.assert_one_job_and_no_secret(state)

    def test_lost_response_new_pat_recovers_via_get_without_another_post(self):
        self.fail_after_post = RuntimeError('synthetic process interruption after commit')
        with self.assertRaises(RuntimeError):
            self.submit()
        original = self.receipt()
        self.assertTrue(original['submission_started'])
        self.assertNotIn('job_id', original)
        state = self.submit(original)
        self.assertEqual(self.posts, 1)
        self.assertEqual(state['recovery'], 'recovered_by_original_plan')
        self.assert_one_job_and_no_secret(state)

    def test_unknown_uncommitted_post_never_retries_with_replacement_pat(self):
        self.block_before_post = True
        with self.assertRaises(RuntimeError):
            self.submit()
        self.block_before_post = False
        state = self.submit(self.receipt())
        self.assertEqual(state['status'], 'submission_unknown')
        self.assertNotIn('job_id', state)
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.repo.list_jobs_for_owner('sixnine', 'superdan'), [])

    def test_lost_key_creation_response_still_revokes_that_temporary_key(self):
        self.fail_after_key = True
        with self.assertRaisesRegex(RuntimeError, 'synthetic lost key response'):
            self.submit()
        keys = self.app.state.auth.list_keys('superdan')
        self.assertEqual(len(keys), 1)
        self.assertIsNotNone(keys[0]['revoked_at'])
        self.assertEqual(self.posts, 0)

    def test_ambiguous_multiple_matching_jobs_refuse_recovery(self):
        state = self.submit()
        state.pop('job_id')
        original_request = public.request
        def duplicate_list(opener, method, path, *args, **kwargs):
            result = original_request(opener, method, path, *args, **kwargs)
            if path.startswith('/v1/jobs?'):
                result['jobs'] *= 2
            return result
        with patch.object(public, 'request', side_effect=duplicate_list):
            state = self.submit(state)
        self.assertEqual(state['status'], 'submission_unknown')
        self.assertNotIn('job_id', state)
        self.assertEqual(self.posts, 1)

    def test_artifact_foreign_url_and_hash_mismatch_never_decode(self):
        content = b'synthetic bytes'
        artifact = dict(id='output', kind='video', mime='video/mp4', size_bytes=len(content),
            sha256='0'*64, content_url='https://untrusted.invalid/output')
        with patch.object(verifier.subprocess, 'run', side_effect=AssertionError('must not decode')) as decode:
            with self.assertRaises(AssertionError):
                verifier.download(self.root, None, {}, artifact)
            artifact['content_url'] = '/v1/artifacts/output/content'
            (self.root/'generated.mp4').write_bytes(content)
            with self.assertRaises(AssertionError):
                verifier.download(self.root, None, {}, artifact)
            decode.assert_not_called()

    def finish_synthetic(self):
        """Only synthesize worker completion; exercise real API/storage/FFmpeg."""
        video, audio = self.root/'fixture.mp4', self.root/'fixture.flac'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=black:s=64x64:r=24:d=5', '-f', 'lavfi', '-i', 'anullsrc=r=24000:cl=mono',
            '-t', '5', '-c:v', 'libx264', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(video)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'anullsrc=r=24000:cl=mono', '-t', '5', str(audio)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        queue = TaskQueue(self.repo)
        claim = self.control.claim('synthetic-worker', 'synthetic-pool')
        self.assertIsNotNone(claim)
        lease = claim.lease
        queue.begin_submission(lease)
        queue.record_submitted(lease, 'synthetic-upstream-no-gpu')
        queue.begin_collection(lease)
        specs = []
        for kind, source, mime in [('video', video, 'video/mp4'), ('audio', audio, 'audio/flac')]:
            data = source.read_bytes()
            key = 'owners/superdan/assets/synthetic-'+kind+'/output'+source.suffix
            self.app.state.storage.put(key, io.BytesIO(data), content_type=mime)
            specs.append(dict(kind=kind, object_key=key, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                validated=True, content_type=mime, duration_s=5, **({'width': 64, 'height': 64, 'fps': 24, 'has_audio': True}
                if kind == 'video' else {})))
        queue.complete(lease, specs, actual_cost_microusd=0)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for local full decode contract')
    def test_public_artifacts_full_decode_adoption_and_browser_document(self):
        state = self.submit()
        self.finish_synthetic()
        with verifier.authenticated() as (browser, agent, headers):
            result = verifier.collect(self.root, state, browser, agent, headers, decoder=verifier.local_decode)
            self.assertTrue(result['adopted'] and result['browser_api_same_document'])
            self.assertEqual({o['kind'] for o in result['outputs']}, {'video', 'audio'})
            self.assertTrue(all(o['full_decode'] for o in result['outputs']))
            version = result['project_version']
            repeated = verifier.collect(self.root, result, browser, agent, headers, decoder=verifier.local_decode)
            self.assertEqual(repeated['project_version'], version)
            project = public.request(agent, 'GET', '/v1/projects/'+verifier.PROJECT, headers=headers)
            public.request(agent, 'POST', '/v1/projects/'+verifier.PROJECT+'/actions', {
                'expected_version': project['version'], 'actions': [
                    {'op': 'sound.set', 'chapter_id': 'chapter', 'mode': 'mixed', 'tracks': []}]},
                {**headers, 'Idempotency-Key': 'synthetic-later-user-edit'})
            with self.assertRaises(AssertionError):
                verifier.collect(self.root, repeated, browser, agent, headers, decoder=verifier.local_decode)

    def test_corrupt_media_never_passes_decode_even_with_matching_hash(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg required')
        content = b'not a media file'
        (self.root/'generated.mp4').write_bytes(content)
        artifact = dict(id='invalid-media', kind='video', mime='video/mp4', size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(), content_url='/v1/artifacts/invalid-media/content')
        with self.assertRaises(AssertionError):
            verifier.download(self.root, None, {}, artifact, decoder=verifier.local_decode)

    def test_docker_decoder_uses_only_approved_immutable_image_and_media_bind(self):
        commit, image_id = 'a'*40, 'sha256:'+'b'*64
        release_root = Path('/srv/sixnine')
        media = verifier.ROOT/'generated.mp4'
        calls, approvals = [], []
        def command(arguments, **options):
            calls.append((arguments, options))
            return (image_id+'\n').encode() if arguments[0] == 'image' else b''
        release = SimpleNamespace(ROOT=release_root, DOCKER='/usr/bin/docker', regular=lambda *a, **k: None,
            approved_manifest=lambda *args: approvals.append(args), command=command,
            validate_image_archive=lambda *args: (image_id,))
        def read_text(path, **kwargs):
            if path.name == 'release-state.json':
                return json.dumps({'current': commit, 'status': 'app_ready'})
            return json.dumps({'commit': commit, 'image': 'sixnine-platform:'+commit, 'image_id': image_id})
        with patch.object(verifier, 'trusted_release', return_value=release), \
             patch.object(verifier, 'check_file'), patch.object(Path, 'read_text', read_text):
            verifier.docker_decode(media, 'video')
            self.assertEqual(approvals, [(release_root, release_root/'releases'/commit, commit)])
            argv, options = calls[-1]
            self.assertIn(image_id, argv)
            for key, value in {'--network': 'none', '--pull': 'never', '--cap-drop': 'ALL', '--memory': '512m',
                '--cpus': '0.5', '--pids-limit': '64', '--entrypoint': '/usr/bin/ffmpeg'}.items():
                self.assertEqual(argv[argv.index(key)+1], value)
            self.assertIn('--read-only', argv)
            self.assertEqual(argv[argv.index('--mount')+1], 'type=bind,src='+str(media)+',dst=/input.mp4,readonly')
            self.assertEqual(argv.count('--mount'), 1)
            self.assertEqual(argv[argv.index('-threads')+1], '2')
            self.assertIn('-xerror', argv)
            self.assertEqual(set(options['environment']), {'PATH', 'LANG', 'DOCKER_CONFIG'})
            calls.clear()
            release.command = lambda *a, **k: ('sha256:'+'c'*64).encode()
            with self.assertRaises(AssertionError):
                verifier.docker_decode(media, 'video')

    def test_docker_decoder_accepts_archive_verified_classic_and_oci_ids_only(self):
        import test_platform_release as archive_contract
        fixture = SimpleNamespace(root=self.root, value={
            'commit': archive_contract.COMMIT, 'image': 'sixnine-platform:'+archive_contract.COMMIT})
        release_root = Path('/srv/sixnine')
        directory = release_root/'releases'/archive_contract.COMMIT
        for format_name in ('legacy', 'classic-oci', 'containerd-oci'):
            if format_name == 'legacy':
                archive = archive_contract.ReleaseTests.archive(fixture)
                expected_ids = {fixture.value['image_id']}
            else:
                archive, expected_ids = archive_contract.ReleaseTests.oci_archive(
                    fixture, containerd=format_name == 'containerd-oci')
            validated, runs = [], []
            def validate_archive(path, manifest):
                self.assertEqual(path, directory/'image.tar.gz')
                self.assertEqual(manifest, fixture.value)
                ids = archive_contract.release.validate_image_archive(archive, manifest)
                validated.append(set(ids))
                return ids
            inspected = fixture.value['image_id']
            def command(argv, **kwargs):
                if argv[0] == 'image':
                    return inspected.encode()
                runs.append(argv)
                return b''
            release = SimpleNamespace(ROOT=release_root, DOCKER='/usr/bin/docker', regular=lambda *a, **k: None,
                approved_manifest=lambda *args: None, command=command, validate_image_archive=validate_archive)
            def read_text(path, **kwargs):
                return json.dumps({'current': archive_contract.COMMIT, 'status': 'app_ready'}
                    if path.name == 'release-state.json' else fixture.value)
            with patch.object(verifier, 'trusted_release', return_value=release), \
                 patch.object(verifier, 'check_file'), patch.object(Path, 'read_text', read_text):
                for identity in expected_ids:
                    inspected = identity
                    with self.subTest(format=format_name, identity=identity):
                        verifier.docker_decode(verifier.ROOT/'generated.mp4', 'video')
                        self.assertEqual(validated[-1], expected_ids)
                        self.assertIn(identity, runs[-1])
                count = len(runs)
                inspected = 'sha256:'+'f'*64
                with self.assertRaises(AssertionError):
                    verifier.docker_decode(verifier.ROOT/'generated.mp4', 'video')
                self.assertEqual(len(runs), count)


if __name__ == '__main__':
    unittest.main()
