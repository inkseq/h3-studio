"""Real-ledger supplier-only projection and first-admission safety checks."""

import copy
from datetime import datetime, timezone
import unittest
import uuid

from sqlalchemy import insert, select, update

from studio_platform.operator_capacity import operator_nodes, operator_commands
from studio_platform.capacity_settings import DEFAULTS
from studio_platform.repository import (
    BudgetExceeded,
    NotFound,
    Scope,
    budget_accounts,
    budget_reservations,
    instance_intents,
    jobs,
    spending_settings,
    plans,
)
from studio_platform.spending_limits import ADMIN_OWNER
from studio_platform.supplier_spending import admit, projection
from test_platform_repository import LedgerCase


class SupplierSpendingTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc).timestamp()
        self.policy()

    def policy(self, *, limit=1_000_000, adopted=True):
        with self.repo.transaction() as c:
            old = (
                c.execute(
                    select(spending_settings).where(
                        spending_settings.c.tenant_id == self.scope.tenant_id,
                        spending_settings.c.owner_id == ADMIN_OWNER,
                    )
                )
                .mappings()
                .first()
            )
            value = copy.deepcopy(DEFAULTS)
            value['tenant_daily_limit_microusd'] = limit
            if not adopted:
                value.pop('capacity_policy_version')
            fields = dict(
                value=value,
                version=old['version'] + 1 if old else 1,
                updated_at=self.now,
                actor_id='admin-test',
            )
            if old:
                c.execute(
                    update(spending_settings)
                    .where(
                        spending_settings.c.tenant_id == self.scope.tenant_id,
                        spending_settings.c.owner_id == ADMIN_OWNER,
                    )
                    .values(**fields)
                )
            else:
                c.execute(
                    insert(spending_settings).values(
                        tenant_id=self.scope.tenant_id, owner_id=ADMIN_OWNER, **fields
                    )
                )

    def rental(
        self,
        amount=100_000,
        *,
        accounts=('owner-budget',),
        state='reserved',
        actual=None,
        created=None,
        settled=None,
        identity=None,
    ):
        reference = str(uuid.uuid4())
        created = self.now - 100 if created is None else created
        with self.repo.transaction() as c:
            c.execute(
                insert(instance_intents).values(
                    id=reference,
                    intent_key=reference,
                    pool='rental-pool',
                    request_hash='a' * 64,
                    state='destroyed'
                    if state in {'settled', 'released'}
                    else 'running',
                    physical_gpus=1,
                    slots=1,
                    reserved_cost_microusd=amount,
                    provider='vast',
                    provider_instance_id=identity,
                    hard_deadline=self.now + 600,
                    created_at=created,
                    updated_at=self.now,
                )
            )
            for account in accounts:
                c.execute(
                    insert(budget_reservations).values(
                        id=str(uuid.uuid4()),
                        account_id=account,
                        reference_type='instance',
                        reference_id=reference,
                        amount_microusd=amount,
                        state=state,
                        actual_cost_microusd=actual,
                        created_at=created,
                        settled_at=settled,
                    )
                )
                if state in {'reserved', 'invoice_pending'}:
                    c.execute(
                        update(budget_accounts)
                        .where(budget_accounts.c.id == account)
                        .values(
                            reserved_microusd=budget_accounts.c.reserved_microusd
                            + amount
                        )
                    )
        return reference

    def read(self):
        with self.repo.engine.connect() as c:
            return projection(c, self.scope.tenant_id, self.now)

    def layer(self, account, owner=None, tenant=None):
        self.repo.configure_budget(
            account,
            tenant_id=tenant or self.scope.tenant_id,
            owner_id=owner,
            limit_microusd=10_000_000,
        )

    def test_rental_layers_and_owners_count_once_without_customer_job_price(self):
        self.layer('shared')
        self.layer('other-owner', 'supervan')
        self.rental(300_000, accounts=('owner-budget', 'shared', 'other-owner'))
        p = self.plan(
            cost=900_000,
            execution={'pool': 'test-pool', 'backend': 'mock', 'expected_runtime_s': 1},
        )
        self.repo.create_job(
            self.scope, p['id'], 'internal', budget_account_ids=('owner-budget',)
        )
        value = self.read()
        self.assertEqual(value['committed_microusd'], 300_000)
        self.assertEqual(value['projected_microusd'], 300_000)
        self.assertEqual(value['details'], [])

    def test_distinct_supplier_allocations_are_additive(self):
        self.rental(200_000, identity='one')
        self.rental(300_000, identity='two')
        self.assertEqual(self.read()['projected_microusd'], 500_000)

    def test_duplicate_allocation_identity_is_not_silently_counted_twice(self):
        self.rental(200_000, identity='same')
        self.rental(300_000, identity='same')
        value = self.read()
        self.assertEqual(value['status'], 'warning')
        self.assertIsNone(value['projected_microusd'])
        self.assertEqual(value['details'][0]['code'], 'supplier_reference_conflict')

    def test_pending_invoice_carries_across_sydney_midnight_settled_does_not(self):
        start = self.read()['period_start']
        self.rental(200_000, state='invoice_pending', created=start - 100_000)
        self.rental(
            600_000,
            state='settled',
            actual=400_000,
            created=start - 200_000,
            settled=start - 1,
        )
        self.rental(
            600_000,
            state='settled',
            actual=300_000,
            created=start - 100,
            settled=start + 1,
        )
        value = self.read()
        self.assertEqual(value['pending_microusd'], 200_000)
        self.assertEqual(value['settled_microusd'], 300_000)
        self.assertEqual(value['projected_microusd'], 500_000)

    def test_dst_uses_calendar_days_not_24_hour_subtraction(self):
        for when, hours in (
            (datetime(2026, 10, 3, 16, tzinfo=timezone.utc), 23),
            (datetime(2026, 4, 4, 16, tzinfo=timezone.utc), 25),
        ):
            with self.subTest(when=when):
                self.now = when.timestamp()
                value = self.read()
                self.assertEqual(
                    value['resets_at'] - value['period_start'], hours * 3600
                )

    def test_conflicting_actual_or_missing_timestamp_warns_without_blocking(self):
        self.layer('shared')
        ref = self.rental(
            500_000,
            accounts=('owner-budget', 'shared'),
            state='settled',
            actual=100_000,
            settled=self.now,
        )
        with self.repo.transaction() as c:
            c.execute(
                update(budget_reservations)
                .where(
                    budget_reservations.c.reference_id == ref,
                    budget_reservations.c.account_id == 'shared',
                )
                .values(actual_cost_microusd=200_000)
            )
        value = self.read()
        self.assertEqual(value['status'], 'warning')
        self.assertTrue(value['uncertainty_warning'])
        self.assertFalse(value['admission_blocked'])
        self.assertIsNone(value['projected_microusd'])
        with self.repo.transaction() as c:
            before = [
                dict(row) for row in c.execute(select(budget_reservations)).mappings()
            ]
            admit(
                c,
                self.scope.tenant_id,
                self.now,
                reference_type='instance',
                additional_microusd=1,
            )
            self.assertEqual(
                before,
                [
                    dict(row)
                    for row in c.execute(select(budget_reservations)).mappings()
                ],
            )
        with self.repo.transaction() as c:
            c.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == ref)
                .values(actual_cost_microusd=100_000, settled_at=None)
            )
        self.assertEqual(
            self.read()['details'][0]['code'], 'supplier_timestamp_unconfirmed'
        )

    def test_unknown_external_obligation_is_not_a_customer_cost_estimate(self):
        # No supplier adapter currently proves an independently charged job.
        plan = self.plan(cost=123_456)
        self.policy(adopted=False)
        self.repo.create_job(
            self.scope, plan['id'], 'unknown', budget_account_ids=('owner-budget',)
        )
        self.policy()
        with self.repo.transaction() as c:
            c.execute(update(jobs).values(request={'simulation': True}))
        value = self.read()
        self.assertEqual(value['details'][0]['code'], 'external_obligation_unconfirmed')
        self.assertIsNone(value['projected_microusd'])
        self.assertEqual(value['confirmed_exposure_microusd'], 0)

    def test_confirmed_cap_takes_precedence_over_unknown_warning(self):
        unknown = self.rental(700_000, state='settled', actual=500_000)
        confirmed = self.rental(900_000)
        value = self.read()
        self.assertTrue(value['uncertainty_warning'])
        self.assertIsNone(value['projected_microusd'])
        self.assertEqual(value['confirmed_exposure_microusd'], 900_000)
        self.assertFalse(value['admission_blocked'])
        with self.repo.transaction() as c:
            original = [
                dict(row) for row in c.execute(select(budget_reservations)).mappings()
            ]
            admit(
                c,
                self.scope.tenant_id,
                self.now,
                reference_type='instance',
                additional_microusd=100_000,
            )
            with self.assertRaisesRegex(BudgetExceeded, 'tenant_daily_limit_reached'):
                admit(
                    c,
                    self.scope.tenant_id,
                    self.now,
                    reference_type='instance',
                    additional_microusd=100_001,
                )
            self.assertEqual(
                original,
                [
                    dict(row)
                    for row in c.execute(select(budget_reservations)).mappings()
                ],
            )
        self.rental(100_000)
        value = self.read()
        self.assertEqual(value['status'], 'limit_reached')
        self.assertTrue(value['admission_blocked'])
        self.assertTrue(value['uncertainty_warning'])
        self.assertIsNone(value['projected_microusd'])
        with self.repo.transaction() as c:
            with self.assertRaisesRegex(BudgetExceeded, 'tenant_daily_limit_reached'):
                admit(c, self.scope.tenant_id, self.now, reference_type='instance')
            self.assertEqual(
                c.execute(
                    select(budget_reservations.c.settled_at).where(
                        budget_reservations.c.reference_id == unknown
                    )
                ).scalar_one(),
                None,
            )
            self.assertEqual(
                c.execute(
                    select(budget_reservations.c.state).where(
                        budget_reservations.c.reference_id == confirmed
                    )
                ).scalar_one(),
                'reserved',
            )

    def test_missing_intent_and_foreign_tenant_reference_refuse_attribution(self):
        ref = self.rental()
        self.layer('foreign', tenant='another-tenant')
        with self.repo.transaction() as c:
            c.execute(
                insert(budget_reservations).values(
                    id=str(uuid.uuid4()),
                    account_id='foreign',
                    reference_type='instance',
                    reference_id=ref,
                    amount_microusd=100_000,
                    state='reserved',
                    created_at=self.now,
                )
            )
        self.assertEqual(
            self.read()['details'][0]['code'], 'supplier_reference_conflict'
        )

    def test_unadopted_policy_keeps_old_admission_and_does_not_write(self):
        self.rental(2_000_000)
        self.policy(adopted=False)
        before = self.read()
        with self.repo.transaction() as c:
            self.assertIsNone(
                admit(
                    c,
                    self.scope.tenant_id,
                    self.now,
                    reference_type='instance',
                    additional_microusd=2_000_000,
                )
            )
        self.assertEqual(before, self.read())
        self.assertEqual(before['status'], 'policy_not_adopted')

    def test_threshold_warns_then_refuses_new_paid_work_and_replay_stays_original(self):
        self.policy(limit=2_000_000)
        self.rental(1_600_000)
        self.assertEqual(self.read()['status'], 'warning')
        plan = self.plan(
            cost=100_000,
            execution={'pool': 'test-pool', 'backend': 'mock', 'expected_runtime_s': 1},
        )
        job = self.repo.create_job(
            self.scope, plan['id'], 'first', budget_account_ids=('owner-budget',)
        )
        snapshot = job['execution_plan']['capacity_policy_snapshot']
        self.assertEqual(snapshot['settings_version'], 2)
        self.rental(500_000)
        self.policy(limit=1_000_000)
        replay = self.repo.create_job(
            self.scope, plan['id'], 'first', budget_account_ids=('owner-budget',)
        )
        self.assertFalse(replay['created'])
        self.assertEqual(replay['execution_plan']['capacity_policy_snapshot'], snapshot)
        with self.assertRaisesRegex(BudgetExceeded, 'tenant_daily_limit_reached'):
            self.repo.create_job(
                self.scope, plan['id'], 'next', budget_account_ids=('owner-budget',)
            )
        with self.repo.engine.connect() as c:
            self.assertEqual(len(list(c.execute(select(jobs)))), 1)
            original = (
                c.execute(select(plans).where(plans.c.id == plan['id']))
                .mappings()
                .one()
            )
            self.assertNotIn('capacity_policy_snapshot', original['execution_plan'])

    def test_planned_job_checks_current_policy_at_first_enqueue(self):
        plan = self.plan(cost=100_000)
        job = self.repo.create_job(
            self.scope, plan['id'], 'planned', initial_status='planned'
        )
        self.assertNotIn('capacity_policy_snapshot', job['execution_plan'])
        self.rental(1_000_000)
        with self.assertRaisesRegex(BudgetExceeded, 'tenant_daily_limit_reached'):
            self.repo.enqueue(
                self.scope, job['id'], budget_account_ids=('owner-budget',)
            )
        self.assertEqual(self.repo.get_job(self.scope, job['id'])['status'], 'planned')

    def test_only_positively_bound_test_rental_is_excluded(self):
        from studio_platform.dstack_test_budget import ACCOUNT

        self.layer(ACCOUNT)
        ref = self.rental(2_000_000, accounts=(ACCOUNT,))
        guard = {
            "version": 2,
            "task_id": "acceptance-456",
            "task_limit_microusd": 20_000_000,
            "budget_account_id": ACCOUNT,
            "max_runtime_seconds": 7200,
            "stop_margin_seconds": 300,
        }
        payload = {
            "tenant_id": self.scope.tenant_id,
            "owner_id": self.scope.owner_id,
            "project_id": "project-inkseq-staging-superdan",
            "capacity_backend": "dstack-v1",
            "configuration_id": "test-configuration",
            "lifecycle": {"test_gpu_guard": guard, "budget_account_ids": [ACCOUNT]},
            "request": {"test_max_duration_s": 7200, "test_task_id": "acceptance-456"},
            "dstack": {
                "run_spec": {
                    "configuration": {"tags": {"inkseq_test_task": "acceptance-456"}}
                }
            },
        }
        with self.repo.transaction() as c:
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == ref)
                .values(pool='inkseq-staging-v6-fl')
            )
            command = str(uuid.uuid4())
            c.execute(
                insert(operator_commands).values(
                    id=command,
                    actor='test',
                    idempotency_key=command,
                    request_hash='b' * 64,
                    kind='start',
                    state='completed',
                    payload={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            c.execute(
                insert(operator_nodes).values(
                    intent_id=ref,
                    command_id=command,
                    ordinal=0,
                    binding_id='binding',
                    binding_hash='c' * 64,
                    payload=payload,
                    desired_state='running',
                    runtime_state='running',
                    updated_at=self.now,
                )
            )
        self.assertEqual(self.read()['projected_microusd'], 0)
        # Merely naming a task or pool does not survive an invalid native tag.
        payload = copy.deepcopy(payload)
        payload['dstack']['run_spec']['configuration']['tags']['inkseq_test_task'] = (
            'different'
        )
        with self.repo.transaction() as c:
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == ref)
                .values(payload=payload)
            )
        value = self.read()
        self.assertEqual(value['status'], 'warning')
        self.assertEqual(value['details'][0]['field'], 'test_gpu_guard')

    def test_free_cpu_job_and_existing_rental_settlement_are_not_rejected(self):
        ref = self.rental(1_100_000)
        plan = self.plan(
            cost=0,
            execution={
                'pool': 'test-pool',
                'backend': 'cpu-render',
                'expected_runtime_s': 1,
            },
        )
        job = self.repo.create_job(self.scope, plan['id'], 'free')
        self.assertIn('capacity_policy_snapshot', job['execution_plan'])
        with self.repo.transaction() as c:
            # Ordinary settlement writes original actual evidence without new
            # admission, even when the accepted exposure already exceeds cap.
            self.repo._settle(c, 'instance', ref, 1_200_000)
        self.assertEqual(self.read()['settled_microusd'], 1_200_000)

    def test_failed_budget_reservation_rolls_back_job_and_new_snapshot(self):
        plan = self.plan(
            cost=100_000,
            execution={'pool': 'test-pool', 'backend': 'mock', 'expected_runtime_s': 1},
        )
        with self.assertRaises(NotFound):
            self.repo.create_job(
                self.scope,
                plan['id'],
                'missing-budget',
                budget_account_ids=('not-configured',),
            )
        with self.repo.engine.connect() as c:
            self.assertEqual(list(c.execute(select(jobs))), [])
            self.assertEqual(list(c.execute(select(budget_reservations))), [])
            original = (
                c.execute(select(plans).where(plans.c.id == plan['id']))
                .mappings()
                .one()
            )
            self.assertNotIn('capacity_policy_snapshot', original['execution_plan'])

    def test_original_accepted_snapshot_survives_blocked_enqueue(self):
        plan = self.plan(
            cost=0,
            execution={'pool': 'test-pool', 'backend': 'mock', 'expected_runtime_s': 1},
        )
        job = self.repo.create_job(
            self.scope, plan['id'], 'accepted', budget_account_ids=()
        )
        snapshot = job['execution_plan']['capacity_policy_snapshot']
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job['id']).values(status='blocked')
            )
        self.rental(1_100_000)
        self.policy(limit=2_000_000)
        resumed = self.repo.enqueue(self.scope, job['id'], budget_account_ids=())
        self.assertEqual(
            resumed['execution_plan']['capacity_policy_snapshot'], snapshot
        )

    def test_policy_snapshot_is_private_in_creator_response_and_retained_for_admin(
        self,
    ):
        from types import SimpleNamespace
        from studio_platform.quality_views import job as public_job

        plan = self.plan(
            cost=100_000,
            execution={'pool': 'test-pool', 'backend': 'mock', 'expected_runtime_s': 1},
        )
        job = self.repo.create_job(
            self.scope, plan['id'], 'response', budget_account_ids=('owner-budget',)
        )
        response = {
            'id': job['id'],
            'execution': job['execution_plan'],
            'status': job['status'],
        }
        before = copy.deepcopy(response)
        creator = public_job(response)
        self.assertNotIn('capacity_policy_snapshot', creator['execution'])
        admin = SimpleNamespace(machine=False, role='admin', status='active')
        self.assertEqual(public_job(response, admin), response)
        self.assertEqual(response, before)

    def test_http_actual_job_gate_returns_only_finite_aggregate_reasons(self):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        plan = self.plan(
            cost=100_000,
            execution={'pool': 'test-pool', 'backend': 'mock', 'expected_runtime_s': 1},
        )
        ref = self.rental(1_100_000)
        app = create_app(
            Settings(
                Path(self.temp.name) / 'http',
                auth_mode='local-test',
                tenant_id=self.scope.tenant_id,
            ),
            repository=self.repo,
        )

        @app.post('/_test/paid-admission')
        def paid_admission():
            return self.repo.create_job(
                self.scope,
                plan['id'],
                'http-rejected',
                budget_account_ids=('owner-budget',),
            )

        with TestClient(app) as client:
            self.assertEqual(
                client.post(
                    '/api/auth/login', json={'username': 'superdan'}
                ).status_code,
                200,
            )
            response = client.post('/_test/paid-admission')
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json(), {'code': 'tenant_daily_limit_reached'})
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            with self.repo.transaction() as c:
                c.execute(
                    update(budget_reservations)
                    .where(budget_reservations.c.reference_id == ref)
                    .values(state='unknown')
                )
            response = client.post('/_test/paid-admission')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(self.read()['uncertainty_warning'])
            self.assertEqual(client.post('/api/auth/logout').status_code, 200)
        with self.repo.engine.connect() as c:
            self.assertEqual(len(list(c.execute(select(jobs)))), 1)
            self.assertEqual(
                c.execute(
                    select(budget_reservations.c.state).where(
                        budget_reservations.c.reference_id == ref
                    )
                ).scalar_one(),
                'unknown',
            )

    def test_cross_owner_machine_admission_and_reservation_are_atomic(self):
        self.repo.configure_pool('gpu', max_instances=10, max_physical_gpus=10)
        self.layer('shared')

        def attempt(index):
            scope = self.scope if index == 0 else self.other
            try:
                with self.repo.transaction() as c:
                    self.repo._lock_capacity(c)
                    admit(
                        c,
                        scope.tenant_id,
                        self.now,
                        reference_type='instance',
                        additional_microusd=600_000,
                    )
                    return self.repo.reserve_instance_intent(
                        scope,
                        'gpu',
                        'atomic-' + str(index),
                        reserved_cost_microusd=600_000,
                        hard_deadline=self.now + 600,
                        budget_account_ids=('shared',),
                        dry_run=False,
                        provider='vast',
                        connection=c,
                    )['id']
            except BudgetExceeded as exc:
                return str(exc)

        result = self.parallel(attempt, count=2)
        self.assertEqual(result.count('tenant_daily_limit_reached'), 1)
        self.assertEqual(self.read()['projected_microusd'], 600_000)


if __name__ == '__main__':
    unittest.main()
