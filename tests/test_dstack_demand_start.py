"""Original-command planning, response-loss and capacity fences; fake HTTP only."""

import copy
import json
from unittest.mock import Mock, patch

from sqlalchemy import insert, select, update

from studio_platform.dstack_capacity import DstackReadRateLimited
from studio_platform.dstack_demand_policy import policy_key
from studio_platform.dstack_demand_start import start_once
from studio_platform.dstack_operator import DstackOperator
from studio_platform.operator_capacity import (
    operator_commands,
    operator_nodes,
    operator_policy,
)
from studio_platform.repository import (
    BudgetExceeded,
    budget_reservations,
    jobs,
    request_hash,
)
from test_platform_repository import LedgerCase
import test_dstack_operator as original


class DemandStartTests(LedgerCase):
    def fleet_planner(self, *, fleet_available):
        import httpx
        from studio_platform.dstack_capacity import DstackClient

        stock_plan = self.client.plan
        reads = []

        def handler(request):
            self.assertEqual(request.url.path, '/api/project/sixnine/runs/get_plan')
            body = json.loads(request.content)
            reads.append(body)
            # Model pinned 0.22.5: stock exists, but ordinary run planning must
            # select a fleet with room before it can expose those offers.
            plan = stock_plan(body['run_spec'])
            if not body.get('for_offers_only', False) and not fleet_available:
                plan['job_plans'][0]['offers'] = []
            return httpx.Response(200, json=plan)

        session = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(session.close)
        client = DstackClient(
            'http://127.0.0.1:3000', 'isolated-test-token', 'sixnine', session=session
        )
        replacement = patch.object(self.client, 'plan', side_effect=client.plan)
        replacement.start()
        self.addCleanup(replacement.stop)
        return reads

    def test_global_stock_without_fleet_cannot_reserve_or_apply(self):
        self.enabled()
        job = self.accepted()
        reads = self.fleet_planner(fleet_available=False)
        with self.repo.engine.connect() as connection:
            before = list(connection.execute(select(budget_reservations)).mappings())
        result = start_once(self.service)
        self.assertEqual(
            result, {'state': 'blocked', 'reason_code': 'offer_unavailable'}
        )
        self.assertEqual(len(reads), 1)
        self.assertFalse(reads[0]['for_offers_only'])
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                list(connection.execute(select(budget_reservations)).mappings()), before
            )
            self.assertEqual(list(connection.execute(select(operator_nodes))), [])
            self.assertEqual(
                list(
                    connection.execute(
                        select(operator_commands).where(
                            operator_commands.c.kind == 'dstack_start'
                        )
                    )
                ),
                [],
            )

    def test_matching_fleet_keeps_original_guarded_start_path(self):
        self.enabled()
        self.accepted()
        reads = self.fleet_planner(fleet_available=True)
        first = start_once(self.service)
        self.assertEqual(first['state'], 'accepted')
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(len(reads), 1)
        self.assertFalse(reads[0]['for_offers_only'])
        # Observing the accepted start must not plan or allocate again.
        self.assertEqual(start_once(self.service)['state'], 'observing')
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(self.client.applied), 1)

    def summary_job(self):
        job = self.accepted()
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job['id'])
                .values(
                    execution_plan=dict(
                        job['execution_plan'],
                        backend='wangp-worker',
                        dispatch_backend='hatchet-v1',
                    ),
                    request=dict(job['request'], request={'mode': 'fl'}),
                )
            )
        return self.repo.get_job(self.scope, job['id'])

    def demand_summary(self):
        from studio_platform.dstack_demand import summary

        with self.repo.engine.connect() as connection:
            # The existing live witness can prove no current occupancy without
            # manufacturing the original allocator's missing zero-cost receipt.
            witness = {
                'observations': [
                    {'intent_id': n, 'counted': False}
                    for n in connection.execute(
                        select(operator_nodes.c.intent_id)
                    ).scalars()
                ]
            }
            with patch.object(
                self.service, '_live_capacity_usage', return_value=witness
            ):
                return summary(self.service, connection, self.now)

    def test_summary_explains_unresolved_original_no_capacity_without_replay(self):
        from test_dstack_capacity import no_capacity_run

        self.enabled()
        job = self.summary_job()
        started = start_once(self.service)
        binding = self.store.load(started['node_id'])
        self.client.runs[binding['run_name']] = no_capacity_run(
            self.client.runs[binding['run_name']]
        )
        self.capacity.observe(started['node_id'])
        before = (
            self.journal(),
            self.old_holds(started['node_id']),
            self.store.load(started['node_id']),
        )
        applied = copy.deepcopy(self.client.applied)
        value = self.demand_summary()
        self.assertEqual(value['running_machines'] + value['starting_machines'], 0)
        self.assertEqual(value['blocked_reason'], 'allocation_uncertain')
        self.assertEqual(value['last_start_blockers'], {'allocation_uncertain': 1})
        self.assertEqual(self.client.applied, applied)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)
        self.assertEqual(
            before,
            (
                self.journal(),
                self.old_holds(started['node_id']),
                self.store.load(started['node_id']),
            ),
        )

    def test_summary_reports_budget_refusal_but_does_not_block_unattempted_work(self):
        self.enabled()
        self.summary_job()
        with patch.object(
            self.service,
            '_preview_for_owner',
            side_effect=BudgetExceeded('dstack_test_task_limit_reached'),
        ):
            self.assertEqual(start_once(self.service)['reason_code'], 'budget_limit')
        original = self.journal()
        value = self.demand_summary()
        self.assertEqual(value['blocked_reason'], 'budget_limit')
        self.assertEqual(value['last_start_blockers'], {'budget_limit': 1})
        self.summary_job()
        value = self.demand_summary()
        self.assertIsNone(value['blocked_reason'])
        self.assertEqual(value['last_start_blockers'], {'budget_limit': 1})
        self.assertEqual(self.journal(), original)
        self.assertEqual(self.client.applied, [])

    def test_summary_clears_old_budget_reason_after_original_start_exists(self):
        self.enabled()
        self.summary_job()
        with patch.object(
            self.service,
            '_preview_for_owner',
            side_effect=BudgetExceeded('dstack_test_task_limit_reached'),
        ):
            start_once(self.service)
        self.now += 80
        started = start_once(self.service)
        self.assertEqual(started['state'], 'accepted')
        # Even a retained old diagnostic cannot override the original start.
        row = self.journal()
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_commands)
                .where(operator_commands.c.id == row['id'])
                .values(payload=dict(row['payload'], reason_code='budget_limit'))
            )
        value = self.demand_summary()
        self.assertIsNone(value['blocked_reason'])
        self.assertEqual(value['last_start_blockers'], {})

    def test_startup_replacement_advances_without_supplier_removal_proof(self):
        from studio_platform.capacity_watchdog import reclaim_node
        from studio_platform.repository import instance_intents

        self.enabled()
        job = self.accepted()
        first = start_once(self.service)
        self.assertEqual(first['state'], 'accepted')
        self.now += 1201
        with self.repo.engine.connect() as connection:
            node = dict(connection.execute(select(operator_nodes)).mappings().one())
            intent = dict(connection.execute(select(instance_intents)).mappings().one())
        recovered = reclaim_node(self.service, node, intent)
        self.assertTrue(recovered['stop_requested'])
        self.assertIsNone(
            self.store.load(node['intent_id']).get('provider_removal_proof')
        )
        waiting = start_once(self.service)
        self.assertEqual(waiting['reason_code'], 'backoff')
        self.now += 80  # Past the original 60s lease plus <=15s identity jitter.
        second = start_once(self.service)
        self.assertEqual(second['state'], 'accepted')
        self.assertNotEqual(second['node_id'], first['node_id'])
        self.assertEqual(self.repo.get_job(self.scope, job['id'])['status'], 'queued')
        self.assertEqual(len(self.client.applied), 2)

    def test_demand_does_not_spend_when_actual_registration_cap_is_disabled(self):
        self.enabled()
        self.accepted()
        self.repo.configure_capacity(max_instances=0, max_physical_gpus=0)
        result = start_once(self.service)
        self.assertEqual(
            (result['state'], result['reason_code']), ('blocked', 'capacity_limit')
        )
        self.assertEqual(self.client.applied, [])

    def test_adopted_ten_launches_and_registers_fifth_under_positive_legacy_four(self):
        from studio_platform.control import WorkerControl, WorkerSpec
        from studio_platform.repository import registered_workers
        from test_capacity_admission import adopt
        from test_dstack_registration import RegistrationCapacityTests

        self.repo.configure_capacity(max_instances=4, max_physical_gpus=4)
        self.repo.configure_pool('dstack-test', max_instances=4, max_physical_gpus=4)
        for index in range(4):
            WorkerControl(self.repo).register(
                WorkerSpec(
                    'legacy-' + str(index),
                    'test-pool',
                    'lium',
                    'old-' + str(index),
                    ('GPU-' + str(index),),
                    ('recipe',),
                    'model',
                    'config',
                )
            )
        adopt(self, 10)
        self.enabled()
        original = self.accepted()
        accepted = start_once(self.service)
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(len(self.client.applied), 1)
        self.provisioned(accepted['node_id'])
        binding = self.store.load(accepted['node_id'])
        run = self.client.runs[binding['run_name']]
        run['status'] = 'running'
        run['latest_job_submission']['job_provisioning_data']['instance_id'] = '4300005'
        self.capacity.observe(accepted['node_id'])
        WorkerControl(
            self.repo, capacity_usage=self.service.registration_capacity_usage
        ).register(RegistrationCapacityTests.spec(self, accepted['node_id']))
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                len(list(connection.execute(select(registered_workers)))), 5
            )
            payload = connection.execute(select(operator_nodes.c.payload)).scalar_one()
        self.assertEqual(
            payload['capacity_policy_snapshot']['value']['max_machines'], 10
        )
        self.assertEqual(payload['request']['lifecycle_version'], 2)
        self.assertEqual(self.repo.get_job(self.scope, original['id']), original)

    def test_legacy_registered_device_blocks_prelaunch_before_any_paid_apply(self):
        from studio_platform.control import WorkerControl, WorkerSpec

        self.enabled()
        original = self.accepted()
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        WorkerControl(self.repo).register(
            WorkerSpec(
                'legacy-worker',
                'test-pool',
                'lium',
                'legacy-machine',
                ('GPU-legacy',),
                ('recipe',),
                'model',
                'config',
            )
        )
        with patch.object(self.service, '_preview_for_owner') as planner:
            self.assertEqual(
                start_once(self.service),
                dict(state='blocked', reason_code='capacity_limit'),
            )
            planner.assert_not_called()
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_job(self.scope, original['id']), original)

    def setUp(self):
        super().setUp()
        original.DstackOperatorTests._setup_dstack(self)
        self.config['project'] = 'sixnine'
        self.config['policy'].update(
            lifecycle_version=2,
            reservation_seconds=3600,
            max_instances=2,
            max_physical_gpus=2,
            demand_start={'profile_ids': ['5090-fl']},
        )
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        profile = self.service.profiles['5090-fl']
        self.protected = dict(
            enabled=True,
            backend='wangp-worker',
            dispatch_backend='hatchet-v1',
            pool=profile['pool'],
            configuration_id=profile['configuration_id'],
            model_id=profile['model_id'],
            engine_manifest_digest=profile['manifest_digest'],
            qualification=dict(status='accepted', verified_at=1.0, expires_at=20000.0),
            reservation=dict(expected_runtime_s=1800, expires_at=20000.0),
            envelope=dict(max_duration_seconds=4.0),
        )
        selected = patch(
            'studio_platform.execution_profiles.selected_policy',
            return_value=self.protected,
        )
        selected.start()
        self.addCleanup(selected.stop)
        self.work = []
        eligible = patch(
            'studio_platform.dstack_demand.eligible_jobs', side_effect=self.eligible
        )
        eligible.start()
        self.addCleanup(eligible.stop)

    def eligible(self, service, connection, now):
        current = {
            row['id']: dict(row) for row in connection.execute(select(jobs)).mappings()
        }
        return [
            (current[identity], service.profiles['5090-fl'])
            for identity in self.work
            if current[identity]['status'] == 'queued'
        ]

    def test_ref_demand_start_uses_same_canonical_ceiling_as_manual_preview(self):
        self.config['profiles'][0].update(mode='ref', max_price_microusd=1_500_000)
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        profile = self.service.profiles['5090-fl']
        self.protected.update(
            configuration_id=profile['configuration_id'],
            model_id=profile['model_id'],
            engine_manifest_digest=profile['manifest_digest'],
        )
        self.enabled()
        self.accepted()
        result = start_once(self.service)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            node = connection.execute(select(operator_nodes)).mappings().one()
        self.assertEqual(node['payload']['selection']['mode'], 'ref')
        self.assertEqual(node['payload']['request']['max_price_microusd'], 1_500_000)
        self.assertEqual(node['payload']['hourly_cost_microusd'], 1_500_000)

    def enabled(self, value=True):
        with self.repo.transaction() as connection:
            row = (
                connection.execute(
                    select(operator_policy).where(
                        operator_policy.c.id == policy_key(self.scope.tenant_id)
                    )
                )
                .mappings()
                .first()
            )
            values = dict(
                payload={'tenant_id': self.scope.tenant_id, 'enabled': value},
                actor='qa-admin',
                updated_at=self.now,
            )
            if row:
                connection.execute(
                    update(operator_policy)
                    .where(operator_policy.c.id == row['id'])
                    .values(**values, version=row['version'] + 1)
                )
            else:
                connection.execute(
                    insert(operator_policy).values(
                        id=policy_key(self.scope.tenant_id), version=1, **values
                    )
                )

    def accepted(self):
        plan = self.repo.create_plan(
            self.scope,
            {'prompt': 'kept original'},
            dict(
                pool='dstack-test',
                expected_runtime_s=120,
                policy_hash=request_hash('protected'),
            ),
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        created = self.repo.create_job(self.scope, plan['id'], 'job-' + plan['id'])
        self.work.append(created['id'])
        return self.repo.get_job(self.scope, created['id'])

    def journal(self):
        with self.repo.engine.connect() as connection:
            return dict(
                connection.execute(
                    select(operator_commands).where(
                        operator_commands.c.kind == 'dstack_demand'
                    )
                )
                .mappings()
                .one()
            )

    def historical_unknown(self):
        from test_dstack_capacity import no_capacity_run

        preview = self.service.preview(
            self.owner, {'profile_id': '5090-fl', 'keep_on': 'idle'}
        )
        old = self.service.start(
            self.owner, {'preview_id': preview['preview_id']}, 'historical-unknown'
        )['node_id']
        bound = self.store.load(old)
        run = no_capacity_run(self.client.runs[bound['run_name']])
        message = 'sixnine_vast_attempts_v1:' + json.dumps(
            dict(
                version=1,
                outcome='unknown',
                attempted_count=1,
                attempts=[
                    dict(
                        ask_id='12345',
                        http_status=400,
                        code='mutation_unknown',
                        outcome='unknown',
                        contract_id=None,
                    )
                ],
            )
        )
        run['latest_job_submission']['termination_reason_message'] = message
        run['jobs'][0]['job_submissions'][0]['termination_reason_message'] = message
        self.client.runs[bound['run_name']] = run
        self.capacity.observe(old)
        return old

    def old_holds(self, identity):
        with self.repo.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_id == identity
                    )
                ).mappings()
            ]

    def provisioned(self, identity):
        bound = self.store.load(identity)
        self.client.runs[bound['run_name']].update(
            status='provisioning',
            latest_job_submission=dict(
                job_provisioning_data=dict(
                    backend='vastai',
                    instance_id='test-' + identity,
                    instance_type=dict(resources=dict(gpus=[dict(name='RTX5090')])),
                )
            ),
        )

    def test_terminal_unknown_history_frees_live_capacity_without_settling_it(self):
        old = self.historical_unknown()
        before, holds = self.store.load(old), self.old_holds(old)
        self.assertEqual(before['state'], 'creation_unknown')
        self.assertTrue(all(row['state'] == 'reserved' for row in holds))
        self.service.provider_inventory = Mock(
            return_value=dict(
                backend='vastai',
                complete=True,
                observed_at=self.now,
                instance_ids=[],
            )
        )
        job = self.accepted()
        self.enabled()
        result = start_once(self.service)
        self.assertEqual(result['state'], 'accepted')
        self.assertNotEqual(result['node_id'], old)
        self.assertEqual(self.store.load(old), before)
        self.assertEqual(self.old_holds(old), holds)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)
        # The current allocation still consumes the configured lower ceiling.
        self.provisioned(result['node_id'])
        self.service.policy['max_instances'] = 1
        self.accepted()
        with patch.object(self.service, '_preview_for_owner') as planner:
            self.assertEqual(
                start_once(self.service),
                dict(state='blocked', reason_code='capacity_limit'),
            )
            planner.assert_not_called()
        self.assertEqual(len(self.client.applied), 2)

    def test_genuinely_unknown_allocation_blocks_before_any_new_plan(self):
        old = self.historical_unknown()
        before, holds = self.store.load(old), self.old_holds(old)
        job = self.accepted()
        self.enabled()
        for overrides in (
            dict(complete=False),
            dict(stale=True),
            dict(instance_ids=['unaccounted-instance']),
        ):
            inventory = {
                'backend': 'vastai',
                'complete': True,
                'observed_at': self.now,
                'instance_ids': [],
                **{k: v for k, v in overrides.items() if k != 'stale'},
            }
            if overrides.get('stale'):
                inventory['observed_at'] -= 121
            with (
                self.subTest(inventory=inventory),
                patch.object(self.service, '_preview_for_owner') as planner,
            ):
                self.service.provider_inventory = Mock(return_value=inventory)
                self.assertEqual(
                    start_once(self.service),
                    dict(state='blocked', reason_code='allocation_uncertain'),
                )
                planner.assert_not_called()
                self.assertEqual(
                    start_once(self.service),
                    dict(state='blocked', reason_code='backoff'),
                )
                self.service.provider_inventory.assert_called_once()
            self.now += 80
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.store.load(old), before)
        self.assertEqual(self.old_holds(old), holds)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_counted_stopped_history_without_absence_proof_blocks_new_plan(self):
        old = self.historical_unknown()
        with self.repo.transaction() as connection:
            node = (
                connection.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == old)
                )
                .mappings()
                .one()
            )
            payload = copy.deepcopy(node['payload'])
            payload['dstack'].update(state='stopped', observed_at=self.now)
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == old)
                .values(
                    payload=payload, desired_state='stopped', runtime_state='stopped'
                )
            )
        self.service.provider_inventory = Mock(
            return_value=dict(
                backend='vastai',
                complete=False,
                observed_at=self.now,
                instance_ids=[],
            )
        )
        before, holds = self.store.load(old), self.old_holds(old)
        job = self.accepted()
        self.enabled()
        with patch.object(self.service, '_preview_for_owner') as planner:
            self.assertEqual(
                start_once(self.service),
                dict(state='blocked', reason_code='allocation_uncertain'),
            )
            planner.assert_not_called()
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.store.load(old), before)
        self.assertEqual(self.old_holds(old), holds)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_uncertain_fleet_cooldown_prevents_another_jobs_live_read(self):
        self.historical_unknown()
        self.service.provider_inventory = Mock(
            return_value=dict(
                backend='vastai',
                complete=False,
                observed_at=self.now,
                instance_ids=[],
            )
        )
        originals = [self.accepted(), self.accepted()]
        self.enabled()
        with (
            patch.object(
                self.service,
                'prelaunch_capacity_usage',
                wraps=self.service.prelaunch_capacity_usage,
            ) as live,
            patch.object(self.service, '_preview_for_owner') as planner,
        ):
            self.assertEqual(
                start_once(self.service),
                dict(state='blocked', reason_code='allocation_uncertain'),
            )
            live.assert_called_once()
            self.assertEqual(
                start_once(self.service), dict(state='blocked', reason_code='backoff')
            )
            live.assert_called_once()
            planner.assert_not_called()
        self.assertEqual(len(self.client.applied), 1)
        for job in originals:
            self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_fresh_ready_row_with_failed_live_read_blocks_new_plan(self):
        old = self.historical_unknown()
        with self.repo.transaction() as connection:
            node = (
                connection.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == old)
                )
                .mappings()
                .one()
            )
            payload = copy.deepcopy(node['payload'])
            payload['dstack'].update(state='ready', observed_at=self.now)
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == old)
                .values(payload=payload, runtime_state='ready')
            )
        before, holds = self.store.load(old), self.old_holds(old)
        job = self.accepted()
        self.enabled()
        with (
            patch.object(self.client, 'get', side_effect=TimeoutError('read failed')),
            patch.object(self.service, '_preview_for_owner') as planner,
        ):
            self.assertEqual(
                start_once(self.service),
                dict(state='blocked', reason_code='allocation_uncertain'),
            )
            planner.assert_not_called()
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.store.load(old), before)
        self.assertEqual(self.old_holds(old), holds)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_opt_in_lookahead_floor_uses_original_start_and_does_not_duplicate_cold_slot(
        self,
    ):
        self.service.policy['demand_start']['lookahead_version'] = 1
        self.enabled()
        profile = self.service.profiles['5090-fl']
        originals = [self.accepted(), self.accepted()]
        with self.repo.transaction() as connection:
            for job in originals:
                execution = dict(
                    job['execution_plan'],
                    enabled=True,
                    backend='wangp-worker',
                    dispatch_backend='hatchet-v1',
                    configuration_id=profile['configuration_id'],
                    model_id=profile['model_id'],
                    engine_manifest_digest=profile['manifest_digest'],
                )
                stored = {
                    'deployment_profile_id': profile['runtime_profile_id'],
                    'request': {'model': profile['model_id'], 'mode': 'fl'},
                }
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == job['id'])
                    .values(execution_plan=execution, request=stored)
                )
        result = start_once(self.service)
        self.assertEqual(result['state'], 'accepted', result)
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            node = connection.execute(select(operator_nodes)).mappings().one()
            self.assertEqual(node['payload']['lifecycle']['idle_shutdown_seconds'], 600)
        # The next queued job may not allocate another floor while this exact
        # owned cold allocation already exists. The original call is not replayed.
        result = start_once(self.service)
        self.assertIn(result['state'], {'observing', 'blocked'})
        self.assertEqual(len(self.client.applied), 1)

    def test_off_is_creation_free_and_manual_start_retains_its_path(self):
        self.accepted()
        self.assertEqual(start_once(self.service), {'state': 'disabled'})
        with self.repo.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(operator_commands))), [])
        preview = self.service.preview(
            self.owner, {'profile_id': '5090-fl', 'keep_on': 'idle'}
        )
        self.service.start(
            self.owner, {'preview_id': preview['preview_id']}, 'manual-off'
        )
        self.assertEqual(len(self.client.applied), 1)

    def test_restart_observes_same_original_command_without_a_new_preview(self):
        job = self.accepted()
        self.enabled()
        with patch.object(
            self.service, '_preview_for_owner', wraps=self.service._preview_for_owner
        ) as preview:
            first = start_once(self.service)
            self.assertEqual(first['state'], 'accepted')
            with patch.object(self.service, 'prelaunch_capacity_usage') as live:
                self.assertEqual(start_once(self.service)['node_id'], first['node_id'])
                live.assert_not_called()
            preview.assert_called_once()
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)
        node = self.store.load(first['node_id'])
        self.assertEqual(node['lifecycle_version'], 2)
        with self.repo.engine.connect() as connection:
            payload = connection.execute(
                select(operator_nodes.c.payload).where(
                    operator_nodes.c.intent_id == first['node_id']
                )
            ).scalar_one()
        self.assertEqual(payload['request']['max_price_microusd'], 750000)
        self.assertEqual(payload['lifecycle']['keep_on'], 'idle')

    def test_parallel_controller_decisions_and_manual_start_never_allocate_third(self):
        for _ in range(3):
            self.accepted()
        self.enabled()
        preview = self.service.preview(
            self.owner, {'profile_id': '5090-fl', 'keep_on': 'idle'}
        )
        manual = self.service.start(
            self.owner, {'preview_id': preview['preview_id']}, 'manual-first'
        )
        self.provisioned(manual['node_id'])
        with patch.object(
            self.service, '_preview_for_owner', wraps=self.service._preview_for_owner
        ) as planner:
            results = self.parallel(lambda _: start_once(self.service), count=6)
        planner.assert_called_once()
        self.assertLessEqual(len(self.client.applied), 2)
        self.assertEqual(sum(result['state'] == 'accepted' for result in results), 1)
        self.assertEqual(len(self.client.applied), 2)
        for identity in self.client.applied:
            self.provisioned(identity)
        self.now += 80  # Past any uncertainty backoff from parallel decisions.
        self.assertEqual(
            start_once(self.service),
            {'state': 'blocked', 'reason_code': 'capacity_limit'},
        )

    def test_existing_budget_guard_refuses_without_changing_original_job_or_hold(self):
        job = self.accepted()
        self.enabled()
        self.repo.configure_budget(
            'owner-budget',
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=1,
        )
        before = self.repo.get_budget('owner-budget')
        self.assertEqual(
            start_once(self.service),
            {'state': 'blocked', 'reason_code': 'budget_limit'},
        )
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_budget('owner-budget'), before)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_auto_uses_edited_preferred_price_instead_of_its_higher_ceiling(self):
        self.settings.operator_admin_owners = ('superdan',)
        old = self.service.admin_prices(self.owner)['profiles'][0]
        self.service.update_price(
            self.owner,
            '5090-fl',
            dict(
                expected_snapshot=old['snapshot'],
                max_price_microusd=700000,
                price_ceiling_microusd=750000,
            ),
        )
        self.accepted()
        self.enabled()
        result = start_once(self.service)
        self.assertEqual(result['state'], 'accepted')
        with self.repo.engine.connect() as connection:
            payload = connection.execute(
                select(operator_nodes.c.payload).where(
                    operator_nodes.c.intent_id == result['node_id']
                )
            ).scalar_one()
        self.assertEqual(payload['request']['max_price_microusd'], 700000)
        self.assertEqual(
            next(iter(self.client.runs.values()))['run_spec']['configuration'][
                'max_price'
            ],
            0.7,
        )

    def test_429_planning_backoff_is_durable_and_respects_retry_after(self):
        self.accepted()
        self.enabled()
        with patch.object(
            self.service, '_preview_for_owner', side_effect=DstackReadRateLimited(180)
        ) as preview:
            self.assertEqual(start_once(self.service)['reason_code'], 'backoff')
            self.now += 120
            with patch.object(self.service, 'prelaunch_capacity_usage') as live:
                self.assertEqual(start_once(self.service)['reason_code'], 'backoff')
                live.assert_not_called()
            preview.assert_called_once()
        self.assertGreaterEqual(self.journal()['payload']['next_decision_at'], 1180)
        self.assertEqual(self.client.applied, [])

    def test_unknown_before_start_commit_reuses_frozen_preview_key_after_backoff(self):
        self.accepted()
        self.enabled()
        with patch.object(
            self.service,
            '_start_for_owner',
            side_effect=TimeoutError('response unknown'),
        ):
            self.assertEqual(
                start_once(self.service)['reason_code'], 'allocation_uncertain'
            )
        frozen = copy.deepcopy(self.journal()['payload'])
        with patch.object(self.service, 'prelaunch_capacity_usage') as live:
            self.assertEqual(start_once(self.service)['reason_code'], 'backoff')
            live.assert_not_called()
        self.now += 80
        with patch.object(
            self.service,
            '_preview_for_owner',
            side_effect=AssertionError('must not choose a new preview'),
        ):
            result = start_once(self.service)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.journal()['payload']['preview_id'], frozen['preview_id'])

    def test_unknown_after_original_commit_and_unproved_stopped_row_never_rerent(self):
        self.accepted()
        self.enabled()
        start = self.service._start_for_owner

        def lost(*args, **kwargs):
            start(*args, **kwargs)
            raise TimeoutError('accepted original response lost')

        with patch.object(self.service, '_start_for_owner', side_effect=lost):
            result = start_once(self.service)
        self.assertEqual(result['state'], 'observing')
        self.now += 80
        with self.repo.transaction() as connection:
            node = connection.execute(select(operator_nodes)).mappings().one()
            payload = copy.deepcopy(node['payload'])
            payload['dstack'].update(state='stopped', dstack_status='failed')
            connection.execute(
                update(operator_nodes).values(payload=payload, runtime_state='stopped')
            )
        with patch.object(
            self.service,
            '_preview_for_owner',
            side_effect=AssertionError('no new allocation evidence'),
        ):
            self.assertEqual(start_once(self.service)['state'], 'observing')
        self.assertEqual(len(self.client.applied), 1)

    def test_cancel_between_preview_and_atomic_start_creates_no_rental(self):
        job = self.accepted()
        self.enabled()
        preview = self.service._preview_for_owner

        def cancel(*args, **kwargs):
            result = preview(*args, **kwargs)
            with self.repo.transaction() as connection:
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == job['id'])
                    .values(status='cancelled')
                )
            return result

        with patch.object(self.service, '_preview_for_owner', side_effect=cancel):
            self.assertEqual(start_once(self.service)['reason_code'], 'policy_changed')
        self.assertEqual(self.client.applied, [])

    def test_kill_switch_after_preview_prevents_start_without_changing_job(self):
        job = self.accepted()
        self.enabled()
        preview = self.service._preview_for_owner

        def disable(*args, **kwargs):
            result = preview(*args, **kwargs)
            self.enabled(False)
            return result

        with patch.object(self.service, '_preview_for_owner', side_effect=disable):
            self.assertEqual(
                start_once(self.service)['reason_code'], 'authority_unavailable'
            )
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_positive_original_no_allocation_proof_retries_only_after_backoff(self):
        from studio_platform.repository import budget_reservations
        from test_dstack_allocation_proof import marker
        from test_dstack_capacity import no_capacity_run

        job = self.accepted()
        self.enabled()
        first = start_once(self.service)
        self.assertEqual(first['state'], 'accepted')
        bound = self.store.load(first['node_id'])
        run = no_capacity_run(self.client.runs[bound['run_name']])
        message = marker()
        run['latest_job_submission']['termination_reason_message'] = message
        run['jobs'][0]['job_submissions'][0]['termination_reason_message'] = message
        self.client.runs[bound['run_name']] = run
        self.capacity.observe(first['node_id'])
        with self.repo.engine.connect() as connection:
            original_hold = dict(
                connection.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_id == first['node_id']
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(
            (original_hold['state'], original_hold['actual_cost_microusd']),
            ('released', 0),
        )
        self.assertEqual(
            start_once(self.service), {'state': 'blocked', 'reason_code': 'backoff'}
        )
        self.assertEqual(len(self.client.applied), 1)
        self.now = self.journal()['payload']['next_decision_at'] + 1
        second = start_once(self.service)
        self.assertEqual(second['state'], 'accepted')
        self.assertNotEqual(second['node_id'], first['node_id'])
        self.assertEqual(len(self.client.applied), 2)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                dict(
                    connection.execute(
                        select(budget_reservations).where(
                            budget_reservations.c.reference_id == first['node_id']
                        )
                    )
                    .mappings()
                    .one()
                ),
                original_hold,
            )
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def close_slow_allocation(self, node_id, machine, *, proof=True):
        from test_host_policy import physical
        from studio_platform.repository import instance_intents

        instance = str(60000000 + len(self.client.applied))
        with self.repo.transaction() as c:
            node = dict(
                c.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                )
                .mappings()
                .one()
            )
            payload = copy.deepcopy(node['payload'])
            payload['host_observation'] = physical(machine, instance, self.now)
            payload['dstack'].update(
                provider_instance_id=instance, state='runtime_unconfirmed'
            )
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=payload)
            )
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node_id)
                .values(provider_instance_id=instance, state='running')
            )
            node['payload'] = payload
            intent = dict(
                c.execute(
                    select(instance_intents).where(instance_intents.c.id == node_id)
                )
                .mappings()
                .one()
            )
            payload = self.service.record_download_timebox(c, node, intent, self.now)
            self.assertIsNotNone(payload)
            payload['stop_reason_code'] = 'dstack_download_timebox_exceeded'
            payload['dstack'].update(state='stopped', dstack_status='terminated')
            if proof:
                payload['dstack']['provider_removal_proof'] = dict(
                    version=1,
                    complete=True,
                    absent=True,
                    backend='vastai',
                    run_id=payload['dstack']['run_id'],
                    provider_instance_id=instance,
                )
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    payload=payload, desired_state='stopped', runtime_state='stopped'
                )
            )
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node_id)
                .values(state='destroyed')
            )
        self.client.runs[payload['dstack']['run_name']].update(
            status='terminated',
            latest_job_submission=dict(
                job_provisioning_data=dict(
                    backend='vastai',
                    instance_id=instance,
                    instance_type=dict(resources=dict(gpus=[dict(name='RTX5090')])),
                )
            ),
        )
        self.service.provider_inventory = Mock(
            side_effect=lambda backend: dict(
                backend='vastai', complete=True, observed_at=self.now, instance_ids=[]
            )
        )
        return payload

    def test_download_replacements_bounded_original_job_history_and_single_journal(
        self,
    ):
        job = self.accepted()
        self.enabled()
        for index in range(3):
            machine = str(19493 + index)
            self.client.vast_offer_mapping = lambda *args, _machine=machine, **kw: dict(
                observed_at=self.now,
                offers=[
                    dict(
                        offer_id='12345',
                        machine_id=_machine,
                        country='US',
                        region='test-region',
                        advertised_download_mbps=2000,
                    )
                ],
            )
            result = start_once(self.service)
            self.assertEqual(result['state'], 'accepted')
            stopped = self.close_slow_allocation(result['node_id'], machine)
            self.assertFalse(stopped['host_failure']['qualified_start_failure'])
            self.assertEqual(stopped['download_timebox_event']['machine_id'], machine)
            outcome = start_once(self.service)
            if index < 2:
                self.assertEqual(outcome, dict(state='blocked', reason_code='backoff'))
                self.now = self.journal()['payload']['next_decision_at'] + 1
            else:
                self.assertEqual(
                    outcome, dict(state='blocked', reason_code='download_retry_limit')
                )
        with patch.object(self.service, '_preview_for_owner') as planner:
            for _ in range(3):
                self.now += 1000
                self.assertEqual(
                    start_once(self.service),
                    dict(state='blocked', reason_code='download_retry_limit'),
                )
            planner.assert_not_called()
        self.assertEqual(len(self.client.applied), 3)
        self.assertEqual(self.journal()['payload']['download_replacements'], 2)
        self.assertEqual(len(self.journal()['payload']['download_stops']), 3)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_slow_stop_without_removal_proof_never_reoffers_and_creator_sees_starting(
        self,
    ):
        from studio_platform.dstack_demand import job_wait_stage

        job = self.accepted()
        self.enabled()
        result = start_once(self.service)
        self.close_slow_allocation(result['node_id'], '19493', proof=False)
        with patch.object(self.service, '_preview_for_owner') as planner:
            self.assertEqual(start_once(self.service)['state'], 'observing')
            self.now += 1000
            self.assertEqual(start_once(self.service)['state'], 'observing')
            planner.assert_not_called()
        with self.repo.engine.connect() as c:
            self.assertEqual(job_wait_stage(c, job, self.now), 'machine_starting')
        self.assertEqual(self.journal()['payload'].get('download_replacements', 0), 0)
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_download_exclusion_is_job_scoped_and_preferred_country_keeps_fallback(
        self,
    ):
        job = self.accepted()
        self.enabled()
        result = start_once(self.service)
        self.close_slow_allocation(result['node_id'], '19493')
        start_once(self.service)
        self.now = self.journal()['payload']['next_decision_at'] + 1
        # The sole mapped machine is the original slow physical host: no re-rent.
        self.assertEqual(start_once(self.service)['reason_code'], 'offer_unavailable')
        self.assertEqual(len(self.client.applied), 1)
        # Manual start retains its independent path; no new global deny store.
        preview = self.service.preview(
            self.owner, dict(profile_id='5090-fl', keep_on='idle')
        )
        self.assertTrue(preview['offers'])
        self.assertEqual(self.repo.get_job(self.scope, job['id']), job)

    def test_us_eu_offer_preference_preserves_other_qualifying_fallback(self):
        native = self.client.plan

        def plan(spec):
            result = native(spec)
            first = result['job_plans'][0]['offers'][0]
            first['price'] = 0.5
            second = copy.deepcopy(first)
            second['instance']['name'] = '54321'
            second['price'] = 0.7
            result['job_plans'][0]['offers'].append(second)
            return result

        self.client.plan = plan
        self.client.vast_offer_mapping = lambda *a, **kw: dict(
            observed_at=self.now,
            offers=[
                dict(
                    offer_id='12345',
                    machine_id='19493',
                    country='JP',
                    region='jp-tokyo',
                    advertised_download_mbps=2000,
                ),
                dict(
                    offer_id='54321',
                    machine_id='19494',
                    country='Germany',
                    region='de-berlin',
                    advertised_download_mbps=1500,
                ),
            ],
        )
        preview = self.service.preview(
            self.owner, dict(profile_id='5090-fl', keep_on='idle')
        )
        self.assertEqual([r['offer_id'] for r in preview['offers']], ['54321', '12345'])
        self.client.vast_offer_mapping = lambda *a, **kw: dict(
            observed_at=self.now,
            offers=[
                dict(
                    offer_id='12345',
                    machine_id='19493',
                    country='JP',
                    region='jp-tokyo',
                    advertised_download_mbps=2000,
                )
            ],
        )
        fallback = self.service.preview(
            self.owner, dict(profile_id='5090-fl', keep_on='idle')
        )
        self.assertEqual([r['offer_id'] for r in fallback['offers']], ['12345'])

    def test_fresh_bandwidth_floor_drift_refuses_before_paid_apply(self):
        preview = self.service.preview(
            self.owner, dict(profile_id='5090-fl', keep_on='idle')
        )
        for bandwidth in (None, True, float('nan'), 999):
            self.client.vast_offer_mapping = lambda *a, _rate=bandwidth, **kw: dict(
                observed_at=self.now,
                offers=[
                    dict(
                        offer_id='12345',
                        machine_id='19493',
                        advertised_download_mbps=_rate,
                    )
                ],
            )
            with (
                self.subTest(rate=str(bandwidth)),
                self.assertRaisesRegex(ValueError, 'host_offer_identity_changed'),
            ):
                self.service.start(
                    self.owner, {'preview_id': preview['preview_id']}, 'no-paid-drift'
                )
            self.assertEqual(self.client.applied, [])
