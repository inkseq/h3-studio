"""Run the actual public acceptance procedure against disposable password state."""
import importlib.util
import io
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from studio_platform.api import create_app
from studio_platform.settings import Settings

DIRECTORY = Path(__file__).resolve().parents[1] / 'deploy' / 'platform'


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load('runtime_secrets_aws')
with patch.dict(sys.modules, {'runtime_secrets_aws': runtime}):
    verifier = load('verify_public_api')


class PublicVerificationTest(unittest.TestCase):
    def test_public_procedure_without_network_or_real_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Settings(data_dir=Path(temporary), public_origin=verifier.ORIGIN,
                                      auth_mode='password'))
            passwords = {name: secrets.token_urlsafe(30) for name in ('superdan', 'supervan')}
            for name, password in passwords.items():
                app.state.auth.set_password(name, password)
            adapters = []
            class Adapter:
                def __init__(self):
                    self.session = TestClient(app, base_url=verifier.ORIGIN)
                    adapters.append(self)
                def open(self, request, timeout):
                    response = self.session.request(request.method, request.full_url,
                        headers=dict(request.header_items()), content=request.data,
                        follow_redirects=False)
                    body = io.BytesIO(response.content)
                    body.code = response.status_code
                    return body
            try:
                with patch.object(verifier, 'client', Adapter), \
                     patch.object(runtime, 'client', return_value=object()), \
                     patch.object(runtime, 'value', return_value=dict(passwords)):
                    result = verifier.verify()
                self.assertEqual(len(result['stories']), 2)
                self.assertFalse(result['generation_called'])
                self.assertIn('all_test_keys_revoked_and_sessions_logged_out', result['checks'])
                self.assertTrue(all(row['revoked_at'] is not None
                    for name in passwords for row in app.state.auth.list_keys(name)))
            finally:
                for adapter in adapters:
                    adapter.session.close()
                app.state.repository.close()


if __name__ == '__main__':
    unittest.main()
