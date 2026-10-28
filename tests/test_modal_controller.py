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

    def test_production_refuses_even_an_enabled_configuration(self):
        self.config(True)
        self.service.settings.public_origin = 'https://inkseq.com'
        with self.assertRaises(ValueError):
            load_extension(self.service, self.root, client_factory=self.factory)
        self.factory.assert_not_called()

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
