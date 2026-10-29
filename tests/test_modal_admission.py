"""Fake provider admission against the original job, capacity and money ledger."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import insert, select, update

from studio_platform.capacity_settings import admission_snapshot, read_policy
from studio_platform.control import effective_registration_limits

from studio_platform.modal_admission import ModalAdmission
from studio_platform.modal_host import HostSpec, ModalHosts
from studio_platform.repository import (
    Scope,
    budget_reservations,
    capacity_gate,
    instance_intents,
    pool_limits,
    registered_workers,
    scaler_receipts,
    spending_settings,
)
from studio_platform.runtime_catalog import engine_manifest
from test_modal_budget import GUARD
from test_modal_host import FakeModal
from test_platform_repository import LedgerCase
from studio_platform.modal_runtime import PROFILE
from test_capacity_admission import adopt


class ModalAdmissionFixture(LedgerCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope('sixnine', 'superdan', 'project-inkseq-staging-superdan')
        self.pool = 'inkseq-staging-v7-fl'
        self.repo.configure_pool(self.pool, max_instances=10, max_physical_gpus=10)
        self.repo.configure_budget(
            GUARD['budget_account_id'], tenant_id='sixnine', limit_microusd=50_000_000
        )
        self.client = FakeModal()
        self.hosts = ModalHosts(
            self.repo,
            self.client,
            HostSpec('ap-test', 'im-test', 'vo-test', 4_000_000),
            environment='staging',
            enabled=True,
        )
        manifest = engine_manifest(PROFILE, 'fl')
        self.profile = dict(
            id='staging-v7-fl',
            pool=self.pool,
            owner_id='superdan',
            project_id=self.scope.project_id,
            runtime_profile_id=PROFILE,
            mode='fl',
            budget_account_ids=[GUARD['budget_account_id']],
        )
        self.work = []
        adopt(self, maximum=10)
        self.set_limits(
            max_hourly_price_microusd=4_000_000,
            mode_hourly_ceiling_microusd={
                'fl': 5_000_000,
                'ref': 5_000_000,
                'other': 850_000,
            },
            max_runtime_seconds=600,
            test_task_limit_microusd=50_000_000,
        )
        self.service = SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(tenant_id='sixnine'),
            policy={
                'test_gpu_guard': GUARD,
                'demand_start': {'test_task_id': GUARD['task_id']},
            },
            _admin_limits=Mock(side_effect=lambda c: read_policy(c, 'sixnine')),
            profiles={self.profile['id']: self.profile},
            prelaunch_capacity_usage=self.capacity_usage,
            prelaunch_capacity_limits=self.capacity_limits,
        )
        self.admission = ModalAdmission(
            self.service, self.hosts, environment='staging', enabled=True
        )
        self.manifest = manifest
        for target, options in (
            (
                'studio_platform.dstack_demand.eligible_jobs',
                {'side_effect': lambda *_: self.work},
            ),
            (
                'studio_platform.dstack_demand.validate_start',
                {
                    'return_value': {
                        'runtime_revision': 'original',
                        'profile_id': self.profile['id'],
                    }
                },
            ),
        ):
            p = patch(target, **options)
            p.start()
            self.addCleanup(p.stop)

    def set_limits(self, **changes):
        with self.repo.transaction() as c:
            row = (
                c.execute(
                    select(spending_settings).where(
                        spending_settings.c.tenant_id == 'sixnine',
                    )
                )
                .mappings()
                .one()
            )
            c.execute(
                update(spending_settings)
                .where(
                    spending_settings.c.tenant_id == 'sixnine',
                )
                .values(value={**row['value'], **changes}, version=row['version'] + 1)
            )

    def capacity_usage(self, c):
        value = self.repo._global_usage(c)
        active = list(
            c.execute(
                select(instance_intents.c.id).where(
                    instance_intents.c.state != 'destroyed',
                )
            ).scalars()
        )
        return {
            **value,
            'active_instance_ids': active,
            'observations': [],
            'hourly_cost_microusd': 0,
            'native_resources': (),
            'unconfirmed_allocation_ids': (),
        }

    def capacity_limits(self, c, *, profile, for_demand):
        self.assertTrue(for_demand)
        self.assertIs(profile, self.profile)
        return effective_registration_limits(
            c.execute(select(capacity_gate)).mappings().one(),
            c.execute(select(pool_limits).where(pool_limits.c.pool == self.pool))
            .mappings()
            .one(),
            snapshot=admission_snapshot(read_policy(c, 'sixnine')),
        )

    def accepted(self, key):
        plan = self.repo.create_plan(
            self.scope,
            {
                'deployment_profile_id': PROFILE,
                'recipe_id': self.manifest.document['generation_recipe_id'],
                'request': {'mode': 'fl', 'model': 'model'},
            },
            {
                'pool': self.pool,
                'expected_runtime_s': 120,
                'configuration_id': 'test-v7',
                'engine_manifest_digest': self.manifest.digest,
            },
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        job = self.repo.create_job(self.scope, plan['id'], key)
        job = self.repo.get_job(self.scope, job['id'])
        self.work.append((job, self.profile))
        return job


class ModalAdmissionTests(ModalAdmissionFixture):
    def test_default_off_and_production_never_select_or_create(self):
        self.accepted('one')
        for options in ({}, {'environment': 'production', 'enabled': True}):
            self.assertEqual(
                ModalAdmission(self.service, self.hosts, **options).tick()['state'],
                'disabled',
            )
        self.assertFalse(self.client.created)

    def test_one_original_job_is_never_reallocated_on_repeated_ticks(self):
        original = copy.deepcopy(self.accepted('one'))
        first = self.admission.tick()
        self.assertEqual(first['state'], 'starting')
        self.now += 1
        self.assertEqual(self.admission.tick()['intent_id'], first['intent_id'])
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.repo.get_job(self.scope, original['id']), original)

    def test_unknown_create_is_observed_not_replayed(self):
        self.accepted('one')
        self.client.drop_create = True
        self.assertEqual(self.admission.tick()['state'], 'creation_unknown')
        self.admission.tick()
        self.assertEqual(len(self.client.created), 1)

    def test_terminal_original_host_does_not_create_an_automatic_replacement(self):
        self.accepted('one')
        original = self.admission.tick()['intent_id']
        self.hosts.stop(original)
        self.hosts.observe(original)
        self.assertEqual(self.admission.tick()['state'], 'destroyed')
        self.assertEqual(len(self.client.created), 1)

    def test_vast_skips_a_job_while_its_modal_host_lives(self):
        # #784: one provider per job; Vast takes over once the Modal host is gone.
        from studio_platform.dstack_demand_start import modal_covered_jobs

        job = self.accepted('one')
        with self.repo.transaction() as c:
            self.assertEqual(modal_covered_jobs(c, self.now), set())
        intent = self.admission.tick()['intent_id']
        with self.repo.transaction() as c:
            self.assertEqual(modal_covered_jobs(c, self.now), {job['id']})
            # A start that never becomes ready stops holding the job back.
            self.assertEqual(modal_covered_jobs(c, self.now + 601), set())
        from studio_platform.dstack_demand_start import modal_machines

        with self.repo.transaction() as c:
            (machine,) = modal_machines(c, self.now)
            self.assertEqual((machine.identity, machine.state), (intent, 'starting'))
            self.assertEqual(modal_machines(c, self.now + 601), ())
        self.hosts.stop(intent)
        self.hosts.observe(intent)
        with self.repo.transaction() as c:
            self.assertEqual(modal_covered_jobs(c, self.now), set())
            self.assertEqual(modal_machines(c, self.now), ())

    def test_two_unresolved_hosts_block_third_and_release_no_funding(self):
        for key in ('one', 'two', 'three'):
            self.accepted(key)
        self.admission.tick()
        self.admission.tick()
        self.assertEqual(self.admission.tick()['reason_code'], 'modal_container_limit')
        self.assertEqual(len(self.client.created), 2)

    def test_starting_host_covers_one_new_input_after_original_leaves_queue(self):
        self.accepted('one')
        self.admission.tick()
        self.work.clear()
        self.accepted('two')
        self.assertEqual(self.admission.tick()['state'], 'idle')
        self.assertEqual(len(self.client.created), 1)
        self.accepted('three')
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(len(self.client.created), 2)

    def test_incompatible_start_does_not_cover_other_configuration(self):
        self.accepted('one')
        self.admission.tick()
        self.work.clear()
        job = self.accepted('two')
        job['execution_plan']['configuration_id'] = 'other-qualified-configuration'
        self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(len(self.client.created), 2)

    def test_ready_idle_host_is_reused_but_expired_host_is_not(self):
        self.accepted('one')
        original = self.admission.tick()['intent_id']
        self.repo.update_instance(original, 'ready')
        self.work.clear()
        self.accepted('two')
        with self.repo.transaction() as c:
            c.execute(
                insert(registered_workers).values(
                    id='modal-worker',
                    pool=self.pool,
                    provider='modal',
                    instance_id='sb-1',
                    spec={},
                    spec_hash='test',
                    state='ready',
                    current_job_id=None,
                    fence=1,
                    expires_at=self.now + 30,
                    updated_at=self.now,
                )
            )
        # Partial compatibility fixture: full consumer/runtime proofs are
        # exercised through real DurableSlots in test_modal_dispatch.
        with (
            patch(
                'studio_platform.modal_admission.WorkerControl.matches',
                return_value=True,
            ),
            patch('studio_platform.modal_dispatch.consumer_reason', return_value=None),
        ):
            self.assertEqual(self.admission.tick()['state'], 'idle')
            self.assertEqual(len(self.client.created), 1)
            self.now += 31
            self.assertEqual(self.admission.tick()['state'], 'starting')
        self.assertEqual(len(self.client.created), 2)

    def test_ready_dedicated_blocks_modal_without_changing_the_vast_planner(self):
        self.accepted('one')
        with patch(
            'studio_platform.modal_admission.dedicated_ready', return_value=True
        ):
            self.assertEqual(self.admission.tick()['state'], 'idle')
        self.assertFalse(self.client.created)

    def test_changed_original_demand_gate_leaves_no_intent_or_provider_call(self):
        from studio_platform.repository import Conflict

        self.accepted('one')
        with patch(
            'studio_platform.dstack_demand.validate_start',
            side_effect=Conflict('dstack_demand_disabled'),
        ):
            self.assertEqual(self.admission.tick()['state'], 'blocked')
        with self.repo.engine.connect() as c:
            self.assertEqual(list(c.execute(select(instance_intents))), [])
        self.assertFalse(self.client.created)

    def test_shared_limit_rolls_back_reservation_before_provider_io(self):
        self.accepted('one')
        self.set_limits(test_task_limit_microusd=1)
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        with self.repo.engine.connect() as c:
            self.assertEqual(list(c.execute(select(instance_intents))), [])
        self.assertFalse(self.client.created)

    def test_paid_profile_and_manifest_cannot_silently_change(self):
        job = self.accepted('one')
        job['request']['deployment_profile_id'] = 'legacy'
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        self.assertFalse(self.client.created)


class ProductionModalAdmissionTests(ModalAdmissionFixture):
    """#688: production Modal backstop funded by a production account."""

    def setUp(self):
        super().setUp()
        self.scope = Scope('sixnine', 'superdan', 'production-h3-acceptance')
        self.pool = 'inkseq-production-v7-fl'
        self.repo.configure_pool(self.pool, max_instances=10, max_physical_gpus=10)
        self.repo.configure_budget(
            'production-gpu', tenant_id='sixnine', limit_microusd=50_000_000
        )
        # The id stays: the patched original demand gate returns it.
        self.profile.update(
            pool=self.pool,
            project_id=self.scope.project_id,
            budget_account_ids=['production-gpu'],
        )
        self.service.profiles = {self.profile['id']: self.profile}
        self.hosts = ModalHosts(
            self.repo,
            self.client,
            HostSpec(
                'ap-test',
                'im-test',
                'vo-test',
                4_000_000,
                app_name='inkseq-production-worker',
            ),
            environment='production',
            enabled=True,
        )
        # Vast per-machine ceilings stay below a Modal all-resource quote.
        self.set_limits(
            max_hourly_price_microusd=3_000_000,
            mode_hourly_ceiling_microusd={
                'fl': 1_200_000,
                'ref': 3_000_000,
                'other': 850_000,
            },
            max_runtime_seconds=7200,
        )
        self.admission = ModalAdmission(
            self.service,
            self.hosts,
            environment='production',
            enabled=True,
            budget_account_id='production-gpu',
        )

    def test_production_needs_its_account_and_matching_hosts(self):
        for options in (
            {'environment': 'production', 'enabled': True},
            {
                'environment': 'staging',
                'enabled': True,
                'budget_account_id': 'production-gpu',
            },
        ):
            self.assertEqual(
                ModalAdmission(self.service, self.hosts, **options).tick()['state'],
                'disabled',
            )

    def test_customer_job_binds_a_production_guard_on_its_account(self):
        from studio_platform.modal_budget import binding

        self.accepted('one')
        first = self.admission.tick()
        self.assertEqual(first['state'], 'starting')
        with self.repo.transaction() as c:
            _, value = binding(c, first['intent_id'])
            reservations = list(
                c.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_id == first['intent_id']
                    )
                ).mappings()
            )
        self.assertEqual(value['guard']['version'], 3)
        self.assertEqual(value['guard']['budget_account_id'], 'production-gpu')
        self.assertEqual(
            {row['account_id'] for row in reservations}, {'production-gpu'}
        )
        self.assertEqual(len(self.client.created), 1)

    def test_staging_test_projects_never_take_the_production_path(self):
        self.profile['project_id'] = 'project-inkseq-staging-superdan'
        self.accepted('one')
        self.assertEqual(
            self.admission.tick()['reason_code'], 'modal_admission_refused'
        )
        self.assertFalse(self.client.created)

    def test_backstop_waits_for_vast_before_starting_modal(self):
        self.admission.start_after_seconds = 300
        job = self.accepted('one')
        self.now = job['created_at'] + 299
        self.assertEqual(self.admission.tick()['state'], 'idle')
        self.now = job['created_at'] + 300
        self.assertEqual(self.admission.tick()['state'], 'starting')

    def test_production_guard_never_names_the_staging_test_account(self):
        from studio_platform.dstack_test_budget import ACCOUNT
        from studio_platform.modal_budget import production_guard

        with self.assertRaises(ValueError):
            production_guard(ACCOUNT, max_runtime=7200)
        with self.assertRaises(ValueError):
            production_guard('production-gpu', max_runtime=7201)


