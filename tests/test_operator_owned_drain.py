"""Joined owned-child retirement and original stop, with fake provider/runtime.

SQLite by default; LedgerCase also permits an isolated local test PostgreSQL.
No process, cloud resource, model, network or production database is started.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS
from unittest.mock import patch

from sqlalchemy import insert, select, update

from studio_platform.artifact_writer import write_receipts
from studio_platform.auth import Principal
from studio_platform.control import WorkerControl
from studio_platform.fleet import FleetConfig
from studio_platform.inference.wangp import WanGPBackend
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.lium_provider import InferenceIdleProof
from studio_platform.operator_boot import OperatorBoot, source_hashes, SOURCE_NAMES
from studio_platform.operator_capacity import (DeploymentBinding, OperatorCapacity, OperatorRegistry,
    operator_commands, operator_nodes)
from studio_platform.operator_controller import OperatorController
from studio_platform.repository import (attempts, budget_reservations, instance_intents, jobs,
    outbox, registered_devices, registered_workers)
from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest, get_profile
from studio_platform.scaler import LaunchSpec, ProviderFact
from studio_platform.targon_provider import TargonIdleProof
from studio_platform.wangp_bootstrap import make_slot
import test_platform_repository as ledger_tests
import test_platform_scaler as scaler_tests


class OwnedChild:
    def __init__(self):
        self.pid, self.code, self.signals = 12345, None, []

    def poll(self):
        return self.code

    def send_signal(self, signal):
        self.signals.append(signal)  # Sending SIGTERM is not exit proof.


class PreparedBoot:
    """The real FleetSupervisor owns fake handles; no saved PID is consulted."""
    def __init__(self, repo, provider, config, *, fleet_factory, **kwargs):
        self.repo, self.provider, self.config = repo, provider, config
        self.fleet_factory = fleet_factory
        self.fleet = self.backend = None
        self.bound_intent = self.bound_instance = None
        self.pending = False
        self.closed = False

    def enable_pollable_upload(self):
        pass

    def preparation_pending(self):
        return self.pending

    def cancel_preparation(self):
        pass  # Cancellation request alone does not end an upload.

    def tick(self, intent_id):
        if self.fleet is None:
            intent = next(row for row in self.repo.list_instance_intents() if row['id'] == intent_id)
            self.bound_intent, self.bound_instance = intent_id, intent['provider_instance_id']
            directory = self.config.work_dir/intent_id
            directory.mkdir(parents=True, exist_ok=True)
            slot = make_slot(self.config, intent,
                {'gpus': [{'uuid': 'GPU-'+str(self.config.profile_slot_index)}]}, directory)
            self.fleet = self.fleet_factory(FleetConfig(directory/'fleet', (slot,), True, 1),
                self.repo, directory/'fleet.json')
            self.fleet.start()
            incarnation = str(self.config.profile_slot_index+1).zfill(32)
            self.info = HostReadiness(self.config.engine_manifest_digest, intent_id, incarnation, True)
            self.backend = WanGPBackend(enabled=True, slot_key=intent_id,
                manifest=engine_manifest(self.config.deployment_profile_id, 'fl'),
                transport=NS(readiness=lambda: self.info), compiler=lambda *_: None,
                expected_incarnation=incarnation)
            WorkerControl(self.repo).mark_ready(slot.spec.worker_id, upstream_idle_confirmed=True)
        self.fleet.tick()
        return {'state': 'fleet_running'}

    def idle_probe(self, intent_id, instance_id):
        if intent_id != self.bound_intent or instance_id != self.bound_instance:
            return None
        kind = InferenceIdleProof if self.config.provider == 'lium' else TargonIdleProof
        now = self.repo.clock()
        return kind(instance_id, now, now, self.backend.is_idle())

    def close(self):
        self.closed = True


class OwnedDrainProvider(scaler_tests.FakeProvider):
    def __init__(self, case, provider):
        super().__init__()
        self.case, self.provider_id = case, provider

    def create(self, tag, launch, *, hard_deadline):
        fact = replace(super().create(tag, launch, hard_deadline=hard_deadline),
            instance_id=tag if self.provider_id == 'lium' else 'workload_'+tag)
        self.facts[tag] = fact
        return fact

    def lifetime(self, tag, instance_id, *, local_created_at, maximum_hours):
        return {'instance_id': instance_id, 'safe_deadline': local_created_at+maximum_hours*3600}

    def reconcile(self, tag, instance_id):
        fact = super().reconcile(tag, instance_id)
        boot = self.case.controller.boots.get(tag)
        if fact.state == 'running' and boot is not None:
            proof = boot.idle_probe(tag, instance_id)
            return replace(fact, idle_confirmed=proof is not None and proof.idle,
                idle_since=proof.idle_since if proof is not None else None)
        return fact


class OwnedDrainTests(ledger_tests.LedgerCase):
    def setUp(self):
        super().setUp()
        self.actor = Principal('superdan', 'browser', auth_mode='password')
        self.control = WorkerControl(self.repo)

    def _start(self, *, slots=1, provider='lium'):
        profile = get_profile(PROFILE_IDS[2] if provider == 'lium' else PROFILE_IDS[3])
        manifest = engine_manifest(profile['id'], 'fl')
        sources = []
        for index in range(slots):
            directory = Path(self.temp.name)/('source-'+str(index))
            directory.mkdir()
            for name in SOURCE_NAMES:
                (directory/name).write_bytes((name+str(index)).encode())
            sources.append(str(directory))
        self.binding = DeploymentBinding(binding_id='owned-drain-test', runtime_profile_id=profile['id'],
            gpu_type='NVIDIA RTX PRO 6000 Blackwell', gpu_count=slots, execution_slots=slots,
            pool='owned-drain-test', configuration_id='owned-native-test', model_id=profile['model_id'],
            recipe_ids=('h3-base-fl2va-v1',), engine_manifest_digest=manifest.digest,
            launch=LaunchSpec(provider, 'owned-native-test', profile['model_id']), scope=self.scope,
            budget_account_ids=('owner-budget',), hourly_cost_microusd=360_000,
            reservation_per_node_microusd=1_000_000, expires_at=10_000, enabled=True,
            boot={'source_dirs': sources, 'source_sha256': [source_hashes(path) for path in sources]})
        registry = OperatorRegistry([self.binding], qualified_providers=(provider,))
        self.service = OperatorCapacity(self.repo, NS(operator_capacity_owners=('superdan',)), registry)
        self.repo.configure_pool(self.binding.pool, max_instances=4, max_physical_gpus=4)
        self.service.update_policy(self.actor, {'expected_version': 0, 'enabled': True,
            'max_instances': 4, 'max_physical_gpus': 4, 'max_hourly_cost_microusd': 2_000_000,
            'idle_shutdown_seconds': 600, 'max_ttl_seconds': 3600})
        root = Path(self.temp.name)
        self.runtime = {'work_dir': str(root/'work'), 'port_start': 31000,
            'ssh_key_file': str(root/'unused-test-key'), 'known_hosts_file': str(root/'unused-hosts'),
            'config_path': str(root/'unused-runtime.json')}
        self.provider = OwnedDrainProvider(self, provider)
        def boot_factory(binding, intent, selection):
            return OperatorBoot(self.repo, self.provider, binding, intent, selection, self.runtime,
                boot_class=PreparedBoot, popen=lambda *args, **kwargs: OwnedChild())
        self.controller = OperatorController(self.service, provider_factory=lambda binding: self.provider,
            boot_factory=boot_factory, enabled=True, leader_id='owned-drain-test-leader')
        chosen = {'runtime_profile_id': profile['id'], 'gpu_type': self.binding.gpu_type, 'mode': 'fl',
            'node_count': 1, 'gpu_count': slots, 'ttl_seconds': 3600, 'provider': provider}
        preview = self.service.preview(self.actor, chosen)
        self.assertTrue(preview['can_start'], preview['blockers'])
        self.service.start(self.actor, {'preview_id': preview['preview_id']}, 'original-start')
        self.controller.tick()
        self.intent = self.repo.list_instance_intents()[0]
        self.boot = self.controller.boots[self.intent['id']]
        self.worker_ids = [next(iter(slot.fleet.children)) for slot in self.boot.slots]
        self.assertTrue(all(self.control.get(worker)['state'] == 'ready' for worker in self.worker_ids))

    def _job(self, key='original-job'):
        request = {'recipe_id': self.binding.recipe_ids[0], 'deployment_profile_id': self.binding.runtime_profile_id, 'request': {'model': self.binding.model_id,
            'prompt': 'synthetic offline request'}}
        execution = {'pool': self.binding.pool, 'expected_runtime_s': 120, 'backend': 'wangp-worker',
            'enabled': True, 'configuration_id': self.binding.configuration_id,
            'engine_manifest_digest': self.binding.engine_manifest_digest, 'output_delivery': 'native-frames-v1'}
        plan = self.repo.create_plan(self.scope, request, execution, expires_at=self.now+1000,
            estimated_cost_microusd=100_000)
        return self.repo.create_job(self.scope, plan['id'], key, budget_account_ids=('owner-budget',))

    def _fail(self, worker_id=None):
        worker_id = worker_id or self.worker_ids[0]
        job = self._job()
        claim = self.control.claim(worker_id, self.binding.pool)
        self.assertIsNotNone(claim)
        self.control.queue.begin_submission(claim.lease)
        self.control.queue.record_submitted(claim.lease, 'original-runtime-operation')
        self.control.queue.fail(claim.lease, 'upstream_generation_failed', actual_cost_microusd=None,
            upstream_stopped=True)
        self.control.observe(worker_id, job['id'], quarantine_failures=True)
        return self.repo.get_job(self.scope, job['id'])

    def _exit(self):
        for slot in self.boot.slots:
            for child in slot.fleet.children.values():
                child.code = 0

    def _request_stop(self):
        node = self.service.state(self.actor)['nodes'][0]
        result = self.service.node_command(self.actor, node['id'], {'expected_version': node['version']},
            'original-stop', 'stop')
        self.stop_id = result['operation']['id']

    def _drain_locally(self):
        self._request_stop()
        self.now += 180
        with self.repo.transaction() as connection:
            self.repo.update_instance(self.intent['id'], 'draining', connection=connection)
        self.boot.request_drain()
        for slot in self.boot.slots:
            slot.fleet.tick()

    def _events(self):
        with self.repo.engine.connect() as connection:
            return list(connection.execute(select(outbox).where(
                outbox.c.event_type == 'worker.owned_drain_retired')).mappings())

    def _assert_retained(self):
        self.assertTrue(all(self.control.get(worker)['state'] != 'retired' for worker in self.worker_ids))
        with self.repo.engine.connect() as connection:
            self.assertTrue(all(row.state == 'owned' for row in connection.execute(select(registered_devices))))
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self._events(), [])

    def test_expired_owned_failure_retires_once_then_original_stop_removes_same_node(self):
        self._start()
        original_job = self._fail()
        self._exit()
        self._request_stop()
        budget = self.repo.get_budget('owner-budget')
        with self.repo.engine.connect() as connection:
            original_attempts = list(connection.execute(select(attempts)).mappings())
            reservations = list(connection.execute(select(budget_reservations)).mappings())
        self.now += 180
        self.controller.tick()  # Old scaler block, then owned retirement.
        worker = self.control.get(self.worker_ids[0])
        self.assertEqual(worker['state'], 'retired')
        self.assertEqual(worker['drain_requested'], 1)
        event = self._events()[0]
        self.assertEqual(event['payload']['next_fence'], event['payload']['previous_fence']+1)
        self.assertEqual(self.provider.destroys, [])
        self.now += 16
        self.controller.tick()  # Fresh provider proof continues original stop.
        for _ in range(2):
            self.now += 16
            self.controller.tick()
        self.assertEqual(self.provider.destroys, [(self.intent['id'], self.intent['provider_instance_id'])])
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_job(self.scope, original_job['id']), original_job)
        self.assertEqual(self.repo.get_budget('owner-budget'), budget)
        self.assertEqual(self.control.get(self.worker_ids[0])['fence'], worker['fence'])
        self.assertEqual(len(self._events()), 1)
        with self.repo.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(attempts)).mappings()), original_attempts)
            self.assertEqual(list(connection.execute(select(budget_reservations)).mappings()), reservations)
            command = connection.execute(select(operator_commands).where(operator_commands.c.id == self.stop_id)).mappings().one()
            self.assertEqual(command['state'], 'completed')
        final = self.repo.list_instance_intents()[0]
        self.assertEqual(final['state'], 'destroyed')
        self.assertEqual(final['hard_deadline'], self.intent['hard_deadline'])

    def test_original_targon_binding_uses_same_owned_retirement(self):
        self._start(provider='targon')
        self._fail()
        self._exit()
        self._drain_locally()
        self.assertTrue(self.boot._retire_stopped_workers())
        self.assertTrue(self.boot._retire_stopped_workers())
        self.assertEqual(len(self._events()), 1)
        self.assertEqual(self.control.get(self.worker_ids[0])['state'], 'retired')

    def test_missing_owned_child_or_pending_preparation_cannot_release(self):
        self._start()
        self._fail()
        self._exit()
        self._drain_locally()
        slot = self.boot.slots[0]
        child = slot.fleet.children[self.worker_ids[0]]
        mutations = (
            (lambda: setattr(child, 'code', None), lambda: setattr(child, 'code', 0)),
            (lambda: setattr(slot, 'pending', True), lambda: setattr(slot, 'pending', False)),
            (lambda: slot.fleet.children.clear(), lambda: slot.fleet.children.update({self.worker_ids[0]: child})),
            (lambda: setattr(slot, 'bound_instance', 'changed'), lambda: setattr(slot, 'bound_instance', self.intent['provider_instance_id'])),
        )
        for mutate, restore in mutations:
            with self.subTest(mutation=mutate):
                mutate()
                self.assertFalse(self.boot._retire_stopped_workers())
                self._assert_retained()
                restore()
        fleet = slot.fleet
        slot.fleet = None
        self.assertFalse(self.boot._retire_stopped_workers())
        slot.fleet = fleet
        self._assert_retained()

    def test_busy_missing_or_replaced_runtime_identity_cannot_release(self):
        self._start()
        self._fail()
        self._exit()
        self._drain_locally()
        slot = self.boot.slots[0]
        original = slot.info
        for changed in (replace(original, idle=False), replace(original, incarnation='a'*32),
                replace(original, manifest_digest='b'*64), replace(original, slot_key='other-intent')):
            with self.subTest(readiness=changed):
                slot.info = changed
                self.assertFalse(self.boot._retire_stopped_workers())
                self._assert_retained()
        slot.info = original
        transport = slot.backend.transport
        transport.readiness = lambda: (_ for _ in ()).throw(TimeoutError('synthetic offline timeout'))
        self.assertFalse(self.boot._retire_stopped_workers())
        self._assert_retained()
        transport.readiness = lambda: slot.info
        slot.backend.expected_incarnation = None
        self.assertFalse(self.boot._retire_stopped_workers())
        self._assert_retained()

    def test_elapsed_idle_probe_is_not_fresh_proof(self):
        self._start()
        self._fail()
        self._exit()
        self._drain_locally()
        slot = self.boot.slots[0]
        def delayed_probe():
            self.now += 31
            return slot.info
        slot.backend.transport.readiness = delayed_probe
        self.assertFalse(self.boot._retire_stopped_workers())
        self._assert_retained()

    def test_unknown_collecting_leased_or_unstopped_history_cannot_release(self):
        self._start()
        job = self._fail()
        self._exit()
        self._drain_locally()
        changes = (
            (jobs, jobs.c.id == job['id'], {'status': 'submission_unknown'}, {'status': 'failed'}),
            (jobs, jobs.c.id == job['id'], {'status': 'collecting'}, {'status': 'failed'}),
            (jobs, jobs.c.id == job['id'], {'lease_worker_id': self.worker_ids[0]}, {'lease_worker_id': None}),
            (attempts, attempts.c.job_id == job['id'], {'upstream_stopped': 0}, {'upstream_stopped': 1}),
            (attempts, attempts.c.job_id == job['id'], {'status': 'running'}, {'status': 'failed'}),
            (registered_workers, registered_workers.c.id == self.worker_ids[0],
                {'current_job_id': job['id']}, {'current_job_id': None}),
        )
        for table, predicate, unsafe, original in changes:
            with self.subTest(unsafe=unsafe):
                with self.repo.transaction() as connection:
                    connection.execute(update(table).where(predicate).values(**unsafe))
                self.assertFalse(self.boot._retire_stopped_workers())
                self._assert_retained()
                with self.repo.transaction() as connection:
                    connection.execute(update(table).where(predicate).values(**original))

    def test_binding_device_change_or_unsettled_collection_receipt_cannot_release(self):
        self._start()
        job = self._fail()
        self._exit()
        self._drain_locally()
        original_hash = self.control.get(self.worker_ids[0])['spec_hash']
        changes = (
            (operator_nodes, {'binding_hash': 'b'*64}, {'binding_hash': self.binding.fingerprint}),
            (registered_workers, {'spec_hash': 'b'*64}, {'spec_hash': original_hash}),
            (registered_devices, {'gpu_id': 'different-GPU'}, {'gpu_id': 'GPU-0'}),
        )
        for table, unsafe, original in changes:
            with self.subTest(unsafe=unsafe):
                with self.repo.transaction() as connection:
                    connection.execute(update(table).values(**unsafe))
                self.assertFalse(self.boot._retire_stopped_workers())
                self._assert_retained()
                with self.repo.transaction() as connection:
                    connection.execute(update(table).values(**original))
        with self.repo.transaction() as connection:
            connection.execute(insert(write_receipts).values(id='a'*64, tenant=self.scope.tenant_id,
                owner=self.scope.owner_id, job_id=job['id'], attempt_id=job['current_attempt_id'],
                version=0, record=json.dumps({'phase': 'reserved'})))
        self.assertFalse(self.boot._retire_stopped_workers())
        self._assert_retained()

    def test_multi_slot_busy_or_live_peer_prevents_partial_retirement(self):
        self._start(slots=2)
        self._fail()
        self._exit()
        self._drain_locally()
        peer = self.boot.slots[1]
        child = peer.fleet.children[self.worker_ids[1]]
        child.code = None
        self.assertFalse(self.boot._retire_stopped_workers())
        self._assert_retained()
        child.code = 0
        peer.info = replace(peer.info, idle=False)
        self.assertFalse(self.boot._retire_stopped_workers())
        self._assert_retained()
        peer.info = replace(peer.info, idle=True)
        self.assertTrue(self.boot._retire_stopped_workers())
        self.assertTrue(self.boot._retire_stopped_workers())
        self.assertEqual(len(self._events()), 2)
        self.assertTrue(all(self.control.get(worker)['state'] == 'retired' for worker in self.worker_ids))

    def test_same_transaction_prevents_new_claim_during_idle_probe(self):
        self._start()
        queued = self._job()
        self._exit()
        self._drain_locally()
        entered, release = threading.Event(), threading.Event()
        slot = self.boot.slots[0]
        def locked_probe():
            entered.set()
            if not release.wait(5):
                raise AssertionError('test_probe_release_timeout')
            return slot.info
        slot.backend.transport.readiness = locked_probe
        with ThreadPoolExecutor(max_workers=2) as executor:
            retirement = executor.submit(self.boot._retire_stopped_workers)
            self.assertTrue(entered.wait(5))
            claim = executor.submit(self.control.claim, self.worker_ids[0], self.binding.pool)
            try:
                with self.assertRaises(FutureTimeout):
                    claim.result(timeout=.1)
            finally:
                release.set()
            self.assertTrue(retirement.result(timeout=5))
            self.assertIsNone(claim.result(timeout=5))
        unchanged = self.repo.get_job(self.scope, queued['id'])
        self.assertEqual(unchanged['status'], 'queued')
        self.assertIsNone(unchanged['current_attempt_id'])
        self.assertEqual(self.control.get(self.worker_ids[0])['state'], 'retired')

    def test_partial_transition_error_rolls_back_all_slots_and_events(self):
        self._start(slots=2)
        self._exit()
        self._drain_locally()
        original = WorkerControl._retire_locked
        calls = []
        def fail_second(control, connection, worker):
            calls.append(worker['id'])
            if len(calls) == 2:
                raise RuntimeError('synthetic database interruption')
            return original(control, connection, worker)
        with patch.object(WorkerControl, '_retire_locked', fail_second):
            with self.assertRaisesRegex(RuntimeError, 'synthetic database interruption'):
                self.boot._retire_stopped_workers()
        self.assertEqual(len(calls), 2)
        self._assert_retained()

    def test_running_desired_state_cannot_be_retired_by_stopping_flag_alone(self):
        self._start()
        self._exit()
        self.boot.request_drain()
        self.assertFalse(self.boot._retire_stopped_workers())
        self._assert_retained()
