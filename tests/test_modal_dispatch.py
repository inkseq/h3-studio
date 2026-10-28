"""Real durable slots/worker claims with a fake private Modal HTTP endpoint."""

from dataclasses import asdict
import json
from pathlib import Path

import httpx

from studio_platform.dbos_transport import DurableSlots
from studio_platform.fleet import SlotConfig, slot_runner
from studio_platform.inference.native_outputs import native_delivery_spec
from studio_platform.inference.wangp import WanGPBackend
from studio_platform.inference.wangp_contract import (
    HostReadiness,
    OperationReceipt,
    PreparedRequest,
)
from studio_platform.inference.wangp_http import HTTPWanGPTransport
from studio_platform.inference.wangp_profile_dispatch import H3ProfileCompiler
from studio_platform.modal_capacity import registration_usage
from studio_platform.modal_dispatch import consumer_reason
from studio_platform.modal_runtime import PROFILE
from studio_platform.postgres_dispatch import (
    PostgresSlotRunner,
    record_consumer,
    withdraw_consumer,
)
from studio_platform.repository import BudgetExceeded
from studio_platform.runtime_catalog import profile_output_delivery
from studio_platform.settings import Settings
from studio_platform.storage import LocalObjectStore
import test_modal_admission as fixture
import test_modal_capacity as capacity_fixture
from test_platform_wangp_profiles import job_for


class FakeModalHTTP:
    def __init__(self, manifest, identity):
        self.manifest, self.identity = manifest, identity
        self.incarnation = 'a' * 32
        self.receipt, self.submissions, self.lose_response = None, [], False

    def handle(self, request):
        path = request.url.path
        if path == '/v1/readiness':
            return httpx.Response(
                200,
                json=asdict(
                    HostReadiness(
                        self.manifest.digest,
                        'modal-' + self.identity,
                        self.incarnation,
                        self.receipt is None,
                    )
                ),
            )
        if path == '/v1/operations' and request.method == 'POST':
            value = json.loads(request.content)
            value['inputs'] = tuple(value['inputs'])
            prepared = PreparedRequest(**value)
            self.submissions.append(prepared.attempt_tag)
            self.receipt = OperationReceipt(
                prepared.operation_id,
                prepared.job_id,
                prepared.attempt_tag,
                prepared.request_hash,
                prepared.manifest_digest,
                prepared.identity_digest,
                'modal-' + self.identity,
                self.incarnation,
                'running',
                prepared.generate_audio,
            )
            if self.lose_response:
                raise httpx.ReadError('synthetic lost original response')
        if self.receipt and path in {
            '/v1/operations',
            '/v1/operations/' + self.receipt.operation_id,
        }:
            return httpx.Response(200, json=self.receipt.to_dict())
        return httpx.Response(404)


