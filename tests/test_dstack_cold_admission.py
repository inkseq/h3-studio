"""Original cold-job admission over stopped history; no provider operations."""

import copy
import uuid
from unittest.mock import patch

from sqlalchemy import func, insert, select, update

from studio_platform.dstack_demand import profile_hash
from studio_platform.dstack_demand_policy import policy_key
from studio_platform.dstack_operator import DstackOperator
from studio_platform.operator_capacity import operator_policy
from studio_platform.repository import (
    Conflict,
    Scope,
    attempts,
    budget_reservations,
    jobs,
    request_hash,
)
from studio_platform.runtime_catalog import profile_output_delivery
from test_platform_repository import LedgerCase
import test_dstack_operator as original


class ColdAdmissionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        original.DstackOperatorTests._setup_dstack(self)
        self.config['project'] = 'sixnine'
        self.config['policy'].update(
            lifecycle_version=2,
            reservation_seconds=3600,
            expires_at=None,
            demand_start={'profile_ids': ['5090-fl']},
        )
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        self.profile = self.service.profiles['5090-fl']
        self.compiled = dict(
            deployment_profile_id=self.profile['runtime_profile_id'],
            recipe_id=self.profile['recipe_id'],
            request=dict(model=self.profile['model_id'], mode='fl'),
        )
        self.execution = dict(
            backend='wangp-worker',
            dispatch_backend='hatchet-v1',
            enabled=True,
            quote_known=True,
            admission_state='queued',
            pool=self.profile['pool'],
            configuration_id=self.profile['configuration_id'],
            engine_manifest_digest=self.profile['manifest_digest'],
            output_delivery=profile_output_delivery(self.profile['runtime_profile_id']),
            expected_runtime_s=120,
            policy_hash=request_hash('current-policy'),
        )
        self.execution['demand_start'] = dict(
            version=1,
            profile_id=self.profile['id'],
            profile_hash=profile_hash(self.config, self.profile),
            policy_hash=self.execution['policy_hash'],
            test_task_id=None,
        )
        protected = patch(
            'studio_platform.dstack_demand._protected_configuration',
            return_value=self.config,
        )
        protected.start()
        self.addCleanup(protected.stop)
        with self.repo.transaction() as connection:
            connection.execute(
                insert(operator_policy).values(
                    id=policy_key(self.scope.tenant_id),
                    version=1,
                    payload=dict(tenant_id=self.scope.tenant_id, enabled=True),
                    actor='test-admin',
                    updated_at=self.now,
                )
            )
        self.repo.configure_budget(
            'history-budget',
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=100,
        )
        history = self.repo.reserve_instance_intent(
            self.scope,
            self.profile['pool'],
            'old-machine',
            provider='test-only',
            physical_gpus=1,
            slots=1,
            hard_deadline=self.now + 1000,
            reserved_cost_microusd=1,
            budget_account_ids=('history-budget',),
            dry_run=False,
        )
        for state in ('creating', 'starting', 'draining', 'destroying', 'destroyed'):
            self.repo.update_instance(
                history['id'],
                state,
                destruction_confirmed=state == 'destroyed',
                **(
                    {'provider_instance_id': 'old-terminated-instance'}
                    if state == 'starting'
                    else {}
                ),
            )

    def plan(self, *, execution=None, scope=None):
        return self.repo.create_plan(
            scope or self.scope,
            self.compiled,
            execution or self.execution,
            expires_at=self.now + 900,
            estimated_cost_microusd=100000,
        )

    def assert_one_admission(self, job):
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(jobs)), 1
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(attempts)), 0
            )
            holds = list(
                connection.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_type == 'job',
                        budget_reservations.c.reference_id == job['id'],
                    )
                ).mappings()
            )
        self.assertEqual(len(holds), 1)
        self.assertEqual(
            (holds[0]['state'], holds[0]['amount_microusd']), ('reserved', 100000)
        )
        self.assertEqual(
            self.repo.get_budget('owner-budget')['reserved_microusd'], 100000
        )
        self.assertEqual(self.client.applied, [])

    def test_direct_cold_create_preserves_one_original_job_and_hold(self):
        plan = self.plan()
        job = self.repo.create_job(
            self.scope,
            plan['id'],
            'direct-cold',
            budget_account_ids=('owner-budget',),
        )
        again = self.repo.create_job(
            self.scope,
            plan['id'],
            'direct-cold',
            budget_account_ids=('owner-budget',),
        )
        self.assertEqual(job['id'], again['id'])
        self.assertEqual(job['status'], 'queued')
        self.assert_one_admission(job)

    def test_quick_chat_planned_enqueue_retains_original_job_and_one_hold(self):
        scope = Scope(
            self.scope.tenant_id,
            self.scope.owner_id,
            self.scope.project_id,
            'quick-chat-execution',
        )
        plan = self.plan(scope=scope)
        planned = self.repo.create_job(
            scope, plan['id'], 'original-execution', initial_status='planned'
        )
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)
        job = self.repo.enqueue(
            scope, planned['id'], budget_account_ids=('owner-budget',)
        )
        replay = self.repo.create_job(
            scope, plan['id'], 'original-execution', initial_status='planned'
        )
        self.assertEqual(job['id'], planned['id'])
        self.assertEqual(replay['id'], planned['id'])
        self.assertEqual(job['status'], 'queued')
        self.assert_one_admission(job)

    def test_malformed_or_mismatched_marker_and_runtime_off_cannot_bypass_drain(self):
        original_marker = self.execution['demand_start']
        markers = (
            {},
            {**original_marker, 'version': True},
            {**original_marker, 'profile_id': 'another-profile'},
            {**original_marker, 'profile_hash': '0' * 64},
            {**original_marker, 'policy_hash': '0' * 64},
            {**original_marker, 'test_task_id': 'another-task'},
            {**original_marker, 'extra': True},
        )
        for marker in markers:
            with self.subTest(marker=marker):
                execution = {**self.execution, 'demand_start': marker}
                plan = self.plan(execution=execution)
                with self.assertRaisesRegex(Conflict, 'capacity_drain_repreflight'):
                    self.repo.create_job(self.scope, plan['id'], uuid.uuid4().hex)
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_policy)
                .where(operator_policy.c.id == policy_key(self.scope.tenant_id))
                .values(payload=dict(tenant_id=self.scope.tenant_id, enabled=False))
            )
        plan = self.plan()
        with self.assertRaisesRegex(Conflict, 'capacity_drain_repreflight'):
            self.repo.create_job(self.scope, plan['id'], 'disabled')
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 0)
        self.assertEqual(self.client.applied, [])

    def test_cold_marker_cannot_bypass_private_warm_binding_or_legacy_warm_guard(self):
        plan = self.plan()
        with patch(
            'studio_platform.capacity.pool_members_require_warm_binding',
            return_value=True,
        ):
            with self.assertRaisesRegex(Conflict, 'capacity_drain_repreflight'):
                self.repo.create_job(self.scope, plan['id'], 'private-warm')
        for field in ('capacity_binding', 'capacity_approval_id'):
            execution = copy.deepcopy(self.execution)
            execution[field] = 'old-private-reference'
            plan = self.plan(execution=execution)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(Conflict, 'capacity_drain_repreflight'),
            ):
                self.repo.create_job(self.scope, plan['id'], field)
        execution = {k: v for k, v in self.execution.items() if k != 'demand_start'}
        plan = self.plan(execution=execution)
        with self.assertRaisesRegex(Conflict, 'capacity_drain_repreflight'):
            self.repo.create_job(self.scope, plan['id'], 'legacy-warm')
