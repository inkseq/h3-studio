"""A new credit customer's original job uses the shared fleet; fake provider only."""

import copy
import json
import os
from pathlib import Path
import uuid
from unittest.mock import patch

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    delete,
    insert,
    select,
    text,
    update,
)

from studio_platform import credits, credits_pricing
from studio_platform.auth import Auth, Principal
from studio_platform.capabilities import compile_request
from studio_platform.credit_schema import metadata as credit_metadata, prices, entries
from studio_platform.credits_routes import CreditAPI
from studio_platform.dstack_demand import demand_admission, eligible_jobs
from studio_platform.dstack_demand_policy import policy_key
from studio_platform.dstack_demand_start import start_once
from studio_platform.dstack_operator import DstackOperator
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.generation_budget_binding import ensure_owner_binding
from studio_platform.identity_schema import metadata as identity_metadata, users
from studio_platform.operator_capacity import operator_policy, operator_nodes
from studio_platform.repository import (
    Scope,
    budget_accounts,
    budget_reservations,
    instance_intents,
)
from studio_platform.schema_version import CREDIT_ADMISSION_REVISION
from studio_platform.settings import Settings
from studio_platform.workos_auth import WorkOSAuth
from test_platform_api import generation_request
from test_platform_execution_policy import synthetic_assets
from test_platform_repository import LedgerCase
import test_dstack_operator as dstack_fixtures
import test_credits as credit_fixtures


BINDINGS = (
    Path(__file__).resolve().parents[1]
    / 'deploy/dstack/taomate-v7.production-bindings.json'
)


