"""Exit projection over real registered devices; provider operations stay fake."""

import copy
from types import MethodType, SimpleNamespace
from unittest.mock import patch
import uuid

from sqlalchemy import insert, select, update

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.dstack_capacity import live_capacity_usage
from studio_platform.dstack_operator import DstackOperator
from studio_platform.modal_capacity import registration_usage
from studio_platform.native_capacity_inventory import prelaunch_usage, resource_overlay
from studio_platform.repository import (
    budget_reservations,
    instance_intents,
    registered_devices,
    registered_workers,
    request_hash,
    scaler_actions,
    scaler_receipts,
)
from studio_platform.runtime_catalog import profile_output_delivery
from studio_platform.modal_runtime import PROFILE
from test_modal_admission import ModalAdmissionFixture


class ModalExitCapacityTests(ModalAdmissionFixture):
    def setUp(self):
        super().setUp()
        self.service._observed_registration_usage = lambda _: (
            live_capacity_usage(SimpleNamespace(), [], clock=self.repo.clock),
            {},
        )
        self.service._registration_resource_overlay = MethodType(
            DstackOperator._registration_resource_overlay, self.service
        )
        self.service.prelaunch_capacity_usage = lambda c: prelaunch_usage(
            self.service, c
        )
        self.accepted('one')
        self.identity = self.admission.tick()['intent_id']
        self.spec = WorkerSpec(
            'modal-' + self.identity.replace('-', ''),
            self.pool,
            'modal',
            'sb-1',
            ('gpu0',),
            (self.manifest.document['generation_recipe_id'],),
            self.manifest.document['model_id'],
            'test-v7',
            'wangp-worker',
            self.manifest.digest,
            profile_output_delivery(PROFILE),
            'hatchet-v1',
        )
        with self.repo.transaction() as c:
            c.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=self.identity,
                    operation='modal_runtime_bound',
                    observed_at=self.now,
                    facts={
                        'instance_id': self.spec.instance_id,
                        'manifest_digest': self.spec.engine_manifest_digest,
                        'runtime_incarnation': 'a' * 32,
                    },
                )
            )
        WorkerControl(
            self.repo,
            capacity_usage=lambda c, s: registration_usage(
                self.service, self.hosts, c, s
            ),
        ).register(self.spec)
        self.work = []

    def exit(self):
        self.client.hosts['inkseq-' + self.identity]['exit_code'] = 0
        self.assertEqual(self.hosts.observe(self.identity)['state'], 'destroyed')

    def original_rows(self):
        with self.repo.engine.connect() as c:
            return {
                'intent': dict(
                    c.execute(
                        select(instance_intents).where(
                            instance_intents.c.id == self.identity
                        )
                    )
                    .mappings()
                    .one()
                ),
                'worker': dict(
                    c.execute(
                        select(registered_workers).where(
                            registered_workers.c.id == self.spec.worker_id
                        )
                    )
                    .mappings()
                    .one()
                ),
                'device': dict(
                    c.execute(
                        select(registered_devices).where(
                            registered_devices.c.worker_id == self.spec.worker_id
                        )
                    )
                    .mappings()
                    .one()
                ),
                'reservations': [
                    dict(row)
                    for row in c.execute(
                        select(budget_reservations).where(
                            budget_reservations.c.reference_id == self.identity
                        )
                    ).mappings()
                ],
                'receipts': [
                    dict(row)
                    for row in c.execute(
                        select(scaler_receipts).where(
                            scaler_receipts.c.intent_id == self.identity
                        )
                    ).mappings()
                ],
            }

    def test_registered_exit_admits_next_job_without_rewriting_history_or_money(self):
        self.exit()
        before = self.original_rows()
        with self.repo.engine.connect() as c:
            self.assertEqual(prelaunch_usage(self.service, c)['resources'], {})
            self.assertEqual(self.repo._global_usage(c)['physical_gpus'], 1)
        self.accepted('two')
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(len(self.client.created), 2)
        self.assertEqual(self.original_rows(), before)
        self.assertEqual(before['device']['state'], 'owned')
        self.assertTrue(all(r['state'] == 'reserved' for r in before['reservations']))

    def test_registration_overlay_reuses_native_read_and_matches_prelaunch(self):
        self.exit()
        with self.repo.engine.connect() as c:
            expected = prelaunch_usage(self.service, c)
            intents = {
                row['id']: dict(row)
                for row in c.execute(select(instance_intents)).mappings()
            }
            usage, runs = self.service._observed_registration_usage([])
            with (
                patch.object(self.service, '_observed_registration_usage') as observe,
                patch.object(
                    self.service,
                    '_registration_resource_overlay',
                    wraps=self.service._registration_resource_overlay,
                ) as raw,
            ):
                result = resource_overlay(self.service, c, [], intents, usage, runs)
            observe.assert_not_called()
            raw.assert_called_once_with(c, [], intents, usage, runs)
            self.assertEqual(result, expected)

    def assert_occupied(self):
        with self.repo.engine.connect() as c:
            self.assertGreaterEqual(
                prelaunch_usage(self.service, c)['physical_gpus'], 1
            )
        self.accepted('two')
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        self.assertEqual(len(self.client.created), 1)

    def test_destroyed_status_and_stop_marker_without_exit_are_not_proof(self):
        with self.repo.transaction() as c:
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.identity)
                .values(state='destroyed')
            )
            c.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == self.identity)
                .values(destroy_started_at=self.now)
            )
        self.assert_occupied()

    def test_wrong_instance_or_uncertain_exit_retains_device(self):
        self.exit()
        for facts in (
            {'id': 'sb-wrong', 'exit_code': 0},
            {'id': 'sb-1', 'exit_code': None},
            {'id': 'sb-1', 'exit_code': False},
        ):
            with self.subTest(facts=facts):
                with self.repo.transaction() as c:
                    c.execute(
                        update(scaler_receipts)
                        .where(
                            scaler_receipts.c.intent_id == self.identity,
                            scaler_receipts.c.operation == 'observed',
                        )
                        .values(facts=facts)
                    )
                with self.repo.engine.connect() as c:
                    self.assertEqual(
                        prelaunch_usage(self.service, c)['physical_gpus'], 1
                    )
        self.assert_occupied()

    def test_invalid_runtime_binding_cannot_exclude_device(self):
        self.exit()
        with self.repo.transaction() as c:
            row = (
                c.execute(
                    select(scaler_receipts).where(
                        scaler_receipts.c.intent_id == self.identity,
                        scaler_receipts.c.operation == 'modal_runtime_bound',
                    )
                )
                .mappings()
                .one()
            )
            c.execute(
                update(scaler_receipts)
                .where(scaler_receipts.c.id == row['id'])
                .values(facts={**row['facts'], 'runtime_incarnation': 'uncertain'})
            )
        self.assert_occupied()

    def test_foreign_owned_device_on_same_or_other_host_keeps_counting(self):
        self.exit()
        with self.repo.transaction() as c:
            worker = dict(
                c.execute(
                    select(registered_workers).where(
                        registered_workers.c.id == self.spec.worker_id
                    )
                )
                .mappings()
                .one()
            )
            for instance, gpu in (('sb-1', 'gpu1'), ('sb-foreign', 'gpu0')):
                foreign = copy.deepcopy(worker)
                foreign['id'] = 'foreign-' + instance
                foreign['instance_id'] = instance
                foreign['spec'].update(
                    worker_id=foreign['id'],
                    instance_id=instance,
                    physical_gpu_ids=[gpu],
                )
                foreign['spec_hash'] = request_hash(foreign['spec'])
                c.execute(insert(registered_workers).values(**foreign))
                c.execute(
                    insert(registered_devices).values(
                        provider='modal',
                        instance_id=instance,
                        gpu_id=gpu,
                        worker_id=foreign['id'],
                        state='owned',
                    )
                )
        with self.repo.engine.connect() as c:
            self.assertEqual(
                prelaunch_usage(self.service, c)['resources'],
                {
                    ('modal', 'sb-1'): 1,
                    ('modal', 'sb-foreign'): 1,
                },
            )
        self.assert_occupied()