class ModalDispatchTests(fixture.ModalAdmissionFixture):
    registered_spec = capacity_fixture.ModalCapacityTests.registered_spec

    def tearDown(self):
        if getattr(self, 'slots', None) is not None:
            self.slots.close()
        super().tearDown()

    def accepted_native(self, key='one'):
        template, _ = job_for(
            PROFILE, {'mode': 'fl', 'height': 480, 'steps': 5, 'input_roles': []}
        )
        compiled = template['request']
        compiled['request']['generate_audio'] = False
        delivery = profile_output_delivery(PROFILE)
        execution = dict(
            pool=self.pool,
            backend='wangp-worker',
            enabled=True,
            dispatch_backend='hatchet-v1',
            output_delivery=delivery,
            delivery_spec=native_delivery_spec(compiled, delivery),
            deployment_profile_id=PROFILE,
            expected_runtime_s=120,
            configuration_id='test-v7',
            engine_manifest_digest=self.manifest.digest,
        )
        plan = self.repo.create_plan(
            self.scope,
            compiled,
            execution,
            expires_at=self.now + 1000,
            estimated_cost_microusd=0,
        )
        job = self.repo.create_job(self.scope, plan['id'], key)
        job = self.repo.get_job(self.scope, job['id'])
        self.work.append((job, self.profile))
        return job

    def native_slots(self):
        self.identity = self.admission.tick()['intent_id']
        self.spec = self.registered_spec(self.identity)
        self.remote = FakeModalHTTP(self.manifest, self.identity)
        transport = HTTPWanGPTransport(
            'http://127.0.0.1:8199',
            'synthetic-' + 'x' * 48,
            transport=httpx.MockTransport(self.remote.handle),
            expected_incarnation='a' * 32,
        )
        self.addCleanup(transport.close)
        self.backend = WanGPBackend(
            enabled=True,
            slot_key='modal-' + self.identity,
            manifest=self.manifest,
            transport=transport,
            compiler=H3ProfileCompiler(self.manifest, transport.stage_input),
            expected_incarnation='a' * 32,
        )
        settings = Settings(
            Path(self.temp.name),
            generation_enabled=True,
            execution_backend='wangp-worker',
            public_origin='https://staging.inkseq.com',
        )

        def context(config, worker_id, selected_settings, *, repository):
            def runner(*args, **kwargs):
                # Synthetic policy issuer only; real slot/consumer/window/claim/
                # submission fences below remain enabled throughout this test.
                kwargs['submission_guard'] = lambda _: True
                return PostgresSlotRunner(
                    *args,
                    **kwargs,
                    collection_lock_dir=Path(self.temp.name) / 'collect',
                )

            return slot_runner(
                config,
                worker_id,
                selected_settings,
                repository=repository,
                backend_factory=lambda *_: self.backend,
                store_factory=lambda _: LocalObjectStore(
                    Path(self.temp.name) / 'objects'
                ),
                runner_factory=runner,
                capacity_usage=lambda c, spec: registration_usage(
                    self.service, self.hosts, c, spec
                ),
            )

        slots = DurableSlots(
            self.repo,
            settings,
            Path(self.temp.name) / 'durable',
            runner_context=context,
        )
        self.slots = slots
        slots.__enter__()
        self.slot = SlotConfig(
            self.spec,
            enabled=True,
            confirmed_idle=True,
            endpoint='http://127.0.0.1:8199',
            allowed_origins=('http://127.0.0.1:8199',),
            runtime_config_file=str(Path(self.temp.name) / 'synthetic-runtime.json'),
        )
        slots.ensure(self.identity, self.slot)
        self.runner = slots._slots[self.identity].runner
        self.assertIsInstance(self.runner, PostgresSlotRunner)
        return slots

    def turn(self, slots, job):
        return slots.turn(
            self.scope.tenant_id, self.scope.owner_id, self.scope.project_id, job['id']
        )

    def test_real_ensure_turn_claims_and_submits_without_a_dstack_node(self):
        job = self.accepted_native()
        slots = self.native_slots()
        frozen = self.repo.get_job(self.scope, job['id'])
        self.assertEqual(self.turn(slots, job)['state'], 'running')
        current = self.repo.get_job(self.scope, job['id'])
        self.assertEqual(current['request'], frozen['request'])
        self.assertEqual(current['execution_plan'], frozen['execution_plan'])
        self.assertEqual(current['attempt_no'], 1)
        self.assertEqual(self.remote.submissions, [current['current_attempt_id']])
        self.assertEqual(self.turn(slots, job)['state'], 'running')
        self.assertEqual(len(self.remote.submissions), 1)

    def test_unknown_http_submit_recovers_original_attempt_without_replay(self):
        job = self.accepted_native()
        slots = self.native_slots()
        self.remote.lose_response = True
        self.assertEqual(self.turn(slots, job)['state'], 'submission_unknown')
        original = self.repo.get_job(self.scope, job['id'])
        self.now += 31
        self.assertEqual(self.turn(slots, job)['state'], 'running')
        current = self.repo.get_job(self.scope, job['id'])
        self.assertEqual(current['current_attempt_id'], original['current_attempt_id'])
        self.assertEqual(current['attempt_no'], 1)
        self.assertEqual(len(self.remote.submissions), 1)

    def test_replacement_runtime_cannot_refresh_or_claim(self):
        job = self.accepted_native()
        slots = self.native_slots()
        self.backend.expected_incarnation = 'b' * 32
        with self.assertRaises(BudgetExceeded):
            self.turn(slots, job)
        self.backend.expected_incarnation = 'a' * 32
        self.assertFalse(self.remote.submissions)
        self.assertEqual(self.repo.get_job(self.scope, job['id'])['attempt_no'], 0)

    def test_stale_consumer_or_new_fence_blocks_until_original_process_refreshes(self):
        job = self.accepted_native()
        slots = self.native_slots()
        control = self.runner.control
        with self.repo.engine.connect() as c:
            self.assertIsNone(
                consumer_reason(c, control.get(self.spec.worker_id), self.now)
            )
        control.mark_ready(self.spec.worker_id, upstream_idle_confirmed=True)
        self.assertIsNone(
            control.claim(self.spec.worker_id, self.pool, purpose='generate')
        )
        slots.ensure(self.identity, self.slot)
        self.now += 61
        self.assertIsNone(
            control.claim(self.spec.worker_id, self.pool, purpose='generate')
        )
        self.assertFalse(self.remote.submissions)
        slots.ensure(self.identity, self.slot)
        self.assertEqual(self.turn(slots, job)['state'], 'running')

    def test_stale_withdrawal_cannot_hide_a_new_consumer_and_stop_withdraws_latest(
        self,
    ):
        self.accepted_native()
        slots = self.native_slots()
        control = self.runner.control
        old = self.runner._consumer_observation
        fresh = record_consumer(self.repo, control, self.backend, self.spec.worker_id)
        withdraw_consumer(self.repo, control, self.backend, self.spec.worker_id, old)
        with self.repo.engine.connect() as c:
            self.assertIsNone(
                consumer_reason(c, control.get(self.spec.worker_id), self.now)
            )
        self.runner._consumer_observation = fresh
        slots.stop(self.identity)
        with self.repo.engine.connect() as c:
            self.assertEqual(
                consumer_reason(c, control.get(self.spec.worker_id), self.now),
                'modal_queue_consumer_unconfirmed',
            )
