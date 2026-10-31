"""Adopted Modal limits, positive native witnesses and immutable liabilities."""

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from sqlalchemy import insert, select

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.modal_capacity import BACKEND, registration_usage
from studio_platform.repository import (
    BudgetExceeded,
    Conflict,
    Scope,
    budget_reservations,
    instance_intents,
    outbox,
    scaler_actions,
    scaler_receipts,
)
from studio_platform.runtime_catalog import profile_output_delivery
from studio_platform.modal_runtime import PROFILE
import test_modal_admission as fixture
import test_dstack_operator as native_fixture
import test_dstack_registration as registration_fixture


class ModalCapacityTests(fixture.ModalAdmissionFixture):
    def no_create(self):
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        self.assertFalse(self.client.created)
        with self.repo.engine.connect() as c:
            self.assertEqual(c.scalar(select(instance_intents.c.id).limit(1)), None)

    def test_global_and_mode_price_caps_refuse_without_reserving_money(self):
        self.accepted('one')
        self.set_limits(max_hourly_price_microusd=3_000_000)
        self.no_create()
        self.set_limits(
            max_hourly_price_microusd=4_000_000,
            mode_hourly_ceiling_microusd={
                'fl': 850_000,
                'ref': 5_000_000,
                'other': 850_000,
            },
        )
        self.no_create()

    def test_existing_modal_ceiling_counts_in_fleet_hourly_limit(self):
        self.accepted('one')
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.set_limits(fleet_hourly_limit_microusd=7_999_999)
        self.accepted('two')
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        self.assertEqual(len(self.client.created), 1)
        self.set_limits(fleet_hourly_limit_microusd=8_000_000)
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(len(self.client.created), 2)

    def test_positive_old_count_is_replaced_but_global_and_pool_off_are_hard(self):
        self.accepted('one')
        self.accepted('two')
        self.repo.configure_capacity(max_instances=0, max_physical_gpus=0)
        self.no_create()
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(self.pool, max_instances=0, max_physical_gpus=0)
        self.no_create()
        self.repo.configure_pool(self.pool, max_instances=1, max_physical_gpus=1)
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(len(self.client.created), 2)

    def test_missing_or_unknown_native_witness_never_calls_provider(self):
        self.accepted('one')
        self.service.prelaunch_capacity_usage = Mock(return_value={})
        self.no_create()
        original = self.capacity_usage

        def unconfirmed(c):
            return {
                **original(c),
                'observations': [{'reason_code': 'dstack_capacity_unconfirmed'}],
            }

        self.service.prelaunch_capacity_usage = unconfirmed
        self.no_create()

    def test_foreign_absent_node_admits_but_foreign_unconfirmed_refuses(self):
        # #848 nodes from another dstack control plane: absent in the provider
        # account holds no capacity (it blocked every Modal start on staging).
        self.accepted('one')
        original = self.capacity_usage

        def foreign(reason, counted):
            def usage(c):
                value = original(c)
                return {
                    **value,
                    'observations': [
                        *value.get('observations', []),
                        {'reason_code': reason, 'counted': counted},
                    ],
                }

            return usage

        self.service.prelaunch_capacity_usage = foreign(
            'dstack_foreign_unconfirmed', True
        )
        self.no_create()
        self.service.prelaunch_capacity_usage = foreign(
            'dstack_foreign_provider_absent', False
        )
        self.assertEqual(self.admission.tick()['state'], 'starting')

    def test_precreate_rechecks_current_snapshot_and_retains_original_binding(self):
        self.accepted('one')
        with self.repo.transaction() as c:
            _, identity = self.admission._select(c)
        self.set_limits(max_hourly_price_microusd=3_000_000)
        with self.assertRaisesRegex(Conflict, 'capacity_policy_snapshot_changed'):
            self.hosts.start(identity)
        self.assertFalse(self.client.created)
        with self.repo.engine.connect() as c:
            self.assertIsNone(c.scalar(select(scaler_actions.c.intent_id)))
            self.assertEqual(c.scalar(select(instance_intents.c.state)), 'reserved')
            self.assertEqual(
                c.scalar(
                    select(budget_reservations.c.state).where(
                        budget_reservations.c.reference_type == 'instance',
                    )
                ),
                'reserved',
            )
        self.assertEqual(self.admission.tick()['intent_id'], identity)
        self.assertFalse(self.client.created)

    def test_forged_provider_scope_and_snapshot_cannot_adopt_counts(self):
        from studio_platform.capacity_settings import admission_snapshot

        with self.repo.transaction() as c:
            snapshot = admission_snapshot(self.service._admin_limits(c))
            proof = dict(
                capacity_backend=BACKEND,
                lifecycle_version=2,
                tenant_id=self.scope.tenant_id,
                owner_id=self.scope.owner_id,
                project_id=self.scope.project_id,
                pool=self.pool,
                provider='modal',
            )
            values = dict(
                provider='modal',
                hard_deadline=self.now + 600,
                reserved_cost_microusd=1_000_000,
                budget_account_ids=self.profile['budget_account_ids'],
                capacity_policy_snapshot=snapshot,
                capacity_usage=self.capacity_usage(c),
                connection=c,
                dry_run=False,
            )
            for bad in (
                {**proof, 'capacity_backend': 'dstack-v1'},
                {**proof, 'owner_id': 'foreign'},
                {**proof, 'provider': 'vast'},
                {**proof, 'lifecycle_version': True},
            ):
                with self.assertRaisesRegex(
                    ValueError, 'capacity_admission_provenance_invalid'
                ):
                    self.repo.reserve_instance_intent(
                        self.scope,
                        self.pool,
                        'forged',
                        capacity_provenance=bad,
                        **values,
                    )
            stale = copy.deepcopy(snapshot)
            stale['settings_version'] += 1
            values['capacity_policy_snapshot'] = stale
            with self.assertRaisesRegex(Conflict, 'capacity_policy_snapshot_changed'):
                self.repo.reserve_instance_intent(
                    self.scope, self.pool, 'forged', capacity_provenance=proof, **values
                )
        with self.repo.engine.connect() as c:
            self.assertIsNone(c.scalar(select(instance_intents.c.id)))
        self.assertFalse(self.client.created)

    def native_history(self):
        native = SimpleNamespace(
            repo=self.repo,
            scope=Scope('test-tenant', 'superdan', 'project-1'),
            now=self.now,
        )
        native_fixture.DstackOperatorTests._setup_dstack(native)
        native.service.policy.update(
            max_instances=10, max_physical_gpus=10, max_hourly_cost_microusd=10_000_000
        )
        self.repo.configure_pool('dstack-test', max_instances=10, max_physical_gpus=10)
        native.service.provider_inventory = Mock(
            side_effect=lambda backend: {
                'backend': backend,
                'complete': True,
                'observed_at': self.now,
                'instance_ids': [],
            }
        )
        native.start = lambda **kwargs: native_fixture.DstackOperatorTests.start(
            native, **kwargs
        )
        for index in range(5):
            registration_fixture.RegistrationCapacityTests.node(
                native, str(2000100 + index), terminal=True
            )
        self.service.prelaunch_capacity_usage = native.service.prelaunch_capacity_usage
        return native

    def native_ledger(self):
        with self.repo.engine.connect() as c:
            ids = list(
                c.execute(
                    select(instance_intents.c.id).where(
                        instance_intents.c.provider == 'vast'
                    )
                ).scalars()
            )
            return (
                [
                    dict(r)
                    for r in c.execute(
                        select(instance_intents).where(instance_intents.c.id.in_(ids))
                    ).mappings()
                ],
                [
                    dict(r)
                    for r in c.execute(
                        select(budget_reservations).where(
                            budget_reservations.c.reference_id.in_(ids)
                        )
                    ).mappings()
                ],
                self.repo.get_budget('owner-budget'),
            )

    def test_five_native_absence_witnesses_admit_without_releasing_original_money(self):
        self.native_history()
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(self.pool, max_instances=1, max_physical_gpus=1)
        before = self.native_ledger()
        with self.repo.engine.connect() as c:
            self.assertEqual(self.repo._global_usage(c)['instances'], 5)
        self.accepted('one')
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(self.native_ledger(), before)
        with self.repo.engine.connect() as c:
            event = c.execute(
                select(outbox.c.payload).where(
                    outbox.c.event_type == 'instance.reserved',
                    outbox.c.aggregate_id
                    == c.scalar(
                        select(instance_intents.c.id).where(
                            instance_intents.c.provider == 'modal'
                        )
                    ),
                )
            ).scalar_one()
            self.assertEqual(event['capacity_provenance']['capacity_backend'], BACKEND)
            self.assertEqual(event['capacity_provenance']['provider'], 'modal')
            self.assertIsNotNone(event['capacity_policy_snapshot'])

    def test_incomplete_or_stale_native_absence_cannot_authorize_modal(self):
        native = self.native_history()
        self.accepted('one')
        before = self.native_ledger()
        for inventory in (
            {
                'backend': 'vastai',
                'complete': False,
                'observed_at': self.now,
                'instance_ids': [],
            },
            {
                'backend': 'vastai',
                'complete': True,
                'observed_at': self.now - 31,
                'instance_ids': [],
            },
        ):
            native.service.provider_inventory.side_effect = None
            native.service.provider_inventory.return_value = inventory
            self.assertEqual(self.admission.tick()['state'], 'blocked')
            self.assertFalse(self.client.created)
            self.assertEqual(self.native_ledger(), before)

    def test_live_native_allocation_requires_a_positive_gpu_inventory(self):
        native = self.native_history()
        run = next(iter(native.client.runs.values()))
        run['status'] = 'running'
        del run['latest_job_submission']['job_provisioning_data']['instance_type'][
            'resources'
        ]['gpus']
        self.accepted('one')
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        self.assertFalse(self.client.created)

    def test_live_native_and_modal_share_the_same_fleet_price_ceiling(self):
        native = self.native_history()
        next(iter(native.client.runs.values()))['status'] = 'running'
        self.accepted('one')
        self.set_limits(fleet_hourly_limit_microusd=4_749_999)
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        self.assertFalse(self.client.created)
        self.set_limits(fleet_hourly_limit_microusd=4_750_000)
        self.assertEqual(self.admission.tick()['state'], 'starting')

    def registered_spec(self, identity):
        spec = WorkerSpec(
            'modal-' + identity.replace('-', ''),
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
                    intent_id=identity,
                    operation='modal_runtime_bound',
                    observed_at=self.now,
                    facts={
                        'instance_id': spec.instance_id,
                        'manifest_digest': spec.engine_manifest_digest,
                        'runtime_incarnation': 'a' * 32,
                    },
                )
            )
        return spec

    def test_original_modal_registration_fulfills_at_positive_old_global_count(self):
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.accepted('one')
        identity = self.admission.tick()['intent_id']
        self.accepted('two')
        self.admission.tick()
        self.set_limits(max_machines=1, max_hourly_price_microusd=3_000_000)
        spec = self.registered_spec(identity)
        before = self.repo.get_budget(self.profile['budget_account_ids'][0])
        control = WorkerControl(
            self.repo,
            capacity_usage=lambda c, s: registration_usage(
                self.service, self.hosts, c, s
            ),
        )
        self.assertEqual(control.register(spec)['id'], spec.worker_id)
        self.assertEqual(
            self.repo.get_budget(self.profile['budget_account_ids'][0]), before
        )
        with self.repo.engine.connect() as c:
            observation = registration_usage(self.service, self.hosts, c, spec)
            self.assertEqual(
                observation['capacity_admission']['capacity_backend'], BACKEND
            )

    def test_registration_refuses_extra_device_foreign_binding_and_exited_host(self):
        self.accepted('one')
        identity = self.admission.tick()['intent_id']
        spec = self.registered_spec(identity)
        for altered in (
            replace(spec, physical_gpu_ids=('gpu0', 'gpu1')),
            replace(spec, worker_id='foreign'),
            replace(spec, configuration_id='foreign'),
        ):
            with self.repo.engine.connect() as c, self.assertRaises(BudgetExceeded):
                registration_usage(self.service, self.hosts, c, altered)
        self.client.hosts['inkseq-' + identity]['exit_code'] = 137
        with self.repo.engine.connect() as c, self.assertRaises(BudgetExceeded):
            registration_usage(self.service, self.hosts, c, spec)

    def test_registration_keeps_global_and_pool_disabled_gates(self):
        self.accepted('one')
        identity = self.admission.tick()['intent_id']
        spec = self.registered_spec(identity)
        control = WorkerControl(
            self.repo,
            capacity_usage=lambda c, s: registration_usage(
                self.service, self.hosts, c, s
            ),
        )
        self.repo.configure_capacity(max_instances=0, max_physical_gpus=0)
        with self.assertRaisesRegex(BudgetExceeded, 'global_capacity_disabled'):
            control.register(spec)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(self.pool, max_instances=0, max_physical_gpus=0)
        with self.assertRaisesRegex(BudgetExceeded, 'pool_disabled'):
            control.register(spec)