class CustomerDemandTests(LedgerCase):
    policy = credit_fixtures.CreditTests.policy
    seed = credit_fixtures.CreditTests.seed

    def setUp(self):
        super().setUp()
        self.now = 1793152400.0
        dstack_fixtures.DstackOperatorTests._setup_dstack(self)
        document = json.loads(BINDINGS.read_text(encoding='utf-8-sig'))
        self.profile_path = Path(self.temp.name) / 'execution-profiles.json'
        self.profile_path.write_text(
            json.dumps(
                {'schema_version': 1, 'policies': document['execution_policies']}
            )
        )
        self.profile_path.chmod(0o600)
        self.settings = Settings(
            Path(self.temp.name),
            tenant_id=self.scope.tenant_id,
            execution_backend='wangp-worker',
            generation_enabled=True,
            execution_profiles_file=self.profile_path,
            operator_capacity_owners=('superdan', 'supervan'),
            operator_admin_owners=('superdan',),
        )
        self.config['project'] = 'sixnine'
        self.config['profiles'] = [
            p for p in document['operator_profiles'] if p['owner_id'] == 'superdan'
        ]
        self.config['policy'].update(
            expires_at=None,
            lifecycle_version=2,
            reservation_seconds=3600,
            max_instances=2,
            max_physical_gpus=2,
            max_hourly_cost_microusd=1_700_000,
            max_ttl_seconds=7200,
            demand_start={'profile_ids': [p['id'] for p in self.config['profiles']]},
        )
        self.operator_path = Path(self.temp.name) / 'operator.json'
        self.operator_path.write_text(json.dumps(self.config))
        self.operator_path.chmod(0o600)
        env = patch.dict(
            os.environ, {'DSTACK_OPERATOR_CONFIG': str(self.operator_path)}
        )
        env.start()
        self.addCleanup(env.stop)
        marker = Table(
            'alembic_version',
            MetaData(),
            Column('version_num', String(32), primary_key=True),
        )
        self.fleet_user_id = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            identity_metadata.create_all(connection)
            credit_metadata.create_all(connection)
            marker.create(connection)
            connection.execute(
                insert(marker).values(version_num=CREDIT_ADMISSION_REVISION)
            )
            connection.exec_driver_sql(
                "ALTER TABLE platform_budget_reservations ADD COLUMN budget_role VARCHAR(20) NOT NULL DEFAULT 'enforced' CONSTRAINT ck_budget_reservation_role CHECK (budget_role IN ('accounting','enforced'))"
            )
            connection.execute(
                insert(users).values(
                    id=self.fleet_user_id,
                    tenant=self.scope.tenant_id,
                    owner='superdan',
                    role='admin',
                    status='active',
                    display_name='Disposable fleet owner',
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            connection.execute(
                insert(prices).values(
                    tenant=self.scope.tenant_id,
                    revision='price-1',
                    rates=credits_pricing.DEFAULT_RATES,
                    configuration=credits_pricing.DEFAULT_CONFIGURATION,
                    published_by=self.fleet_user_id,
                    published_at=self.now,
                )
            )
        for account_id in (
            'demand-job-total-20261004',
            'demand-gpu-remaining-20261004',
        ):
            self.repo.configure_budget(
                account_id, tenant_id=self.scope.tenant_id, limit_microusd=10_000_000
            )
        self.repo.configure_budget(
            'demand-job-owner-superdan-20261004',
            tenant_id=self.scope.tenant_id,
            owner_id='superdan',
            limit_microusd=10_000_000,
        )
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        self.client.apply = self.apply_fake
        for profile in self.service.profiles.values():
            self.repo.configure_pool(
                profile['pool'], max_instances=2, max_physical_gpus=2
            )
        self.policy()
        self.set_demand()
        self.workos = WorkOSAuth(
            self.repo,
            Auth(self.repo.engine, mode='local-test', tenant=self.scope.tenant_id),
            self.settings,
        )
        self.customer = self.workos._upsert(
            {
                'subject': 'user_' + 'A' * 24,
                'email': 'customer@example.test',
                'display_name': 'New customer',
            },
            {'purpose': 'signin'},
            None,
        )
        self.customer_scope = Scope(
            self.scope.tenant_id,
            self.customer['owner'],
            'customer-story',
            'browser:customer',
        )
        self.customer_budget = (
            'demand-job-owner-' + self.customer['owner'] + '-20261004'
        )
        self.seed(100, owner=self.customer['owner'])

    def set_demand(self, enabled=True):
        with self.repo.transaction() as connection:
            key = policy_key(self.scope.tenant_id)
            row = (
                connection.execute(
                    select(operator_policy).where(operator_policy.c.id == key)
                )
                .mappings()
                .first()
            )
            value = {
                'payload': {'tenant_id': self.scope.tenant_id, 'enabled': enabled},
                'actor': 'test-admin',
                'updated_at': self.now,
            }
            if row:
                connection.execute(
                    update(operator_policy)
                    .where(operator_policy.c.id == key)
                    .values(**value, version=row['version'] + 1)
                )
            else:
                connection.execute(
                    insert(operator_policy).values(**value, id=key, version=1)
                )

    def apply_fake(self, spec):
        """The original fake assumed one account; this fixture proves both holds."""
        identity = spec['configuration']['tags']['sixnine_intent']
        self.assertTrue(self.store.load(identity)['apply_started'])
        with self.repo.engine.connect() as connection:
            holds = (
                connection.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_id == identity
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual(len(holds), 2)
        self.assertTrue(all(row['state'] == 'reserved' for row in holds))
        self.client.applied.append(identity)
        run = {
            'id': str(uuid.uuid4()),
            'project_name': self.client.project,
            'run_spec': copy.deepcopy(spec),
            'status': 'submitted',
            'cost': 0.1,
        }
        self.client.runs[spec['run_name']] = run
        return copy.deepcopy(run)

    def preflight(self, mode='fl'):
        body = generation_request(
            self.customer_scope.project_id,
            deployment_profile_id='h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7',
            recipe_id='h3-base-' + ('fl2va' if mode == 'fl' else 'ref2va') + '-v1',
            controls={
                'duration': 5,
                'resolution': '480P',
                'seed': '123',
                'steps': 5,
                'sampler_name': 'euler',
            },
        )
        if mode == 'ref':
            body['inputs'] = {'images': ['image']}
        assets = synthetic_assets()
        assets['image']['metadata']['model_ready'] = True
        compiled, fingerprint = compile_request(
            body, assets.__getitem__, backend='wangp-worker'
        )
        return compiled, ExecutionPolicies(self.settings, self.repo).evaluate(
            compiled, self.customer_scope, fingerprint
        )

    def admit(self, mode='fl'):
        compiled, result = self.preflight(mode)
        self.assertTrue(result.execution['enabled'], result.execution['blockers'])
        self.assertEqual(result.execution['credit_quote']['version'], 2)
        plan = self.repo.create_plan(
            self.customer_scope,
            compiled,
            result.execution,
            expires_at=result.expires_at,
            estimated_cost_microusd=result.cost,
        )
        ExecutionPolicies(self.settings, self.repo).ensure_current(
            plan, self.customer_scope
        )
        job = self.repo.create_job(
            self.customer_scope,
            plan['id'],
            'customer-' + mode,
            budget_account_ids=result.execution['budget_account_ids'],
        )
        return self.repo.get_job(self.customer_scope, job['id'])

    def test_new_customer_uses_shared_fleet_without_owner_profile_or_usd_grant(self):
        self.assertNotIn(
            self.customer['owner'],
            {p['owner_id'] for p in self.service.profiles.values()},
        )
        self.assertEqual(
            self.repo.get_budget(self.customer_budget)['limit_microusd'], 0
        )
        job = self.admit()
        with self.repo.engine.connect() as connection:
            work = eligible_jobs(self.service, connection, self.now)
            roles = {
                row['account_id']: row['budget_role']
                for row in connection.execute(
                    text(
                        'SELECT account_id,budget_role FROM platform_budget_reservations WHERE reference_id=:id'
                    ),
                    {'id': job['id']},
                ).mappings()
            }
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0][0]['owner_id'], self.customer['owner'])
        self.assertEqual(work[0][1]['owner_id'], 'superdan')
        self.assertEqual(roles[self.customer_budget], 'accounting')
        self.assertEqual(roles['demand-job-total-20261004'], 'enforced')
        started = start_once(self.service)
        self.assertEqual(started['state'], 'accepted', started)
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            intent = (
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == started['node_id']
                    )
                )
                .mappings()
                .one()
            )
            node = (
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == started['node_id']
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(intent['pool'], job['pool'])
        self.assertEqual(node['payload']['tenant_id'], self.scope.tenant_id)
        self.assertEqual(node['payload']['owner_id'], 'superdan')
        self.assertEqual(self.repo.get_job(self.customer_scope, job['id']), job)
        original = copy.deepcopy(self.client.applied)
        self.assertEqual(start_once(self.service)['state'], 'observing')
        self.assertEqual(self.client.applied, original)

    def test_ref_customer_selects_only_ref_fleet_and_preserves_job_owner(self):
        job = self.admit('ref')
        with self.repo.engine.connect() as connection:
            work = eligible_jobs(self.service, connection, self.now)
        self.assertEqual(
            [(p['mode'], p['owner_id']) for _, p in work], [('ref', 'superdan')]
        )
        self.assertEqual(job['owner_id'], self.customer['owner'])

    def test_charging_off_cannot_turn_zero_customer_binding_into_usd_authority(self):
        self.policy(False)
        before = self.repo.get_budget(self.customer_budget)
        _, result = self.preflight()
        self.assertFalse(result.execution['enabled'])
        self.assertEqual(result.execution['budget_error_code'], 'budget_exceeded')
        self.assertEqual(self.repo.get_budget(self.customer_budget), before)
        self.assertEqual(start_once(self.service)['state'], 'idle')
        self.assertEqual(self.client.applied, [])

    def test_global_capacity_refusal_precedes_provider_apply(self):
        job = self.admit()
        self.repo.configure_capacity(max_instances=0, max_physical_gpus=0)
        result = start_once(self.service)
        self.assertEqual(result['reason_code'], 'capacity_limit')
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_job(self.customer_scope, job['id']), job)

    def test_customer_suspend_after_admission_preserves_job_and_prevents_start(self):
        job = self.admit()
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == self.customer['id'])
                .values(status='suspended')
            )
        self.assertEqual(start_once(self.service)['state'], 'idle')
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_job(self.customer_scope, job['id']), job)

    def test_demand_off_preserves_original_customer_job_and_hold(self):
        job = self.admit()
        with self.repo.engine.connect() as connection:
            before = list(connection.execute(select(entries)).mappings())
        self.set_demand(False)
        self.assertEqual(start_once(self.service)['state'], 'disabled')
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_job(self.customer_scope, job['id']), job)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                list(connection.execute(select(entries)).mappings()), before
            )

    def test_other_tenant_cannot_borrow_the_customer_fleet(self):
        compiled, _ = self.preflight()
        policy = next(
            p
            for p in json.loads(self.profile_path.read_text())['policies']
            if p['recipe_ids'] == [compiled['recipe_id']]
        )
        foreign = Scope(
            'other-tenant', self.customer['owner'], self.customer_scope.project_id
        )
        self.assertIsNone(
            demand_admission(self.settings, self.repo, foreign, compiled, policy)
        )
        self.assertEqual(self.client.applied, [])

    def test_backfill_reuses_existing_binding_without_granting_or_rewriting_money(self):
        before = self.repo.get_budget(self.customer_budget)
        with self.repo.transaction() as connection:
            entry_count = len(connection.execute(select(entries)).all())
            result = ensure_owner_binding(
                connection,
                self.settings,
                self.customer,
                actor_user_id=self.fleet_user_id,
                reason='Test original binding replay',
                now=self.now,
            )
        self.assertEqual(
            result,
            {
                'user_id': self.customer['id'],
                'created': [],
                'existing': [self.customer_budget],
            },
        )
        self.assertEqual(self.repo.get_budget(self.customer_budget), before)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                len(connection.execute(select(entries)).all()), entry_count
            )

    def test_missing_old_customer_binding_requires_audited_backfill_not_read_repair(
        self,
    ):
        with self.repo.transaction() as connection:
            connection.execute(
                delete(budget_accounts).where(
                    budget_accounts.c.id == self.customer_budget
                )
            )
        for _ in range(2):
            with self.assertRaisesRegex(credits.CreditError, 'credit_quote_invalid'):
                self.preflight()
        with self.repo.engine.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    select(budget_accounts).where(
                        budget_accounts.c.id == self.customer_budget
                    )
                ).first()
            )
            before_entries = list(connection.execute(select(entries)).mappings())
        api = CreditAPI(self.repo, self.settings)
        admin = Principal('superdan', 'browser:admin', role='admin')
        body = {'reason': 'Audited existing customer binding'}
        first = api.bind_generation_budget(
            admin, self.customer['id'], body, key='existing-customer'
        )
        self.assertEqual(first['created'], [self.customer_budget])
        self.assertEqual(
            api.bind_generation_budget(
                admin, self.customer['id'], body, key='existing-customer'
            ),
            first,
        )
        self.assertEqual(
            self.repo.get_budget(self.customer_budget)['limit_microusd'], 0
        )
        self.assertTrue(self.preflight()[1].execution['enabled'])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                list(connection.execute(select(entries)).mappings()), before_entries
            )

    def test_foreign_owner_binding_refuses_without_provider_or_credit_hold(self):
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == self.customer_budget)
                .values(owner_id='foreign-owner')
            )
            before_entries = list(connection.execute(select(entries)).mappings())
        with self.assertRaisesRegex(credits.CreditError, 'credit_quote_invalid'):
            self.preflight()
        self.assertEqual(self.client.applied, [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                list(connection.execute(select(entries)).mappings()), before_entries
            )

    def test_credit_customer_cannot_bypass_shared_generation_budget(self):
        self.repo.configure_budget(
            'demand-job-total-20261004',
            tenant_id=self.scope.tenant_id,
            limit_microusd=0,
        )
        _, result = self.preflight()
        self.assertFalse(result.execution['enabled'])
        self.assertEqual(result.execution['budget_error_code'], 'budget_exceeded')
        self.assertEqual(self.client.applied, [])

    def test_customer_credits_do_not_authorize_supplier_fleet_budget(self):
        job = self.admit()
        self.repo.configure_budget(
            'demand-gpu-remaining-20261004',
            tenant_id=self.scope.tenant_id,
            limit_microusd=0,
        )
        result = start_once(self.service)
        self.assertEqual(result['reason_code'], 'budget_limit')
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_job(self.customer_scope, job['id']), job)

    def test_second_customer_reuses_starting_shared_capacity(self):
        first_scope = self.customer_scope
        first = self.admit()
        started = start_once(self.service)
        self.assertEqual(started['state'], 'accepted')
        second = self.workos._upsert(
            {
                'subject': 'user_' + 'B' * 24,
                'email': 'other@example.test',
                'display_name': 'Other customer',
            },
            {'purpose': 'signin'},
            None,
        )
        self.seed(100, owner=second['owner'], key='other-customer-grant')
        self.customer_scope = Scope(
            self.scope.tenant_id, second['owner'], 'customer-story', 'browser:other'
        )
        second_job = self.admit()
        start_once(self.service)
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.repo.get_job(first_scope, first['id']), first)
        self.assertEqual(
            self.repo.get_job(self.customer_scope, second_job['id']), second_job
        )
