"""Private CPU binding on fake original Sandboxes; no provider/model calls."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from sqlalchemy import insert, select

from studio_platform.modal_admission import OPERATION
from studio_platform.modal_worker_binding import ModalWorkerBindings
from studio_platform.modal_runtime import PROFILE
from studio_platform.repository import BudgetExceeded, scaler_receipts
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.runtime_catalog import engine_manifest
from test_modal_budget import ModalBudgetCase


class WorkerBindingTests(ModalBudgetCase):
    def setUp(self):
        super().setUp()
        self.identity = self.reserve()
        self.hosts.start(self.identity)
        self.manifest = engine_manifest(PROFILE, 'fl')
        with self.repo.transaction() as c:
            c.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=self.identity,
                    operation=OPERATION,
                    observed_at=self.now,
                    facts={
                        'mode': 'fl',
                        'configuration_id': 'v7-test',
                        'manifest_digest': self.manifest.digest,
                    },
                )
            )
        self.connect = Mock(return_value=SimpleNamespace(object_id='sb-1'))
        self.initialize = Mock()
        self.observe = Mock(return_value=True)
        self.readiness = Mock(
            return_value=HostReadiness(
                self.manifest.digest, 'modal-' + self.identity, 'a' * 32, True
            )
        )
        self.root = Path(self.temp.name) / 'modal-bindings'
        self.bindings = ModalWorkerBindings(
            self.repo,
            self.root,
            self.connect,
            stderr_sink=object(),
            initializer=self.initialize,
            observer=self.observe,
            readiness=self.readiness,
        )

    def test_original_slot_uses_one_private_token_and_one_initialization(self):
        first = self.bindings.slot(self.identity)
        again = self.bindings.slot(self.identity)
        self.assertEqual(first, again)
        self.assertEqual(self.initialize.call_count, 1)
        self.assertEqual(self.observe.call_count, 1)
        self.assertEqual(first.spec.provider, 'modal')
        self.assertEqual(first.spec.instance_id, 'sb-1')
        self.assertEqual(first.spec.dispatch_backend, 'hatchet-v1')
        with self.repo.engine.connect() as c:
            facts = list(c.execute(select(scaler_receipts.c.facts)).scalars())
        value = self.initialize.call_args.args[1]
        self.assertNotIn(value['token'], str(facts))

    def test_worker_runner_uses_the_same_guarded_capacity_reader(self):
        slot = self.bindings.slot(self.identity)
        config, reader = Mock(), Mock()
        config.slot.return_value = slot
        self.bindings.capacity_usage = reader
        with patch('studio_platform.modal_worker_binding.slot_runner') as runner:
            self.bindings.runner_context(
                config, slot.spec.worker_id, Mock(), repository=self.repo
            )
        self.assertIs(runner.call_args.kwargs['capacity_usage'], reader)
        self.assertTrue(callable(runner.call_args.kwargs['backend_factory']))

    def test_missing_capacity_reader_cannot_fall_back_to_unqualified_registration(self):
        slot = self.bindings.slot(self.identity)
        config = Mock()
        config.slot.return_value = slot
        with patch('studio_platform.modal_worker_binding.slot_runner') as runner:
            with self.assertRaises(BudgetExceeded):
                self.bindings.runner_context(
                    config, slot.spec.worker_id, Mock(), repository=self.repo
                )
        runner.assert_not_called()

    def test_unknown_initialize_reads_original_and_never_executes_again(self):
        self.initialize.side_effect = ValueError('private-value')
        with self.assertRaisesRegex(ValueError, 'modal_worker_binding_unconfirmed'):
            self.bindings.slot(self.identity)
        self.initialize.side_effect = None
        self.bindings.slot(self.identity)
        self.assertEqual(self.initialize.call_count, 1)

    def test_missing_remote_binding_after_unknown_is_not_reinstalled(self):
        self.initialize.side_effect = ValueError('private-value')
        with self.assertRaises(ValueError):
            self.bindings.slot(self.identity)
        self.observe.return_value = False
        with self.assertRaises(ValueError):
            self.bindings.slot(self.identity)
        self.assertEqual(self.initialize.call_count, 1)

    def test_replacement_incarnation_cannot_reuse_original_slot(self):
        self.bindings.slot(self.identity)
        self.readiness.return_value = HostReadiness(
            self.manifest.digest, 'modal-' + self.identity, 'b' * 32, True
        )
        with self.assertRaises(ValueError):
            self.bindings.slot(self.identity)

    def test_foreign_sandbox_cannot_receive_private_launch_token(self):
        self.connect.return_value.object_id = 'sb-other'
        with self.assertRaises(ValueError):
            self.bindings.slot(self.identity)
        self.initialize.assert_not_called()

    def test_missing_cpu_launch_after_initialization_cannot_mint_new_identity(self):
        self.bindings.slot(self.identity)
        path = self.root / self.identity / 'launch.json'
        path.rename(path.with_suffix('.original'))
        with self.assertRaises(ValueError):
            self.bindings.slot(self.identity)
        self.assertFalse(path.exists())
        self.assertEqual(self.initialize.call_count, 1)
