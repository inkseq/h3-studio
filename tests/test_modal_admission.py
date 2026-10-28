"""Fake provider admission against the original job, capacity and money ledger."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import select

from studio_platform.modal_admission import ModalAdmission
from studio_platform.modal_host import HostSpec, ModalHosts
from studio_platform.repository import Scope, instance_intents
from studio_platform.runtime_catalog import engine_manifest
from test_modal_budget import GUARD
from test_modal_host import FakeModal
from test_platform_repository import LedgerCase
from studio_platform.modal_runtime import PROFILE


class ModalAdmissionTests(LedgerCase):
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
        self.service = SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(tenant_id='sixnine'),
            policy={
                'test_gpu_guard': GUARD,
                'demand_start': {'test_task_id': GUARD['task_id']},
            },
            _admin_limits=Mock(
                return_value={
                    'value': {
                        'test_task_limit_microusd': 50_000_000,
                        'max_runtime_seconds': 600,
                    }
                }
            ),
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
                {'return_value': {'runtime_revision': 'original'}},
            ),
        ):
            p = patch(target, **options)
            p.start()
            self.addCleanup(p.stop)

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

    def test_two_unresolved_hosts_block_third_and_release_no_funding(self):
        for key in ('one', 'two', 'three'):
            self.accepted(key)
        self.admission.tick()
        self.admission.tick()
        self.assertEqual(self.admission.tick()['reason_code'], 'modal_container_limit')
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
        self.service._admin_limits.return_value['value']['test_task_limit_microusd'] = 1
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        with self.repo.engine.connect() as c:
            self.assertEqual(list(c.execute(select(instance_intents))), [])
        self.assertFalse(self.client.created)

    def test_paid_profile_and_manifest_cannot_silently_change(self):
        job = self.accepted('one')
        job['request']['deployment_profile_id'] = 'legacy'
        self.assertEqual(self.admission.tick()['state'], 'blocked')
        self.assertFalse(self.client.created)
