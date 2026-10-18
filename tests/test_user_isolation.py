"""Two-user isolation acceptance: temp SQLite/media and blocked HTTP transport.

Does not import the production DATA, start the worker, or make a GPU request.
"""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image


class UserIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='h3-user-isolation-')
        cls.data = Path(cls.temp.name) / 'main'
        cls.server = cls.load_server(cls.data)
        cls.network = mock.patch(
            'httpx.HTTPTransport.handle_request',
            side_effect=AssertionError('Isolation tests forbid HTTP network requests'),
        )
        cls.network.start()

    @classmethod
    def load_server(cls, data, origin=''):
        spec = importlib.util.spec_from_file_location(
            '_h3_owned_test_' + uuid.uuid4().hex,
            (Path(__file__).resolve().parents[1] / 'server.py'),
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            os.environ,
            {
                'H3_STUDIO_DATA': str(data),
                'H3_COMFY_URL': 'http://127.0.0.1:8189',
                'H3_PUBLIC_ORIGIN': origin,
            },
        ):
            spec.loader.exec_module(module)
        return module

    @classmethod
    def tearDownClass(cls):
        cls.server.STOP.set()
        cls.network.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.server.STOP.clear()
        self.server.BLOCKED = None
        with self.server.db() as c:
            for table in ('jobs', 'uploads', 'auth_sessions'):
                c.execute('DELETE FROM ' + table)
        self.dan = TestClient(self.server.app)
        self.van = TestClient(self.server.app)
        self.anon = TestClient(self.server.app)
        self.dan.post(
            '/api/auth/login', json={'username': 'superdan'}
        ).raise_for_status()
        self.van.post(
            '/api/auth/login', json={'username': 'supervan'}
        ).raise_for_status()
        self.cap_patch = mock.patch.object(
            self.server,
            'capability',
            return_value={
                'backends': [{'available': True, 'reason': ''}],
                'expert_url': 'http://127.0.0.1:8189/',
            },
        )
        self.cap = self.cap_patch.start()

    def tearDown(self):
        self.cap_patch.stop()
        for client in (self.dan, self.van, self.anon):
            client.close()

    def payload(self, **extra):
        raw = dict(
            backend='comfy-local',
            model=self.server.MODEL,
            mode='fl',
            prompt='A cinematic quiet mountain sunrise.',
            duration=5,
            resolution='768P',
            aspect_ratio='16:9',
            generate_audio=True,
            seed=42,
            inputs={},
        )
        raw.update(extra)
        return raw

    def upload(self, client):
        stream = io.BytesIO()
        Image.new('RGB', (320, 320), 'orange').save(stream, format='PNG')
        response = client.post(
            '/api/uploads',
            files={'file': ('reference.png', stream.getvalue(), 'image/png')},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def job(self, client, payload=None, **kwargs):
        response = client.post('/api/jobs', json=payload or self.payload(), **kwargs)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_login_allowlist_cookie_flags_and_only_hashes_persisted(self):
        response = self.anon.post('/api/auth/login', json={'username': 'supervan'})
        self.assertEqual(
            response.json(),
            {'username': 'supervan', 'authentication': 'username-only-test'},
        )
        cookie = response.headers['set-cookie'].lower()
        for flag in ('httponly', 'samesite=lax', 'path=/', 'max-age=43200'):
            self.assertTrue(flag in cookie)
        self.assertEqual(self.anon.get('/api/auth/me').json(), response.json())
        token = self.anon.cookies.get(self.server.SESSION_COOKIE)
        with self.server.db() as c:
            row = c.execute(
                'SELECT token_hash,username FROM auth_sessions WHERE token_hash=?',
                (self.server.session_hash(token),),
            ).fetchone()
        self.assertTrue(row is not None and row[1] == 'supervan')
        self.assertTrue(row[0] != token and len(row[0]) == 64)
        self.assertFalse(token in response.text)
        for bad in (
            {'username': 'unknown'},
            {'username': 'Superdan'},
            {'username': ' superdan'},
            {'username': []},
            {'username': None},
            {'username': 'superdan', 'owner': 'supervan'},
            [],
            {},
        ):
            with self.subTest(body=bad):
                self.assertEqual(
                    self.anon.post('/api/auth/login', json=bad).status_code, 422
                )

    def test_unauthenticated_api_cannot_read_submit_or_download(self):
        jid = 'f' * 32
        uid = 'e' * 32
        requests = [
            ('get', '/api/auth/me', {}),
            ('get', '/api/capabilities', {}),
            ('get', '/api/jobs', {}),
            ('get', f'/api/jobs/{jid}', {}),
            ('get', f'/api/jobs/{jid}/output', {}),
            ('get', f'/api/jobs/{jid}/audio', {}),
            ('post', f'/api/jobs/{jid}/cancel', {}),
            ('get', f'/api/uploads/{uid}', {}),
            ('get', f'/api/uploads/{uid}/content', {}),
            ('post', '/api/jobs', {'json': self.payload()}),
            ('post', '/api/workflow-preview', {'json': self.payload()}),
            (
                'post',
                '/api/uploads',
                {'files': {'file': ('bad.png', b'bad', 'image/png')}},
            ),
        ]
        self.cap.reset_mock()
        for method, path, kwargs in requests:
            with self.subTest(method=method, path=path):
                response = getattr(self.anon, method)(path, **kwargs)
                self.assertEqual(response.status_code, 401)
                self.assertIn('no-store', response.headers['cache-control'])
        self.cap.assert_not_called()
        self.assertEqual(self.anon.get('/').status_code, 200)

    def test_logout_and_expiry_revoke_persisted_session(self):
        token = self.dan.cookies.get(self.server.SESSION_COOKIE)
        self.assertEqual(self.dan.post('/api/auth/logout').json(), {'logged_out': True})
        self.assertEqual(self.dan.get('/api/auth/me').status_code, 401)
        self.anon.cookies.set(self.server.SESSION_COOKIE, token)
        self.assertEqual(self.anon.get('/api/auth/me').status_code, 401)
        self.assertEqual(self.anon.post('/api/auth/logout').status_code, 200)
        with self.server.db() as c:
            c.execute(
                'UPDATE auth_sessions SET expires=0 WHERE username=?', ('supervan',)
            )
        self.assertEqual(self.van.get('/api/auth/me').status_code, 401)
        with self.server.db() as c:
            self.assertEqual(
                c.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0], 0
            )

    def test_account_switch_rotates_cookie_and_invalidates_old_session(self):
        old = self.dan.cookies.get(self.server.SESSION_COOKIE)
        self.dan.post(
            '/api/auth/login', json={'username': 'supervan'}
        ).raise_for_status()
        self.assertEqual(self.dan.get('/api/auth/me').json()['username'], 'supervan')
        self.assertTrue(old != self.dan.cookies.get(self.server.SESSION_COOKIE))
        self.anon.cookies.set(self.server.SESSION_COOKIE, old)
        self.assertEqual(self.anon.get('/api/auth/me').status_code, 401)

    def test_owned_upload_metadata_content_ranges_and_cache_headers(self):
        for owner, other in ((self.dan, self.van), (self.van, self.dan)):
            asset = self.upload(owner)
            uid = asset['id']
            self.assertEqual(owner.get(f'/api/uploads/{uid}').status_code, 200)
            data = owner.get(asset['preview_url'], headers={'Range': 'bytes=0-15'})
            self.assertIn(data.status_code, (200, 206))
            self.assertIn('no-store', data.headers['cache-control'])
            for path in (f'/api/uploads/{uid}', asset['preview_url']):
                response = other.get(path, headers={'Range': 'bytes=0-15'})
                self.assertEqual(response.status_code, 422)
                self.assertNotEqual(response.content, data.content)
            self.assertIn(other.head(asset['preview_url']).status_code, (404, 405))

    def test_job_list_status_cancel_and_all_downloads_are_owned(self):
        for owner, other in ((self.dan, self.van), (self.van, self.dan)):
            job = self.job(owner)
            jid = job['id']
            self.assertEqual(owner.get(f'/api/jobs/{jid}').status_code, 200)
            self.assertEqual(other.post(f'/api/jobs/{jid}/cancel').status_code, 404)
            self.assertEqual(self.server.get_job(jid)['status'], 'queued')
            job.update(
                status='succeeded',
                output_url=f'/api/jobs/{jid}/output',
                audio_output_url=f'/api/jobs/{jid}/audio',
            )
            self.server.put_job(job)
            (self.data / 'outputs' / (jid + '.mp4')).write_bytes(
                b'offline owned video fixture'
            )
            (self.data / 'outputs' / (jid + '.flac')).write_bytes(
                b'offline owned audio fixture'
            )
            for suffix in ('', '/output', '/audio'):
                path = f'/api/jobs/{jid}' + suffix
                self.assertEqual(owner.get(path).status_code, 200)
                response = other.get(path, headers={'Range': 'bytes=0-7'})
                self.assertEqual(response.status_code, 404)
            self.assertIn(other.head(f'/api/jobs/{jid}/output').status_code, (404, 405))
        dan_jobs = self.dan.get('/api/jobs').json()['jobs']
        van_jobs = self.van.get('/api/jobs').json()['jobs']
        self.assertEqual(len(dan_jobs), 1)
        self.assertEqual(len(van_jobs), 1)
        self.assertEqual(dan_jobs[0]['owner'], 'superdan')
        self.assertEqual(van_jobs[0]['owner'], 'supervan')
        self.assertNotEqual(dan_jobs[0]['id'], van_jobs[0]['id'])

    def test_foreign_reference_first_last_frame_and_guide_ids_rejected_before_gpu(self):
        foreign = self.upload(self.dan)['id']
        own = self.upload(self.van)['id']
        cases = [
            self.payload(mode='ref', inputs={'images': [foreign]}),
            self.payload(inputs={'first_frame': foreign}),
            self.payload(inputs={'last_frame': foreign}),
            self.payload(guides=[{'media_id': foreign, 'time_seconds': 0}]),
            self.payload(
                mode='ref',
                inputs={'images': [own]},
                guides=[{'media_id': foreign, 'time_seconds': 2}],
            ),
        ]
        with mock.patch.object(
            self.server,
            'capability',
            side_effect=AssertionError('Foreign ID must not probe GPU'),
        ):
            for raw in cases:
                for endpoint in ('/api/workflow-preview', '/api/jobs'):
                    with self.subTest(endpoint=endpoint, inputs=raw['inputs']):
                        self.assertEqual(
                            self.van.post(endpoint, json=raw).status_code, 422
                        )
        self.assertEqual(self.van.get('/api/jobs').json(), {'jobs': []})

    def test_own_reference_and_guide_preview_preserve_controls_and_uint64_seed(self):
        uid = self.upload(self.van)['id']
        raw = self.payload(
            mode='ref',
            inputs={'images': [uid]},
            guides=[{'media_id': uid, 'time_seconds': 2}],
            video_decode='tiled',
            seed=str(2**64 - 1),
            sampler_name='euler',
            scheduler='karras',
            steps=25,
        )
        self.cap.reset_mock()
        response = self.van.post('/api/workflow-preview', json=raw)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body['gpu_submitted'])
        self.assertEqual(body['request']['seed'], str(2**64 - 1))
        self.assertTrue(
            any(n['class_type'] == 'MiniMaxH3AddGuide' for n in body['graph'].values())
        )
        self.cap.assert_not_called()

    def test_owner_cannot_be_spoofed_in_raw_request_or_login_fields(self):
        for key in ('owner', 'username', 'user_id'):
            raw = self.payload()
            raw[key] = 'superdan'
            for path in ('/api/jobs', '/api/workflow-preview'):
                self.assertEqual(self.van.post(path, json=raw).status_code, 422)
        job = self.job(
            self.van, headers={'X-Username': 'superdan', 'X-Owner': 'superdan'}
        )
        self.assertEqual(job['owner'], 'supervan')
        self.assertEqual(self.dan.get('/api/jobs').json(), {'jobs': []})

    def test_idempotency_key_is_unique_within_each_owner_not_global(self):
        key = {'Idempotency-Key': 'same-key-two-users'}
        dan = self.job(self.dan, headers=key)
        van = self.job(self.van, headers=key)
        self.assertNotEqual(dan['id'], van['id'])
        with mock.patch.object(
            self.server,
            'capability',
            side_effect=AssertionError('Replay must not probe GPU'),
        ):
            self.assertEqual(self.job(self.dan, headers=key)['id'], dan['id'])
            self.assertEqual(self.job(self.van, headers=key)['id'], van['id'])
        changed = self.payload()
        changed['prompt'] = 'A different scene.'
        self.assertEqual(
            self.van.post('/api/jobs', json=changed, headers=key).status_code, 409
        )

    def test_single_global_worker_loads_only_the_job_owners_media(self):
        dan_uid = self.upload(self.dan)['id']
        van_uid = self.upload(self.van)['id']
        wrong, _ = self.server.validate_job(
            self.payload(mode='ref', inputs={'images': [dan_uid]}), owner='superdan'
        )
        client = mock.Mock()
        with self.assertRaises(HTTPException):
            self.server.execute_job(
                {'id': uuid.uuid4().hex, 'owner': 'supervan', 'request': wrong}, client
            )
        client.post.assert_not_called()
        own_request, _ = self.server.validate_job(
            self.payload(mode='ref', inputs={'images': [van_uid]}), owner='supervan'
        )
        upload_response = mock.Mock()
        upload_response.json.return_value = {'name': van_uid + '.normalized.png'}
        queue_response = mock.Mock()
        queue_response.json.return_value = {
            'queue_running': [['existing']],
            'queue_pending': [],
        }
        client.post.return_value = upload_response
        client.get.return_value = queue_response
        with self.assertRaisesRegex(RuntimeError, 'GPU队列已有其他任务'):
            self.server.execute_job(
                {'id': uuid.uuid4().hex, 'owner': 'supervan', 'request': own_request},
                client,
            )
        self.assertEqual(client.post.call_count, 1)
        self.assertEqual(
            client.post.call_args.args[0], self.server.COMFY + '/upload/image'
        )
        self.assertEqual(
            client.post.call_args.kwargs['files']['image'][0],
            van_uid + '.normalized.png',
        )

    def test_legacy_migration_preserves_assets_old_keys_and_can_be_reopened(self):
        data = Path(self.temp.name) / ('legacy-' + uuid.uuid4().hex)
        data.mkdir()
        raw = self.payload()
        jid = 'd' * 32
        uid = 'a' * 32
        record = {
            'id': jid,
            'status': 'succeeded',
            'request': raw,
            'request_fingerprint': hashlib.sha256(
                json.dumps(raw, sort_keys=True).encode()
            ).hexdigest(),
        }
        with sqlite3.connect(data / 'studio.sqlite3') as c:
            c.execute(
                'CREATE TABLE uploads(id TEXT PRIMARY KEY,metadata TEXT NOT NULL)'
            )
            c.execute(
                'CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,record TEXT NOT NULL,idem TEXT UNIQUE)'
            )
            c.execute(
                'INSERT INTO uploads VALUES(?,?)',
                (uid, json.dumps({'id': uid, 'kind': 'image'})),
            )
            c.execute(
                'INSERT INTO jobs VALUES(?,?,?,?,?)',
                (jid, 'succeeded', 1, json.dumps(record), 'legacy-existing-key'),
            )
        c.close()
        server = self.load_server(data)
        first = TestClient(server.app)
        second = TestClient(server.app)
        try:
            first.post(
                '/api/auth/login', json={'username': 'superdan'}
            ).raise_for_status()
            second.post(
                '/api/auth/login', json={'username': 'supervan'}
            ).raise_for_status()
            self.assertEqual(first.get(f'/api/uploads/{uid}').status_code, 200)
            self.assertEqual(second.get(f'/api/uploads/{uid}').status_code, 422)
            self.assertEqual(first.get(f'/api/jobs/{jid}').json()['owner'], 'superdan')
            self.assertEqual(second.get(f'/api/jobs/{jid}').status_code, 404)
            with mock.patch.object(
                server,
                'capability',
                side_effect=AssertionError('Legacy replay must not probe GPU'),
            ):
                replay = first.post(
                    '/api/jobs',
                    json=raw,
                    headers={'Idempotency-Key': 'legacy-existing-key'},
                )
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertEqual(replay.json()['id'], jid)
            with mock.patch.object(
                server,
                'capability',
                return_value={'backends': [{'available': True, 'reason': ''}]},
            ):
                fresh = second.post(
                    '/api/jobs',
                    json=raw,
                    headers={'Idempotency-Key': 'legacy-existing-key'},
                )
            self.assertEqual(fresh.status_code, 202, fresh.text)
            self.assertNotEqual(fresh.json()['id'], jid)
            token = first.cookies.get(server.SESSION_COOKIE)
            reopened = self.load_server(data)
            third = TestClient(reopened.app)
            try:
                third.cookies.set(reopened.SESSION_COOKIE, token)
                self.assertEqual(
                    third.get('/api/auth/me').json()['username'], 'superdan'
                )
                self.assertEqual(third.get(f'/api/jobs/{jid}').json()['id'], jid)
                with reopened.db() as c:
                    self.assertEqual(
                        c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2
                    )
            finally:
                third.close()
        finally:
            first.close()
            second.close()

    def test_starter_with_historical_image_is_superdan_only(self):
        paths = (
            '/h3-ref-bf16-workflow.json',
            '/%2e/h3-ref-bf16-workflow.json',
            '/%2f/h3-ref-bf16-workflow.json',
            '/x/%2e%2e/h3-ref-bf16-workflow.json',
            '/x%5c..%5ch3-ref-bf16-workflow.json',
            '/h3-ref-bf16-workflow.json/',
            '/H3-REF-BF16-WORKFLOW.JSON',
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(self.anon.get(path).status_code, (401, 404))
                self.assertEqual(self.van.get(path).status_code, 404)
                self.assertIn(self.anon.head(path).status_code, (401, 404))
                self.assertEqual(
                    self.van.get(path, headers={'Range': 'bytes=0-7'}).status_code, 404
                )
        response = self.dan.get(paths[0])
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['cache-control'])
        self.assertIsNone(self.van.get('/api/capabilities').json()['expert_url'])
        self.assertEqual(
            self.dan.get('/api/capabilities').json()['expert_url'],
            'http://127.0.0.1:8189/',
        )

    def test_exact_public_origin_is_opt_in_https_secure_cookie_and_no_wildcards(self):
        data = Path(self.temp.name) / ('public-' + uuid.uuid4().hex)
        server = self.load_server(data, origin='https://studio.example.org')
        client = TestClient(server.app, base_url='https://studio.example.org')
        try:
            response = client.post(
                '/api/auth/login',
                json={'username': 'supervan'},
                headers={'Origin': 'https://studio.example.org'},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue('secure' in response.headers['set-cookie'].lower())
            self.assertEqual(client.get('/api/auth/me').status_code, 200)
            self.assertEqual(
                client.get(
                    '/api/auth/me', headers={'Host': 'attacker.example.org'}
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    '/api/auth/logout',
                    headers={'Origin': 'https://attacker.example.org'},
                ).status_code,
                403,
            )
        finally:
            client.close()
        for origin in (
            'https://*.example.org',
            'http://studio.example.org',
            'https://studio.example.org/path',
            'https://user:secret@studio.example.org',
            'https://studio.example.org?key=redacted',
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(RuntimeError):
                    self.load_server(data, origin=origin)

    def test_openapi_documents_cookie_session_without_exposing_one(self):
        schema = self.anon.get('/openapi.json').json()
        scheme = schema['components']['securitySchemes']['TestSession']
        self.assertEqual(scheme['in'], 'cookie')
        self.assertEqual(scheme['name'], self.server.SESSION_COOKIE)
        self.assertEqual(
            schema['paths']['/api/jobs']['post']['security'], [{'TestSession': []}]
        )
        self.assertNotIn('security', schema['paths']['/api/auth/login']['post'])


if __name__ == '__main__':
    unittest.main()
