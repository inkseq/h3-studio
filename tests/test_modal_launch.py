"""Private launch install and SDK ambiguity; no provider or model calls."""

import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from studio_platform.modal_entry import install_launch, launch_document
from studio_platform.modal_launch import initialize
from studio_platform.modal_runtime import PROFILE
from studio_platform.runtime_catalog import engine_manifest


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.state.chmod(0o700)
        self.value = {
            'intent_id': '12345678-1234-1234-1234-123456789012',
            'mode': 'fl',
            'manifest_digest': engine_manifest(PROFILE, 'fl').digest,
            'token': 'a' * 64,
        }

    def test_one_atomic_install_reconciles_same_binding_without_rewrite(self):
        self.assertEqual(install_launch(self.value, self.state), 'installed')
        path = self.state / 'launch' / 'value.json'
        before = path.stat()
        self.assertEqual(launch_document(path), self.value)
        self.assertEqual(install_launch(dict(self.value), self.state), 'observed')
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse((self.state / 'launch.pending').exists())
        with self.assertRaises(ValueError):
            install_launch({**self.value, 'token': 'b' * 64}, self.state)
        self.assertEqual(launch_document(path), self.value)

    def test_interrupted_pending_write_is_not_replaced(self):
        pending = self.state / 'launch.pending'
        pending.write_bytes(b'partial')
        with self.assertRaises(FileExistsError):
            install_launch(self.value, self.state)
        self.assertEqual(pending.read_bytes(), b'partial')
        self.assertFalse((self.state / 'launch').exists())

    def test_sdk_stdin_only_and_unknown_never_retries(self):
        stream = io.BytesIO()
        stream.drain = Mock()
        stream.write_eof = Mock()
        process = SimpleNamespace(
            stdin=stream, stdout=io.BytesIO(b'installed\n'), wait=Mock(return_value=0)
        )
        sandbox = SimpleNamespace(exec=Mock(return_value=process))
        self.assertEqual(
            initialize(sandbox, self.value, stderr_sink=object()), 'installed'
        )
        self.assertEqual(json.loads(stream.getvalue()), self.value)
        args, kwargs = sandbox.exec.call_args
        self.assertNotIn(self.value['token'], str(args))
        self.assertEqual(kwargs['timeout'], 15)
        sandbox.exec.side_effect = RuntimeError('provider-private-error')
        with self.assertRaisesRegex(ValueError, '^modal_launch_result_unknown$'):
            initialize(sandbox, self.value, stderr_sink=object())
        self.assertEqual(sandbox.exec.call_count, 2)

    def test_invalid_launch_never_reaches_sdk(self):
        sandbox = SimpleNamespace(exec=Mock())
        with self.assertRaises(ValueError):
            initialize(
                sandbox, {**self.value, 'api_key': 'forbidden'}, stderr_sink=object()
            )
        sandbox.exec.assert_not_called()
