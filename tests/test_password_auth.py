"""Password authentication against temporary SQLite and fake credentials only."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

from fastapi.testclient import TestClient
import password_auth

FAKE_PASSWORD = 'fixture-only-password-A'
OTHER_PASSWORD = 'fixture-only-password-B'


class PasswordAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='h3-password-tests-')
        self.data = Path(self.temporary.name)
        self.server = self.load_server('password')
        self.client = TestClient(self.server.app, base_url='https://studio.example.org')
        self.network = mock.patch(
            'httpx.HTTPTransport.handle_request',
            side_effect=AssertionError('Password tests forbid network access'),
        )
        self.network.start()
        for username in password_auth.USERS:
            password_auth.set_password(self.server.DB, username, FAKE_PASSWORD)

    def load_server(self, mode):
        spec = importlib.util.spec_from_file_location(
            '_h3_password_test_' + uuid.uuid4().hex,
            (Path(__file__).resolve().parents[1] / 'server.py'),
        )
        server = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            os.environ,
            {
                'H3_STUDIO_DATA': str(self.data),
                'H3_AUTH_MODE': mode,
                'H3_PUBLIC_ORIGIN': 'https://studio.example.org',
                'H3_GENERATION_ENABLED': '0',
            },
        ):
            spec.loader.exec_module(server)
        return server

    def tearDown(self):
        self.client.close()
        self.network.stop()
        self.temporary.cleanup()

    def login(self, username='superdan', password=FAKE_PASSWORD, client=None):
        return (client or self.client).post(
            '/api/auth/login', json={'username': username, 'password': password}
        )

    def test_two_accounts_password_login_and_secure_opaque_cookie(self):
        for username in ('superdan', 'supervan'):
            result = self.login(username)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(
                result.json(), {'username': username, 'authentication': 'password'}
            )
            for flag in ('httponly', 'secure', 'samesite=lax'):
                self.assertIn(flag, result.headers['set-cookie'].lower())
            self.assertEqual(
                self.client.get('/api/auth/me').json()['username'], username
            )
        with self.server.db() as connection:
            hashes = connection.execute(
                'SELECT password_hash FROM auth_passwords'
            ).fetchall()
            sessions = connection.execute(
                'SELECT token_hash,auth_mode FROM auth_sessions'
            ).fetchall()
        self.assertTrue(all(row[0].startswith(b'$2b$12$') for row in hashes))
        self.assertTrue(
            all(len(row[0]) == 64 and row[1] == 'password' for row in sessions)
        )
        self.assertNotIn(FAKE_PASSWORD, json.dumps(result.json()))

    def test_config_and_health_expose_only_readiness(self):
        result = self.client.get('/api/auth/config')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json(), {'authentication': 'password', 'auth_ready': True}
        )
        self.assertIn('no-store', result.headers['cache-control'])
        self.assertTrue(self.client.get('/healthz').json()['auth_ready'])
        with self.server.db() as connection:
            connection.execute("DELETE FROM auth_passwords WHERE username='supervan'")
        self.assertFalse(self.client.get('/api/auth/config').json()['auth_ready'])
        self.assertEqual(self.client.get('/healthz').status_code, 503)
        self.assertEqual(self.login().status_code, 503)

    def test_wrong_and_unknown_accounts_have_same_error_and_bcrypt_check(self):
        with mock.patch.object(
            password_auth.bcrypt, 'checkpw', wraps=password_auth.bcrypt.checkpw
        ) as check:
            wrong = self.login(password=OTHER_PASSWORD)
            unknown = self.login(username='not-an-account')
        self.assertEqual(check.call_count, 2)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(
            (wrong.status_code, wrong.json()), (unknown.status_code, unknown.json())
        )
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)

    def test_missing_password_and_oversized_or_nonstring_password_are_rejected(self):
        for raw in (
            {'username': 'superdan'},
            {'username': 'superdan', 'password': None},
            {'username': 'superdan', 'password': '界' * 25},
            {'username': 'superdan', 'password': 'a' * 73},
        ):
            result = self.client.post('/api/auth/login', json=raw)
            self.assertEqual(result.status_code, 401)
            self.assertEqual(result.json(), {'detail': '用户名或密码错误'})
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)

    def test_malformed_and_large_body_do_not_echo_secrets(self):
        for body in (
            '{"password":',
            json.dumps({'username': 'superdan', 'password': 'a' * 3000}),
        ):
            result = self.client.post(
                '/api/auth/login',
                content=body,
                headers={'Content-Type': 'application/json'},
            )
            self.assertEqual(result.status_code, 401)
            self.assertEqual(result.json(), {'detail': '用户名或密码错误'})

    def test_rate_limit_fixed_window_expires_without_account_lockout(self):
        start = 2_000_000_000.0
        with mock.patch.object(self.server.time, 'time', return_value=start):
            for _ in range(5):
                self.assertEqual(self.login(password=OTHER_PASSWORD).status_code, 401)
        with mock.patch.object(self.server.time, 'time', return_value=start + 299):
            blocked = self.login()
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers['retry-after'], '1')
        with mock.patch.object(self.server.time, 'time', return_value=start + 301):
            self.assertEqual(self.login().status_code, 200)
        with self.server.db() as connection:
            self.assertEqual(
                connection.execute('SELECT COUNT(*) FROM auth_login_limits').fetchone()[
                    0
                ],
                0,
            )

    def test_password_change_revokes_only_target_sessions_and_old_password(self):
        other = TestClient(self.server.app, base_url='https://studio.example.org')
        try:
            self.assertEqual(self.login().status_code, 200)
            self.assertEqual(self.login('supervan', client=other).status_code, 200)
            password_auth.set_password(self.server.DB, 'superdan', OTHER_PASSWORD)
            self.assertEqual(self.client.get('/api/auth/me').status_code, 401)
            self.assertEqual(other.get('/api/auth/me').status_code, 200)
            self.assertEqual(self.login().status_code, 401)
            self.assertEqual(self.login(password=OTHER_PASSWORD).status_code, 200)
        finally:
            other.close()

    def test_password_change_during_login_does_not_create_old_password_session(self):
        original = self.server.verify_login

        def changed_during_verification(username, password):
            revision = original(username, password)
            password_auth.set_password(self.server.DB, username, OTHER_PASSWORD)
            return revision

        with mock.patch.object(
            self.server, 'verify_login', side_effect=changed_during_verification
        ):
            self.assertEqual(self.login().status_code, 401)
        with self.server.db() as connection:
            self.assertEqual(
                connection.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0],
                0,
            )

    def test_disabled_account_prevents_ready_and_session_use(self):
        self.assertEqual(self.login().status_code, 200)
        with self.server.db() as connection:
            connection.execute(
                "UPDATE auth_passwords SET disabled=1 WHERE username='superdan'"
            )
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)
        self.assertFalse(self.client.get('/api/auth/config').json()['auth_ready'])

    def test_legacy_username_session_does_not_authorize_password_mode(self):
        legacy = self.load_server('username-test')
        with TestClient(legacy.app, base_url='https://studio.example.org') as client:
            self.assertEqual(
                client.post(
                    '/api/auth/login', json={'username': 'superdan'}
                ).status_code,
                200,
            )
            self.client.cookies.update(client.cookies)
            self.assertEqual(self.client.get('/api/auth/me').status_code, 401)

    def test_schema_password_is_writeonly_and_config_is_public(self):
        schema = self.client.get('/openapi.json').json()
        body = schema['paths']['/api/auth/login']['post']['requestBody']['content'][
            'application/json'
        ]['schema']
        self.assertEqual(body['required'], ['username', 'password'])
        self.assertTrue(body['properties']['password']['writeOnly'])
        self.assertNotIn('enum', body['properties']['username'])
        self.assertNotIn('security', schema['paths']['/api/auth/config']['get'])
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(
            self.client.post(
                '/api/auth/logout', headers={'Origin': 'https://attacker.invalid'}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post('/api/auth/logout').status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)

    def test_readiness_rejects_missing_table_or_invalid_hash(self):
        with sqlite3.connect(':memory:') as connection:
            self.assertFalse(password_auth.passwords_ready(connection))
        with self.server.db() as connection:
            connection.execute(
                "UPDATE auth_passwords SET password_hash=? WHERE username='supervan'",
                (b'not-a-hash',),
            )
            self.assertFalse(password_auth.passwords_ready(connection))

    def test_interactive_provisioner_never_prints_password_and_no_pipe(self):
        spec = importlib.util.spec_from_file_location(
            '_h3_manage_users_test',
            Path(__file__).resolve().parents[1] / 'tools/manage_users.py',
        )
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        output = io.StringIO()
        with (
            mock.patch(
                'sys.argv',
                [
                    'manage_users.py',
                    '--data-dir',
                    str(self.data),
                    '--username',
                    'superdan',
                ],
            ),
            mock.patch.object(tool.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(
                tool.getpass, 'getpass', side_effect=[OTHER_PASSWORD, OTHER_PASSWORD]
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(tool.main(), 0)
        self.assertNotIn(OTHER_PASSWORD, output.getvalue())
        self.assertNotIn('$2b$', output.getvalue())
        with (
            mock.patch('sys.argv', ['manage_users.py', '--data-dir', str(self.data)]),
            mock.patch.object(tool.sys.stdin, 'isatty', return_value=False),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                tool.main()


if __name__ == '__main__':
    unittest.main()
