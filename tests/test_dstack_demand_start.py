"""Original-command planning, response-loss and capacity fences; fake HTTP only."""

import copy
from unittest.mock import patch

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
from studio_platform.repository import jobs, request_hash
from test_platform_repository import LedgerCase
import test_dstack_operator as original


class DemandStartTests(LedgerCase):
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
            self.assertEqual(start_once(self.service)['node_id'], first['node_id'])
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
        self.service.start(
            self.owner, {'preview_id': preview['preview_id']}, 'manual-first'
        )
        with patch.object(
            self.service, '_preview_for_owner', wraps=self.service._preview_for_owner
        ) as planner:
            results = self.parallel(lambda _: start_once(self.service), count=6)
        planner.assert_called_once()
        self.assertLessEqual(len(self.client.applied), 2)
        self.assertEqual(sum(result['state'] == 'accepted' for result in results), 1)
        self.assertEqual(len(self.client.applied), 2)
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
            self.assertEqual(start_once(self.service)['reason_code'], 'backoff')
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
