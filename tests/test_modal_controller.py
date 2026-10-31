"""Default-OFF integration and same-loop ordering, without SDK/credential I/O."""

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from studio_platform.modal_controller import ModalExtension, load_extension
from test_modal_budget import ModalBudgetCase


class ModalControllerTests(ModalBudgetCase):
    def setUp(self):
        super().setUp()
        self.service = SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(
                public_origin='https://staging.inkseq.com', generation_queue='dbos'
            ),
        )
        self.factory = Mock()
        self.root = Path(self.temp.name)

    def config(self, enabled):
        path = self.root / 'modal.json'
        path.write_text(
            json.dumps(dict(version=1, enabled=enabled, host=asdict(self.spec)))
        )
        path.chmod(0o600)

    def test_absent_or_disabled_configuration_does_not_load_sdk_or_secret(self):
        self.assertIsNone(
            load_extension(self.service, self.root, client_factory=self.factory)
        )
        self.config(False)
        self.assertIsNone(
            load_extension(self.service, self.root, client_factory=self.factory)
        )
        self.factory.assert_not_called()

    def test_production_refuses_a_staging_shaped_configuration(self):
        self.config(True)
        self.service.settings.public_origin = 'https://inkseq.com'
        with self.assertRaises(ValueError):
            load_extension(self.service, self.root, client_factory=self.factory)
        self.factory.assert_not_called()

    def test_production_backstop_needs_its_own_app_file_and_account(self):
        # #688: production uses an explicit credentials file, the production
        # Modal app and the production budget account; never staging values.
        from dataclasses import replace

        self.service.settings.public_origin = 'https://inkseq.com'
        credentials = str(self.root / 'modal-credentials.json')
        self.factory.return_value = (self.client, Mock())
        for app_name, enabled in (
            ('inkseq-production-worker', True),
            ('inkseq-staging-worker', False),
        ):
            spec = replace(self.spec, app_name=app_name)
            path = self.root / 'modal.json'
            path.write_text(
                json.dumps(
                    dict(
                        version=1,
                        enabled=True,
                        host=asdict(spec),
                        credentials_file=credentials,
                        budget_account_id='production-gpu',
                        start_after_seconds=0,
                        vast_min_queue=20,
                        vast_after_seconds=600,
                    )
                )
            )
            path.chmod(0o600)
            extension = load_extension(
                self.service, self.root, client_factory=self.factory
            )
            self.assertEqual(extension.hosts.environment, 'production')
            self.assertIs(extension.admission.enabled, enabled)
            self.assertIs(extension.admission.production, True)
            self.assertEqual(extension.admission.start_after_seconds, 0)
            self.assertEqual(
                self.service.modal_first,
                {'vast_min_queue': 20, 'vast_after_seconds': 600},
            )

    def test_configuration_without_profile_is_the_v7_image(self):
        from studio_platform.modal_runtime import V7

        host = asdict(self.spec)
        host.pop('profile')
        path = self.root / 'modal.json'
        path.write_text(json.dumps(dict(version=1, enabled=True, host=host)))
        path.chmod(0o600)
        self.factory.return_value = (self.client, Mock())
        extension = load_extension(self.service, self.root, client_factory=self.factory)
        self.assertEqual(extension.hosts.spec.profile, V7)

    def test_live_original_without_configuration_refuses_silent_abandonment(self):
        self.reserve()
        with self.assertRaises(ValueError):
            load_extension(self.service, self.root, client_factory=self.factory)
        self.factory.assert_not_called()

    def test_off_switch_preserves_original_host_reconciliation(self):
        self.reserve()
        self.config(False)
        self.factory.return_value = (self.client, Mock())
        extension = load_extension(self.service, self.root, client_factory=self.factory)
        self.assertFalse(extension.admission.enabled)
        self.assertEqual(self.client.created, [])

    def test_sandbox_exec_gets_modal_stream_type(self):
        from modal.stream_type import StreamType

        self.reserve()
        self.config(False)
        self.factory.return_value = (self.client, Mock())
        extension = load_extension(self.service, self.root, client_factory=self.factory)
        self.assertIs(extension.bindings.stderr_sink, StreamType.DEVNULL)

    def test_fallback_runs_before_original_vast_planner_in_same_tick(self):
        order = []
        extension = ModalExtension(self.service, self.hosts, Mock(), enabled=True)
        original = Mock()
        original.tick.side_effect = lambda: order.append('vast') or {'state': 'running'}
        extension.wrap(original)
        extension.lifecycle = Mock()
        extension.lifecycle.sweep.side_effect = lambda: order.append('sweep') or []
        extension.admission = Mock()
        extension.admission.tick.side_effect = lambda: order.append('modal') or {}
        self.assertEqual(extension.tick()['state'], 'running')
        self.assertEqual(order, ['sweep', 'modal', 'vast'])

    def test_exited_host_keeps_off_switch_reconciliation_and_alerts(self):
        identity = self.reserve()
        self.hosts.start(identity)
        self.hosts.stop(identity)
        self.hosts.observe(identity)
        self.config(False)
        self.factory.return_value = (self.client, Mock())
        extension = load_extension(self.service, self.root, client_factory=self.factory)
        self.assertIsNotNone(extension)
        self.assertFalse(extension.admission.enabled)

    def test_safety_only_reconciles_never_admits(self):
        extension = ModalExtension(self.service, self.hosts, Mock(), enabled=True)
        original = Mock()
        original.safety_sweep.return_value = {'state': 'observed'}
        extension.wrap(original)
        extension.lifecycle, extension.admission = Mock(), Mock()
        extension.lifecycle.sweep.return_value = []
        extension.safety_sweep()
        extension.admission.tick.assert_not_called()
