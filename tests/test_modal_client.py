"""Fake Modal SDK: no installed SDK, authentication, image build or rental."""

from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from studio_platform.modal_client import ModalSandboxClient
from studio_platform.modal_host import HostSpec


class ModalClientTests(unittest.TestCase):
    def setUp(self):
        self.sdk = SimpleNamespace(
            __version__='1.6.3', App=Mock(), Image=Mock(), Volume=Mock(), Sandbox=Mock()
        )
        self.client = object()
        self.spec = HostSpec('ap-test', 'im-test', 'vo-test', 4_000_000)
        self.adapter = ModalSandboxClient(self.sdk, self.client, self.spec)
        self.sdk.App.lookup.return_value.app_id = 'ap-test'
        self.request = {
            **asdict(self.spec),
            'timeout': 600,
            'name': 'inkseq-00000000-0000-4000-8000-000000000001',
        }

    def test_constructor_never_calls_sdk(self):
        for item in [self.sdk.App, self.sdk.Image, self.sdk.Volume, self.sdk.Sandbox]:
            self.assertEqual(item.mock_calls, [])

    def test_unqualified_sdk_fails_before_auth_or_create(self):
        self.sdk.__version__ = '1.2.4'
        with self.assertRaisesRegex(ValueError, 'modal_sdk_version_unqualified'):
            ModalSandboxClient(self.sdk, self.client, self.spec)
        self.sdk.Sandbox.create.assert_not_called()

    def test_exact_image_and_volume_and_single_process_bounded_resources(self):
        self.sdk.Sandbox.create.return_value.object_id = 'sb-original'
        self.assertEqual(self.adapter.create(self.request), 'sb-original')
        self.sdk.Image.from_id.assert_called_once_with('im-test', client=self.client)
        self.sdk.Volume.from_id.assert_called_once_with('vo-test', client=self.client)
        options = self.sdk.Sandbox.create.call_args.kwargs
        self.assertEqual(options['timeout'], 600)
        self.assertIsNone(options['idle_timeout'])
        self.assertEqual(options['cpu'], (2, 4))
        self.assertEqual(options['memory'], (41984, 41984))
        self.assertNotIn('secrets', options)
        self.assertNotIn('env', options)
        self.assertNotIn('encrypted_ports', options)

    def test_changed_image_or_limits_never_create(self):
        for key, value in [
            ('image_id', 'im-other'),
            ('max_containers', 3),
            ('max_inputs', 2),
            ('timeout', 0),
            ('idle_timeout', 600),
        ]:
            with self.assertRaises(ValueError):
                self.adapter.create({**self.request, key: value})
        self.sdk.Sandbox.create.assert_not_called()

    def test_original_lookup_includes_terminated_and_never_creates(self):
        host = self.sdk.Sandbox.from_name.return_value
        host.object_id = 'sb-original'
        host.poll.return_value = 137
        self.assertEqual(
            self.adapter.lookup(self.request['name']),
            {'id': 'sb-original', 'exit_code': 137},
        )
        self.sdk.Sandbox.from_name.assert_called_once_with(
            'inkseq-staging-worker',
            self.request['name'],
            include_terminated=True,
            client=self.client,
        )
        self.sdk.Sandbox.create.assert_not_called()

    def test_stop_only_targets_original_id(self):
        self.adapter.terminate('sb-original')
        self.sdk.Sandbox.from_id.assert_called_once_with(
            'sb-original', client=self.client
        )
        self.sdk.Sandbox.from_id.return_value.terminate.assert_called_once_with()