class ModalProfileTests(ModalAdmissionFixture):
    """v7 -> v8 switch: one Modal image serves one runtime profile."""

    def test_receipts_before_v8_mean_v7_and_unknown_profiles_fail(self):
        from studio_platform.modal_runtime import V7, V8, selected_profile

        self.assertEqual(selected_profile({}), V7)
        self.assertEqual(selected_profile({'profile': V8}), V8)
        with self.assertRaises(ValueError):
            selected_profile({'profile': 'something-else'})
        with self.assertRaises(ValueError):
            HostSpec('ap-test', 'im-test', 'vo-test', 4_000_000, profile='unknown')

    def test_a_v8_host_never_takes_a_v7_job(self):
        from studio_platform.modal_runtime import V8

        hosts = ModalHosts(
            self.repo,
            self.client,
            HostSpec('ap-test', 'im-test', 'vo-test', 4_000_000, profile=V8),
            environment='staging',
            enabled=True,
        )
        admission = ModalAdmission(
            self.service, hosts, environment='staging', enabled=True
        )
        self.accepted('one')
        self.assertEqual(admission.tick()['reason_code'], 'modal_admission_refused')
        self.assertFalse(self.client.created)

    def test_v7_admission_records_its_profile(self):
        from studio_platform.modal_runtime import V7

        self.accepted('one')
        intent = self.admission.tick()['intent_id']
        with self.repo.transaction() as c:
            facts = c.execute(
                select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.intent_id == intent,
                    scaler_receipts.c.operation == 'modal_demand_bound',
                )
            ).scalar_one()
        self.assertEqual(facts['profile'], V7)
