"""Exact human review authority, capacity and guardian boundaries; offline only."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import delete, insert, select, text, update

from studio_platform.auth import Principal
from studio_platform.operator_capacity import (
    OperatorError,
    operator_commands,
    operator_nodes,
    operator_heartbeats,
    MANUAL_REVIEW_CONTROLLER_PREFIX,
)
from studio_platform.repository import (
    instance_intents,
    scaler_actions,
    scaler_receipts,
    manually_reviewed_inactive,
    registered_workers,
    registered_devices,
    attempts,
    jobs,
)
import test_operator_capacity as operator_fixtures
import test_targon_cleanup as guardian_fixtures
from tools.targon_cleanup_guard import (
    OperatorManualReviewReader,
    ProtectedManualReviewReader,
)
from tools import targon_cleanup_guard as review_guard
from studio_platform.scaler import LaunchSpec, ProviderFact
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.targon_cleanup import _hash


class ManualReviewTests(operator_fixtures.OperatorTests):
    def setUp(self):
        operator_fixtures.OperatorTests.setUp(self)
        self.controller.leader_id = MANUAL_REVIEW_CONTROLLER_PREFIX + 'offline'
        created = operator_fixtures.OperatorTests.create(self)
        with self.repo.engine.connect() as connection:
            command = (
                connection.execute(
                    select(operator_commands).where(
                        operator_commands.c.id == created['id']
                    )
                )
                .mappings()
                .one()
            )
        self.controller._start(command)
        node = self.service.state(self.actor)['nodes'][0]
        self.service.node_command(
            self.actor,
            node['id'],
            {'expected_version': node['version']},
            'stop-before-review',
            'stop',
        )
        self.uid = 'wrk-offline-review'
        self.node_id = node['id']
        with self.repo.transaction() as connection:
            connection.execute(
                insert(operator_heartbeats).values(
                    id='global',
                    controller_id=self.controller.leader_id,
                    observed_at=self.now,
                    state='running',
                )
            )
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.node_id)
                .values(
                    provider='targon', state='destroying', provider_instance_id=self.uid
                )
            )
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == self.node_id)
                .values(
                    destroy_started_at=self.now,
                    last_observation={'state': 'unknown', 'instance_id': self.uid},
                )
            )
        self.body = self.body_now()

    def body_now(self):
        node = self.service.state(self.actor)['nodes'][0]
        return {
            'expected_version': node['version'],
            'provider_instance_id': self.uid,
            'account_absent': True,
            'no_continuing_charge': True,
        }

    def known_expired_worker(
        self, *, state='draining', worker_id='expired-bound', gpu_id='GPU-old'
    ):
        self.binding = replace(
            self.binding,
            launch=LaunchSpec(
                'targon', self.binding.configuration_id, self.binding.model_id
            ),
        )
        self.registry.bindings[self.binding.binding_id] = self.binding
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == self.node_id)
                .values(binding_hash=self.binding.fingerprint)
            )
        spec = WorkerSpec(
            worker_id,
            self.binding.pool,
            'targon',
            self.uid,
            (gpu_id,),
            self.binding.recipe_ids,
            self.binding.model_id,
            self.binding.configuration_id,
            backend='wangp-worker',
            engine_manifest_digest=self.binding.engine_manifest_digest,
        )
        control = WorkerControl(self.repo)
        worker = control.register(spec)
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == worker_id)
                .values(state=state, expires_at=self.now - 30, drain_requested=1)
            )
        return worker

    def test_exact_review_preserves_reservation_deadline_and_provider_state(self):
        before = self.repo.list_instance_intents()[0]
        budget = self.repo.get_budget('owner-budget')
        result = self.service.manual_review(
            self.actor, self.node_id, self.body, 'review-once'
        )
        self.assertEqual(
            self.service.manual_review(
                self.actor, self.node_id, self.body, 'review-once'
            ),
            result,
        )
        after = self.repo.list_instance_intents()[0]
        for key in (
            'state',
            'provider_instance_id',
            'hard_deadline',
            'reserved_cost_microusd',
            'actual_cost_microusd',
            'billing_status',
        ):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['state'], 'destroying')
        self.assertEqual(after['billing_status'], 'pending')
        self.assertEqual(self.repo.get_budget('owner-budget'), budget)
        snapshot = self.service.state(self.actor)
        self.assertEqual(snapshot['summary']['nodes_active'], 0)
        self.assertEqual(snapshot['summary']['gpus_allocated'], 0)
        self.assertEqual(snapshot['summary']['hourly_cost_microusd'], 0)
        proof = snapshot['nodes'][0]['removal_confirmation']
        self.assertEqual(proof['state'], 'manually_reviewed')
        self.assertIsNone(proof['next_check_at'])
        self.assertEqual(proof['manual_review']['actor'], 'superdan')
        self.assertFalse(snapshot['nodes'][0]['actions']['manual_review']['allowed'])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                len(
                    list(
                        connection.execute(
                            select(scaler_receipts).where(
                                scaler_receipts.c.operation == 'manual_review'
                            )
                        )
                    )
                ),
                1,
            )
        # No constructor, provider query, invoice poll or bootstrap after closure.
        self.controller.provider_factory = Mock(
            side_effect=AssertionError('provider accessed')
        )
        self.controller.tick()
        self.controller.provider_factory.assert_not_called()
        self.assertEqual(
            self.repo.list_instance_intents()[0]['billing_status'], 'pending'
        )

    def test_operator_cookie_attestation_identity_version_and_replay_conflicts(self):
        for actor in (
            None,
            Principal('outsider', 'browser'),
            Principal('superdan', 'pat', machine=True),
        ):
            with self.assertRaises(OperatorError):
                self.service.manual_review(actor, self.node_id, self.body, 'denied')
        for patch, code in (
            ({'account_absent': False}, 'attestation_required'),
            ({'no_continuing_charge': False}, 'attestation_required'),
            ({'provider_instance_id': 'wrk-other'}, 'identity_mismatch'),
            ({'expected_version': 'wrong'}, 'version_conflict'),
        ):
            with self.assertRaisesRegex(OperatorError, code):
                self.service.manual_review(
                    self.actor, self.node_id, {**self.body, **patch}, 'bad-review'
                )
        self.service.manual_review(self.actor, self.node_id, self.body, 'once')
        with self.assertRaisesRegex(OperatorError, 'idempotency_conflict'):
            self.service.manual_review(
                self.actor,
                self.node_id,
                {**self.body, 'provider_instance_id': 'wrk-other'},
                'once',
            )
        with self.assertRaisesRegex(OperatorError, 'manually_reviewed'):
            self.service.manual_review(
                self.actor, self.node_id, self.body_now(), 'new-key'
            )

    def test_http_mark_is_operator_only_read_free_and_exact_replayable(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from studio_platform.operator_routes import register_routes

        app = FastAPI()
        actor = [Principal('outsider', 'browser')]

        @app.middleware('http')
        async def injected_principal(request: Request, next_call):
            request.state.principal = actor[0]
            return await next_call(request)

        register_routes(app, service=self.service)
        path = '/v1/operator/capacity/nodes/' + self.node_id + '/manual-review'
        with TestClient(app) as client:
            self.assertEqual(
                client.post(
                    path, json=self.body, headers={'Idempotency-Key': 'api-once'}
                ).status_code,
                403,
            )
            actor[0] = self.actor
            self.assertEqual(client.post(path, json=self.body).status_code, 422)
            response = client.post(
                path, json=self.body, headers={'Idempotency-Key': 'api-once'}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            replay = client.post(
                path, json=self.body, headers={'Idempotency-Key': 'api-once'}
            )
            self.assertEqual(replay.json(), response.json())

    def test_review_requires_supported_exact_started_instance(self):
        for values, code in (
            ({'state': 'reserved'}, 'instance_unconfirmed'),
            ({'provider_instance_id': None}, 'identity_mismatch'),
            ({'provider': 'vastai'}, 'provider_unsupported'),
        ):
            with self.repo.transaction() as connection:
                connection.execute(update(instance_intents).values(**values))
            with self.assertRaisesRegex(OperatorError, code):
                self.service.manual_review(
                    self.actor, self.node_id, self.body_now(), 'invalid-state'
                )
            with self.repo.transaction() as connection:
                connection.execute(
                    update(instance_intents).values(
                        state='destroying',
                        provider='targon',
                        provider_instance_id=self.uid,
                    )
                )

    def ambiguous_creation(self):
        with self.repo.transaction() as connection:
            connection.execute(delete(operator_heartbeats))
            connection.execute(
                delete(operator_commands).where(operator_commands.c.kind == 'stop')
            )
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.node_id)
                .values(
                    provider='lium', state='creation_unknown', provider_instance_id=None
                )
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == self.node_id)
                .values(desired_state='running', runtime_state='creation_unknown')
            )
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == self.node_id)
                .values(destroy_started_at=None)
            )
        return {**self.body_now(), 'provider_instance_id': None}

    def test_unbound_creation_review_retains_null_identity_unknown_creation_and_budget(
        self,
    ):
        body = self.ambiguous_creation()
        before = self.repo.list_instance_intents()[0]
        budget = self.repo.get_budget('owner-budget')
        result = self.service.manual_review(
            self.actor, self.node_id, body, 'ambiguous-human-review'
        )
        self.assertEqual(
            self.service.manual_review(
                self.actor, self.node_id, body, 'ambiguous-human-review'
            ),
            result,
        )
        after = self.repo.list_instance_intents()[0]
        for field in (
            'state',
            'provider_instance_id',
            'hard_deadline',
            'reserved_cost_microusd',
            'billing_status',
        ):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after['state'], 'creation_unknown')
        self.assertIsNone(after['manual_review']['instance_id'])
        self.assertEqual(
            after['manual_review']['removal_source'], 'operator_attestation'
        )
        self.assertEqual(self.repo.get_budget('owner-budget'), budget)
        self.assertEqual(self.service.state(self.actor)['summary']['nodes_active'], 0)

    def test_null_review_requires_exact_null_and_ambiguous_creation_state(self):
        body = self.ambiguous_creation()
        with self.assertRaisesRegex(OperatorError, 'identity_mismatch'):
            self.service.manual_review(
                self.actor,
                self.node_id,
                {**body, 'provider_instance_id': 'invented'},
                'not-null',
            )
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).values(state='ready'))
        with self.assertRaisesRegex(OperatorError, 'instance_unconfirmed'):
            self.service.manual_review(
                self.actor,
                self.node_id,
                {**body, 'expected_version': self.body_now()['expected_version']},
                'null-ready',
            )

    def test_null_review_rejects_original_pool_obligations_without_guessing_worker_identity(
        self,
    ):
        body = self.ambiguous_creation()
        job = self.job(status='queued')
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job['id'])
                .values(pool=self.binding.pool)
            )
        with self.assertRaisesRegex(OperatorError, 'worker_active'):
            self.service.manual_review(self.actor, self.node_id, body, 'queued-in-pool')
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job['id']).values(status='failed')
            )
            connection.execute(
                insert(registered_workers).values(
                    id='pool-actor',
                    pool=self.binding.pool,
                    provider='lium',
                    instance_id='unknown-other',
                    spec={},
                    spec_hash='a' * 64,
                    state='ready',
                    current_job_id=None,
                    drain_requested=1,
                    fence=1,
                    expires_at=self.now - 30,
                    updated_at=self.now,
                )
            )
        with self.assertRaisesRegex(OperatorError, 'worker_active'):
            self.service.manual_review(
                self.actor, self.node_id, body, 'expired-unbound-actor'
            )
        with self.repo.transaction() as connection:
            connection.execute(update(registered_workers).values(state='retired'))
            connection.execute(
                insert(registered_devices).values(
                    provider='lium',
                    instance_id='unknown-other',
                    gpu_id='GPU-ambiguous',
                    worker_id='pool-actor',
                    state='owned',
                )
            )
        with self.assertRaisesRegex(OperatorError, 'device_unreleased'):
            self.service.manual_review(self.actor, self.node_id, body, 'unbound-device')
        with self.repo.transaction() as connection:
            connection.execute(update(registered_devices).values(state='released'))
            connection.execute(
                insert(attempts).values(
                    id='unknown-pool-attempt',
                    job_id=job['id'],
                    number=1,
                    status='failed',
                    fence=1,
                    worker_id='pool-actor',
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now - 1,
                    upstream_stopped=0,
                )
            )
        with self.assertRaisesRegex(OperatorError, 'attempt_unsafe'):
            self.service.manual_review(
                self.actor, self.node_id, body, 'unbound-attempt'
            )
        with self.repo.engine.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    select(scaler_receipts.c.id).where(
                        scaler_receipts.c.operation == 'manual_review'
                    )
                ).scalar()
            )

    def assert_dashboard_review(self, provider, state):
        with self.repo.transaction() as connection:
            connection.execute(delete(operator_heartbeats))
            connection.execute(
                delete(operator_commands).where(operator_commands.c.kind == 'stop')
            )
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.node_id)
                .values(provider=provider, state=state)
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == self.node_id)
                .values(desired_state='running', runtime_state=state)
            )
            connection.execute(update(scaler_actions).values(destroy_started_at=None))
        before = self.repo.list_instance_intents()[0]
        budget = self.repo.get_budget('owner-budget')
        body = self.body_now()
        result = self.service.manual_review(
            self.actor, self.node_id, body, 'dashboard-review'
        )
        self.assertEqual(
            self.service.manual_review(
                self.actor, self.node_id, body, 'dashboard-review'
            ),
            result,
        )
        after = self.repo.list_instance_intents()[0]
        for field in (
            'state',
            'provider',
            'provider_instance_id',
            'hard_deadline',
            'reserved_cost_microusd',
            'billing_status',
            'actual_cost_microusd',
        ):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.repo.get_budget('owner-budget'), budget)
        node = self.service.state(self.actor)['nodes'][0]
        self.assertEqual(
            (node['desired_state'], node['runtime_state'], node['record_group']),
            ('stopped', 'manually_reviewed', 'history'),
        )
        review = node['removal_confirmation']['manual_review']
        self.assertEqual(
            (
                review['provider'],
                review['original_intent_state'],
                review['removal_source'],
            ),
            (provider, state, 'operator_attestation'),
        )
        self.assertIsNone(review['stop_operation_id'])
        self.assertIsNone(node['removal_confirmation']['next_check_at'])
        self.assertEqual(self.service.state(self.actor)['summary']['nodes_active'], 0)
        self.assertFalse(node['actions']['manual_review']['allowed'])
        self.controller.provider_factory = Mock(
            side_effect=AssertionError('provider accessed')
        )
        self.controller.tick()
        self.controller.provider_factory.assert_not_called()

    def test_lium_dashboard_stop_without_platform_stop_or_controller(self):
        self.assert_dashboard_review('lium', 'ready')

    def test_targon_dashboard_stop_without_platform_stop_or_controller(self):
        self.assert_dashboard_review('targon', 'starting')

    def test_review_receipt_cannot_follow_changed_provider_or_intent_state(self):
        self.service.manual_review(
            self.actor, self.node_id, self.body, 'exact-reviewed-binding'
        )
        with self.repo.transaction() as connection:
            original = (
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == self.node_id
                    )
                )
                .mappings()
                .one()
            )
            self.assertIsNotNone(manually_reviewed_inactive(connection, original))
            self.assertIsNone(
                manually_reviewed_inactive(connection, {**original, 'provider': 'lium'})
            )
            self.assertIsNone(
                manually_reviewed_inactive(connection, {**original, 'state': 'ready'})
            )
            row = (
                connection.execute(
                    select(scaler_receipts).where(
                        scaler_receipts.c.operation == 'manual_review'
                    )
                )
                .mappings()
                .one()
            )
            legacy = {
                key: value
                for key, value in row['facts'].items()
                if key not in {'provider', 'original_intent_state', 'removal_source'}
            }
            connection.execute(
                update(scaler_receipts)
                .where(scaler_receipts.c.id == row['id'])
                .values(facts=legacy)
            )
            self.assertIsNotNone(manually_reviewed_inactive(connection, original))
            self.assertIsNone(
                manually_reviewed_inactive(connection, {**original, 'provider': 'lium'})
            )
            self.assertIsNone(
                manually_reviewed_inactive(connection, {**original, 'state': 'ready'})
            )

    def test_fresh_worker_or_unreleased_device_blocks_even_with_human_attestation(self):
        with self.repo.transaction() as connection:
            connection.execute(
                insert(registered_workers).values(
                    id='bound-worker',
                    pool=self.binding.pool,
                    provider='targon',
                    instance_id=self.uid,
                    spec={},
                    spec_hash='a' * 64,
                    state='ready',
                    current_job_id=None,
                    drain_requested=1,
                    fence=1,
                    expires_at=self.now + 30,
                    updated_at=self.now,
                )
            )
        # State DTO needs only the existing safe worker spec projection.
        with self.assertRaisesRegex(OperatorError, 'worker_active'):
            self.service.manual_review(
                self.actor, self.node_id, self.body, 'active-worker'
            )
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers).values(
                    state='retired', expires_at=self.now - 1
                )
            )
            connection.execute(
                insert(registered_devices).values(
                    provider='targon',
                    instance_id=self.uid,
                    gpu_id='GPU-old',
                    worker_id='bound-worker',
                    state='reserved',
                )
            )
        with self.assertRaisesRegex(OperatorError, 'binding_unconfirmed'):
            self.service.manual_review(
                self.actor, self.node_id, self.body, 'device-held'
            )

    def assert_expired_local_release(self, state):
        worker = self.known_expired_worker(
            state=state, worker_id='expired-' + state, gpu_id='GPU-' + state
        )
        self.assertTrue(
            self.service.state(self.actor)['nodes'][0]['actions']['manual_review'][
                'allowed'
            ]
        )
        before = self.repo.list_instance_intents()[0]
        budget = self.repo.get_budget('owner-budget')
        body = self.body_now()
        key = 'review-expired-' + state
        result = self.service.manual_review(self.actor, self.node_id, body, key)
        with self.repo.engine.connect() as connection:
            after = (
                connection.execute(
                    select(registered_workers).where(
                        registered_workers.c.id == worker['id']
                    )
                )
                .mappings()
                .one()
            )
            device = (
                connection.execute(
                    select(registered_devices).where(
                        registered_devices.c.worker_id == worker['id']
                    )
                )
                .mappings()
                .one()
            )
            review = connection.execute(
                select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.operation == 'manual_review'
                )
            ).scalar_one()
        self.assertEqual(
            (after['state'], after['fence'], device['state']),
            ('retired', worker['fence'] + 1, 'released'),
        )
        audit = review['local_execution_release']
        self.assertFalse(audit['provider_removal_confirmed'])
        self.assertFalse(audit['billing_settled'])
        self.assertEqual(audit['workers'][0]['worker_id'], worker['id'])
        self.assertEqual(audit['workers'][0]['devices'][0]['gpu_id'], 'GPU-' + state)
        self.assertEqual(self.repo.get_budget('owner-budget'), budget)
        after_intent = self.repo.list_instance_intents()[0]
        for field in (
            'state',
            'hard_deadline',
            'provider_instance_id',
            'reserved_cost_microusd',
            'actual_cost_microusd',
            'billing_status',
        ):
            self.assertEqual(after_intent[field], before[field])
        self.assertEqual(
            self.service.manual_review(self.actor, self.node_id, body, key), result
        )
        self.assertEqual(
            WorkerControl(self.repo).get(worker['id'])['fence'], after['fence']
        )
        with self.assertRaisesRegex(Exception, 'lease_lost'):
            WorkerControl(self.repo).heartbeat(worker['id'], worker['fence'])

    def test_known_expired_draining_worker_release_is_atomic_audited_and_replay_safe(
        self,
    ):
        self.assert_expired_local_release('draining')

    def test_known_expired_retired_worker_device_can_be_locally_released(self):
        self.assert_expired_local_release('retired')

    def test_expired_local_release_rejects_unknown_binding_or_orphan_cross_node_device(
        self,
    ):
        worker = self.known_expired_worker()
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == worker['id'])
                .values(spec_hash='f' * 64)
            )
        with self.assertRaisesRegex(OperatorError, 'binding_unconfirmed'):
            self.service.manual_review(
                self.actor, self.node_id, self.body_now(), 'unknown-spec'
            )
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == worker['id'])
                .values(spec_hash=worker['spec_hash'])
            )
            connection.execute(
                insert(registered_devices).values(
                    provider='targon',
                    instance_id='wrk-other',
                    gpu_id='GPU-cross',
                    worker_id=worker['id'],
                    state='owned',
                )
            )
        with self.assertRaisesRegex(OperatorError, 'device_unreleased'):
            self.service.manual_review(
                self.actor, self.node_id, self.body_now(), 'cross-node'
            )
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_devices)
                .where(registered_devices.c.gpu_id == 'GPU-cross')
                .values(state='released')
            )
            connection.execute(
                insert(registered_workers).values(
                    {**worker, 'id': 'foreign-worker', 'instance_id': 'wrk-other'}
                )
            )
            connection.execute(
                insert(registered_devices).values(
                    provider='targon',
                    instance_id=self.uid,
                    gpu_id='GPU-orphan',
                    worker_id='foreign-worker',
                    state='owned',
                )
            )
        with self.assertRaisesRegex(OperatorError, 'device_unreleased'):
            self.service.manual_review(
                self.actor, self.node_id, self.body_now(), 'orphan-owner'
            )
        self.assertEqual(
            WorkerControl(self.repo).get(worker['id'])['state'], 'draining'
        )
        self.assertEqual(
            WorkerControl(self.repo).get(worker['id'])['fence'], worker['fence']
        )

    def test_expired_local_release_requires_safe_all_attempt_history_and_preserves_jobs(
        self,
    ):
        worker = self.known_expired_worker()
        job = self.job(status='queued')
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job['id']).values(status='failed')
            )
            connection.execute(
                insert(attempts).values(
                    id='terminal-but-unknown',
                    job_id=job['id'],
                    number=1,
                    status='failed',
                    fence=1,
                    worker_id=worker['id'],
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now - 1,
                    upstream_task_id='original-upstream',
                    upstream_stopped=0,
                )
            )
        with self.assertRaisesRegex(OperatorError, 'attempt_unsafe'):
            self.service.manual_review(
                self.actor, self.node_id, self.body_now(), 'unsafe-local-review'
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(registered_devices.c.state).where(
                        registered_devices.c.worker_id == worker['id']
                    )
                ).scalar_one(),
                'owned',
            )
            self.assertEqual(
                list(
                    connection.execute(
                        select(scaler_receipts).where(
                            scaler_receipts.c.operation == 'manual_review'
                        )
                    )
                ),
                [],
            )
        self.assertEqual(
            WorkerControl(self.repo).get(worker['id'])['fence'], worker['fence']
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == 'terminal-but-unknown')
                .values(upstream_stopped=1)
            )
        original_job = self.repo.get_job(self.scope, job['id'])
        with self.repo.engine.connect() as connection:
            original_attempt = dict(
                connection.execute(
                    select(attempts).where(attempts.c.id == 'terminal-but-unknown')
                )
                .mappings()
                .one()
            )
        self.service.manual_review(
            self.actor, self.node_id, self.body_now(), 'proven-local-review'
        )
        self.assertEqual(self.repo.get_job(self.scope, job['id']), original_job)
        with self.repo.engine.connect() as connection:
            after = dict(
                connection.execute(
                    select(attempts).where(attempts.c.id == 'terminal-but-unknown')
                )
                .mappings()
                .one()
            )
            review = connection.execute(
                select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.operation == 'manual_review'
                )
            ).scalar_one()
        self.assertEqual(after, original_attempt)
        self.assertEqual(
            review['local_execution_release']['workers'][0]['terminal_attempt_ids'],
            ['terminal-but-unknown'],
        )

    def test_unsafe_attempt_blocks_and_late_provider_cost_cannot_settle_manual_review(
        self,
    ):
        job = self.job(status='queued')
        with self.repo.transaction() as connection:
            connection.execute(
                insert(registered_workers).values(
                    id='expired-worker',
                    pool=self.binding.pool,
                    provider='targon',
                    instance_id=self.uid,
                    spec={},
                    spec_hash='a' * 64,
                    state='retired',
                    current_job_id=None,
                    drain_requested=1,
                    fence=1,
                    expires_at=self.now - 30,
                    updated_at=self.now,
                )
            )
            connection.execute(
                update(jobs).where(jobs.c.id == job['id']).values(status='failed')
            )
            connection.execute(
                insert(attempts).values(
                    id='attempt-offline',
                    job_id=job['id'],
                    number=1,
                    status='failed',
                    fence=1,
                    worker_id='expired-worker',
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now - 1,
                    upstream_task_id='original-upstream',
                    upstream_stopped=0,
                )
            )
        with self.assertRaisesRegex(OperatorError, 'attempt_unsafe'):
            self.service.manual_review(
                self.actor, self.node_id, self.body, 'unsafe-attempt'
            )
        with self.repo.transaction() as connection:
            connection.execute(update(attempts).values(upstream_stopped=1))
        self.service.manual_review(self.actor, self.node_id, self.body, 'safe-reviewed')
        coordinator = self.controller._coordinator(self.binding)
        lease = coordinator.acquire(self.binding.pool, self.controller.leader_id)
        coordinator._apply(
            lease,
            self.node_id,
            ProviderFact(
                'destroyed', instance_id=self.uid, actual_cost_microusd=540000
            ),
            self.now,
        )
        instance = self.repo.list_instance_intents()[0]
        self.assertEqual(instance['state'], 'destroying')
        self.assertIsNone(instance['actual_cost_microusd'])
        self.assertEqual(instance['billing_status'], 'pending')

    def test_reviewed_inactive_exclusion_applies_to_global_and_pool_reservation_barriers(
        self,
    ):
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(
            self.binding.pool, max_instances=1, max_physical_gpus=1
        )
        self.service.manual_review(
            self.actor, self.node_id, self.body, 'capacity-review'
        )
        original = self.repo.list_instance_intents()[0]
        next_intent = self.repo.reserve_instance_intent(
            self.scope,
            self.binding.pool,
            'new-distinct-intent',
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=1000000,
            hard_deadline=self.now + 2000,
            budget_account_ids=('owner-budget',),
            provider='lium',
            dry_run=False,
        )
        self.assertTrue(next_intent['created'])
        self.assertEqual(
            self.repo.list_instance_intents()[0]['reserved_cost_microusd'],
            original['reserved_cost_microusd'],
        )

    def test_postgres_guardian_reader_executes_exact_audit_query(self):
        if self.repo.engine.dialect.name != 'postgresql':
            self.skipTest('requires explicitly configured local test PostgreSQL')
        self.known_expired_worker()
        self.service.manual_review(
            self.actor, self.node_id, self.body_now(), 'pg-reader-review'
        )
        container = 'a' * 64
        queries = []

        def run(arguments, **options):
            if arguments[1] == 'inspect':
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(container)
                    + ' '
                    + json.dumps(
                        {
                            'com.docker.compose.project': 'sixnine-platform',
                            'com.docker.compose.service': 'db',
                        }
                    ),
                )
            wrapper = options['input']
            self.assertTrue(
                wrapper.startswith('BEGIN READ ONLY;\n')
                and wrapper.endswith('\nCOMMIT;')
            )
            query = wrapper.removeprefix('BEGIN READ ONLY;\n').removesuffix('\nCOMMIT;')
            queries.append(query)
            with self.repo.engine.begin() as connection:
                connection.execute(text('SET TRANSACTION READ ONLY'))
                value = connection.exec_driver_sql(query).scalar_one_or_none()
            return SimpleNamespace(returncode=0, stdout=(value or '') + '\n')

        reader = OperatorManualReviewReader(container, run=run)
        review = reader(self.uid)
        self.assertEqual(review['source'], 'operator_database')
        self.assertEqual(review['state'], 'manually_reviewed')
        self.assertEqual(review['instance_id'], self.uid)
        self.assertEqual(review['intent_id'], self.node_id)
        self.assertEqual(review['actor'], 'superdan')
        self.assertIs(review['account_absent'], True)
        self.assertIs(review['no_continuing_charge'], True)
        self.assertIsNone(reader('wrk-other-not-reviewed'))
        with self.repo.engine.connect() as connection:
            command = (
                connection.execute(
                    select(operator_commands).where(
                        operator_commands.c.id == review['operation_id']
                    )
                )
                .mappings()
                .one()
            )
        for field in ('account_absent', 'no_continuing_charge'):
            with self.repo.transaction() as connection:
                connection.execute(
                    update(operator_commands)
                    .where(operator_commands.c.id == command['id'])
                    .values(payload={**command['payload'], field: 'true'})
                )
            self.assertIsNone(
                reader(self.uid)
            )  # A JSON string cannot attest the boolean fact.
        self.assertEqual(len(queries), 4)


class GuardianReviewTests(guardian_fixtures.TargonCleanupTests):
    # Reuse fixtures only, without rerunning their unrelated inherited cases.
    def guardian_with_review(self, reader):
        guardian = self.guardian()
        guardian.manual_review_reader = reader
        return guardian

    def review(self):
        return {
            'schema_version': 1,
            'state': 'manually_reviewed',
            'intent_id': '1' * 8
            + '-'
            + '1' * 4
            + '-'
            + '1' * 4
            + '-'
            + '1' * 4
            + '-'
            + '1' * 12,
            'operation_id': '2' * 8
            + '-'
            + '2' * 4
            + '-'
            + '2' * 4
            + '-'
            + '2' * 4
            + '-'
            + '2' * 12,
            'actor': 'superdan',
            'instance_id': self.uid,
            'deadline': self.deadline,
            'account_absent': True,
            'no_continuing_charge': True,
            'observed_at': self.now,
        }

    def test_manual_audit_stops_only_exact_uid_preserves_original_and_not_removal_proof(
        self,
    ):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        path = self.root / 'receipts' / (self.uid + '.json')
        previous = guardian_fixtures._read(path)
        self.client.get.reset_mock()
        self.client.delete.reset_mock()
        self.guardian_with_review(lambda uid: self.review()).tick()
        accepted = guardian_fixtures._read(path)
        self.assertEqual(accepted['state'], 'manually_reviewed')
        for field in (
            'request',
            'request_hash',
            'deadline',
            'workload_identity',
            'observed_at',
            'delete_started_at',
            'delete_attempts',
        ):
            self.assertEqual(accepted[field], previous[field])
        self.client.get.assert_not_called()
        self.client.delete.assert_not_called()
        self.now += 600
        self.guardian().tick()  # Persisted root-owned audit survives removal of the reader/restart.
        self.client.get.assert_not_called()
        self.client.delete.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'removal_unconfirmed'):
            self.guard.removal_proof(self.uid)
        with self.assertRaisesRegex(ValueError, 'proof_unavailable'):
            self.guard.proof(self.uid)

    def test_invalid_audit_or_db_loss_never_suspends_deadline_cleanup(self):
        for reader in (
            lambda uid: {**self.review(), 'instance_id': 'wrong-uid'},
            Mock(side_effect=RuntimeError('db-offline')),
        ):
            with self.subTest(reader=reader):
                self.arm()
                self.now = self.deadline
                self.client.get.reset_mock()
                self.client.delete.reset_mock()
                self.guardian_with_review(reader).tick()
                self.client.get.assert_called_once()
                self.client.delete.assert_called_once()
                self.assertNotEqual(
                    guardian_fixtures._read(
                        self.root / 'receipts' / (self.uid + '.json')
                    )['state'],
                    'manually_reviewed',
                )
                self.now += 60
                self.deadline = self.now + 120
                (self.root / 'requests' / (self.uid + '.json')).unlink()
                (self.root / 'receipts' / (self.uid + '.json')).unlink()

    def test_root_exception_requires_original_request_and_workload_hash(self):
        self.arm()
        self.now = self.deadline
        self.guardian().tick()
        path = self.root / 'receipts' / (self.uid + '.json')
        previous = guardian_fixtures._read(path)
        record = {
            **self.review(),
            'source': 'protected_operator_attestation',
            'accepted_delete': True,
            'intent_id': None,
            'request_hash': previous['request_hash'],
            'workload_identity_hash': _hash(previous['workload_identity']),
        }
        self.now += 60
        self.client.get.reset_mock()
        self.client.delete.reset_mock()
        self.guardian_with_review(
            lambda uid: {**record, 'workload_identity_hash': '0' * 64}
        ).tick()
        self.client.get.assert_called_once()
        self.assertNotEqual(guardian_fixtures._read(path)['state'], 'manually_reviewed')
        self.client.get.reset_mock()
        self.client.delete.reset_mock()
        self.guardian_with_review(lambda uid: record).tick()
        self.client.get.assert_not_called()
        self.client.delete.assert_not_called()
        self.assertEqual(
            guardian_fixtures._read(path)['manual_review']['source'],
            'protected_operator_attestation',
        )


# unittest normally collects inherited fixture cases. Keep this bounded new slice.
for _name in dir(guardian_fixtures.TargonCleanupTests):
    if _name.startswith('test_'):
        setattr(GuardianReviewTests, _name, None)
for _name in dir(operator_fixtures.OperatorTests):
    if _name.startswith('test_'):
        setattr(ManualReviewTests, _name, None)


class ReviewReaderTests(unittest.TestCase):
    def test_invalid_cli_database_options_reject_before_config_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'not-read.json'
            config.write_text('{}')
            for options in (
                ['--manual-review-database-name', 'app'],
                [
                    '--manual-review-database-container',
                    'a' * 64,
                    '--manual-review-database-name',
                    '',
                ],
            ):
                with (
                    self.subTest(options=options),
                    patch('studio_platform.targon_cleanup._read') as read,
                    patch(
                        'studio_platform.targon_runtime_aws.AwsTargonLoader'
                    ) as loader,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        review_guard.main(['--config', str(config), *options]), 1
                    )
                    read.assert_not_called()
                    loader.assert_not_called()

    def test_database_name_is_explicit_nonsecret_argument_and_cannot_inject_options(
        self,
    ):
        container = 'a' * 64
        calls = []

        def run(arguments, **options):
            calls.append(arguments)
            if arguments[1] == 'inspect':
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(container)
                    + ' '
                    + json.dumps(
                        {
                            'com.docker.compose.project': 'sixnine-platform',
                            'com.docker.compose.service': 'db',
                        }
                    ),
                )
            return SimpleNamespace(returncode=0, stdout='')

        reader = OperatorManualReviewReader(
            container, database_name='sixnine_platform', run=run
        )
        self.assertIsNone(reader('wrk-offline'))
        arguments = calls[-1]
        self.assertEqual(arguments[arguments.index('-d') + 1], 'sixnine_platform')
        self.assertEqual(arguments[arguments.index('-U') + 1], 'postgres')
        self.assertEqual(arguments[arguments.index('--user') + 1], 'postgres')
        for name in (
            '',
            '--host=other',
            ' db',
            'db;DROP TABLE test',
            "db'",
            'a' * 64,
            None,
            True,
            3,
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, 'database_name_invalid'),
            ):
                OperatorManualReviewReader(container, database_name=name, run=run)
        self.assertEqual(len(calls), 2)  # Validation precedes all subprocess calls.

    def test_fixed_read_only_query_bound_to_verified_db_container_and_uid(self):
        container = 'a' * 64
        calls = []

        def run(args, **options):
            calls.append((args, options))
            if args[1] == 'inspect':
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(container)
                    + ' '
                    + json.dumps(
                        {
                            'com.docker.compose.project': 'sixnine-platform',
                            'com.docker.compose.service': 'db',
                        }
                    ),
                )
            return SimpleNamespace(
                returncode=0, stdout='{"state":"manually_reviewed"}\n'
            )

        reader = OperatorManualReviewReader(container, run=run)
        self.assertEqual(reader('wrk-offline')['state'], 'manually_reviewed')
        self.assertIn('BEGIN READ ONLY;', calls[1][1]['input'])
        self.assertIn("i.provider_instance_id='wrk-offline'", calls[1][1]['input'])
        self.assertIn("s.kind='stop'", calls[1][1]['input'])
        self.assertIn('platform_attempts', calls[1][1]['input'])
        self.assertNotIn('sh', calls[1][0])
        arguments = calls[1][0]
        self.assertEqual(arguments[arguments.index('-d') + 1], 'postgres')
        with self.assertRaisesRegex(ValueError, 'identity_invalid'):
            reader("unsafe' OR true")
        with self.assertRaisesRegex(ValueError, 'container_invalid'):
            OperatorManualReviewReader('mutable-name')

    def test_protected_root_exception_checks_trust_before_read_and_exact_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                'source': 'protected_operator_attestation',
                'instance_id': 'wrk-exact',
            }
            (root / 'wrk-exact.json').write_text(json.dumps(record))
            fallback = Mock(return_value=None)
            reader = ProtectedManualReviewReader(root, fallback)
            with patch(
                'studio_platform.targon_cleanup._receipt_trust',
                side_effect=ValueError('untrusted'),
            ):
                with self.assertRaisesRegex(ValueError, 'untrusted'):
                    reader('wrk-exact')
            with patch('studio_platform.targon_cleanup._receipt_trust') as trust:
                self.assertEqual(reader('wrk-exact'), record)
                self.assertEqual(trust.call_args.args[0][-1], root / 'wrk-exact.json')
            fallback.assert_not_called()
            self.assertIsNone(reader('wrk-other'))
            fallback.assert_called_once_with('wrk-other')


if __name__ == '__main__':
    unittest.main()
