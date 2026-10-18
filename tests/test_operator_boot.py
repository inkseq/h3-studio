"""Fake boot/worker boundaries; local temporary sources only, no cloud calls."""

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from studio_platform import operator_boot as subject
from studio_platform.lium_bootstrap import BootError
from studio_platform.lium_provider import InferenceIdleProof
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest, get_profile

INTENT = '11111111-1111-4111-8111-111111111111'
INSTANCE = '22222222-2222-4222-8222-222222222222'
SECOND = '33333333-3333-4333-8333-333333333333'


class FakeConnection:
    def __init__(self, repo):
        self.repo = repo

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement):
        name = statement.get_final_froms()[0].name
        # Assert actual table names rather than silently accepting another query.
        if name == subject.operator_nodes.name:
            row = self.repo.node
        elif name == subject.instance_intents.name:
            row = self.repo.intent
        elif name == subject.registered_workers.name:
            row = self.repo.worker
        else:
            raise AssertionError('Unexpected database table')
        return NS(
            mappings=lambda: NS(
                one=lambda: copy.deepcopy(row), first=lambda: copy.deepcopy(row)
            ),
            first=lambda: copy.deepcopy(row),
        )


class FakeRepo:
    def __init__(self, binding):
        self.intent = {
            'id': INTENT,
            'provider': 'lium',
            'provider_instance_id': INSTANCE,
            'state': 'starting',
            'hard_deadline': 5000,
        }
        self.node = {
            'desired_state': 'running',
            'runtime_state': 'ready',
            'binding_hash': binding.fingerprint,
            'binding_id': 'binding',
            'payload': {
                'lifetime': {
                    'state': 'verified',
                    'instance_id': INSTANCE,
                    'observed_at': 1000,
                    'safe_deadline': 5000,
                }
            },
        }
        self.worker = None
        self.engine = NS(connect=lambda: FakeConnection(self))
        self.clock = lambda: 1000
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeFleet:
    def __init__(self):
        self.drains = 0
        self.ticks = 0
        self.children = {}

    def drain(self):
        self.drains += 1

    def tick(self):
        self.ticks += 1
        return {'children': []}


class FakeBoot:
    def __init__(self, repo, provider, config, **kwargs):
        self.config = config
        self.fleet = FakeFleet()
        self.ticks = []
        self.cancelled = 0
        self.closed = 0
        self.upload_enabled = False
        self.state = 'fleet_running'
        self.proof = None

    def enable_pollable_upload(self):
        self.upload_enabled = True

    def tick(self, identity):
        self.ticks.append(identity)
        return {'state': self.state}

    def cancel_preparation(self):
        self.cancelled += 1

    def idle_probe(self, *args):
        return self.proof

    def close(self):
        self.closed += 1


class OperatorBootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        profile = get_profile(PROFILE_IDS[2])
        sources = []
        for index in range(2):
            directory = self.root / f'source-{index}'
            directory.mkdir()
            for name in subject.SOURCE_NAMES:
                (directory / name).write_bytes((name + str(index)).encode())
            sources.append(str(directory))
        self.binding = NS(
            enabled=True,
            runtime_profile_id=profile['id'],
            execution_slots=2,
            gpu_count=2,
            launch=NS(provider='lium'),
            model_id=profile['model_id'],
            configuration_id='native-test',
            recipe_ids=('h3-base-fl2va-v1',),
            engine_manifest_digest=engine_manifest(profile['id'], 'fl').digest,
            fingerprint='f' * 64,
            pool='native-test',
            expires_at=6000,
            boot={
                'source_dirs': sources,
                'source_sha256': [subject.source_hashes(d) for d in sources],
            },
        )
        self.repo = FakeRepo(self.binding)
        self.config = {
            'work_dir': str(self.root / 'work'),
            'port_start': 31000,
            'ssh_key_file': str(self.root / 'not-a-real-key'),
            'known_hosts_file': str(self.root / 'known-hosts'),
            'config_path': str(self.root / 'protected-runtime.json'),
        }
        self.spawn = []
        self.boot = subject.OperatorBoot(
            self.repo,
            None,
            self.binding,
            self.repo.intent,
            {},
            self.config,
            boot_class=FakeBoot,
            popen=lambda args, **kwargs: (
                self.spawn.append((args, kwargs)) or NS(pid=10)
            ),
        )

    def test_two_gpu_slots_have_distinct_stable_sources_state_and_ports(self):
        a, b = (slot.config for slot in self.boot.slots)
        self.assertNotEqual(a.work_dir, b.work_dir)
        self.assertNotEqual(a.source_dir, b.source_dir)
        self.assertEqual((a.local_port, b.local_port), (31000, 31001))
        self.assertEqual((a.profile_slot_index, b.profile_slot_index), (0, 1))
        self.assertEqual(a.expected_host_gpus, 2)
        self.assertEqual(
            subject.ports_for(self.config['work_dir'], INTENT, 2, 31000), (31000, 31001)
        )
        self.assertEqual(
            subject.ports_for(self.config['work_dir'], SECOND, 1, 31000), (31002,)
        )
        with self.assertRaisesRegex(BootError, 'identity_changed'):
            subject.ports_for(self.config['work_dir'], INTENT, 1, 31000)
        self.assertTrue(all(s.upload_enabled for s in self.boot.slots))
        self.assertFalse(a.smoke_enabled)
        self.assertEqual(a.qualification_profile, QUEUED_TASK_PROFILE)

    def test_changed_source_hash_is_rejected_before_start(self):
        (Path(self.binding.boot['source_dirs'][1]) / 'wangp-manifest.json').write_bytes(
            b'changed'
        )
        with self.assertRaisesRegex(BootError, 'source_changed'):
            subject.OperatorBoot(
                self.repo,
                None,
                self.binding,
                self.repo.intent,
                {},
                self.config,
                boot_class=FakeBoot,
            )
        self.assertEqual(self.spawn, [])

    def test_provider_mismatch_is_rejected_before_slot_construction(self):
        with self.assertRaisesRegex(BootError, 'provider_mismatch'):
            subject.OperatorBoot(
                self.repo,
                None,
                self.binding,
                {**self.repo.intent, 'provider': 'targon'},
                {},
                self.config,
                boot_class=FakeBoot,
            )

    def test_targon_profile_keeps_provider_uid_slot_hardware_and_idle_proof(self):
        from studio_platform.targon_provider import TargonIdleProof

        profile = get_profile(PROFILE_IDS[3])
        binding = copy.deepcopy(self.binding)
        binding.runtime_profile_id = profile['id']
        binding.model_id = profile['model_id']
        binding.engine_manifest_digest = engine_manifest(profile['id'], 'fl').digest
        binding.launch.provider = 'targon'
        binding.execution_slots = binding.gpu_count = 1
        binding.boot = {key: values[:1] for key, values in binding.boot.items()}
        intent = {
            **self.repo.intent,
            'provider': 'targon',
            'provider_instance_id': 'workload_test-123',
        }
        config = {**self.config, 'work_dir': str(self.root / 'targon-work')}
        candidate = subject.OperatorBoot(
            self.repo, None, binding, intent, {}, config, boot_class=FakeBoot
        )
        self.assertEqual(len(candidate.slots), 1)
        self.assertEqual(candidate.slots[0].config.provider, 'targon')
        self.assertEqual(candidate.slots[0].config.min_gpu_bytes, 90 * 1024**3)
        candidate.slots[0].proof = TargonIdleProof(
            'workload_test-123', 1005, 1001, True
        )
        proof = candidate.idle_probe(INTENT, 'workload_test-123')
        self.assertIsInstance(proof, TargonIdleProof)
        self.assertTrue(proof.idle)
        candidate.slots[0].proof = InferenceIdleProof(INSTANCE, 1005, 1001, True)
        self.assertIsNone(candidate.idle_probe(INTENT, 'workload_test-123'))

    def test_guard_is_fail_closed_and_rechecks_node_binding_and_stop(self):
        self.assertFalse(self.boot._start_allowed())
        self.boot.set_start_guard(lambda: True)
        self.assertTrue(self.boot._start_allowed())
        self.repo.node['binding_hash'] = 'changed'
        self.assertFalse(self.boot._start_allowed())
        self.repo.node['binding_hash'] = self.binding.fingerprint
        self.repo.node['desired_state'] = 'draining'
        self.assertFalse(self.boot._start_allowed())
        self.repo.node['desired_state'] = 'running'
        self.boot.request_drain()
        self.assertFalse(self.boot._start_allowed())

    def test_drain_preserves_collection_and_does_not_tick_a_new_boot(self):
        self.assertEqual(self.boot.tick(INTENT)['state'], 'ready')
        prior = [len(s.ticks) for s in self.boot.slots]
        result = self.boot.tick(INTENT, stopping=True)
        self.assertEqual(result['state'], 'draining')
        self.assertEqual([len(s.ticks) for s in self.boot.slots], prior)
        self.assertTrue(
            all(
                s.cancelled and s.fleet.drains and s.fleet.ticks
                for s in self.boot.slots
            )
        )
        self.assertTrue(all(s.closed == 0 for s in self.boot.slots))
        with self.assertRaisesRegex(BootError, 'identity_mismatch'):
            self.boot.tick(SECOND)

    def test_unknown_boot_is_blocked_with_safe_per_slot_cause(self):
        self.boot.slots[0].tick = lambda _: {
            'state': 'bootstrap_reconciliation_required',
            'phase': 'runtime_start_unknown',
            'failure_phase': 'runtime_manifest',
            'error_code': 'wangp_configuration_permissions',
            'error_type': 'ValueError',
            'traceback': 'SECRET',
        }
        result = self.boot.tick(INTENT)
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['reason_code'], 'bootstrap_reconciliation_required')
        self.assertEqual(
            result['slots'][0]['error_code'], 'wangp_configuration_permissions'
        )
        self.assertEqual(result['slots'][0]['failure_phase'], 'runtime_manifest')
        self.assertEqual(result['slots'][1]['state'], 'fleet_running')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(self.spawn, [])
        self.boot.slots[0].tick = lambda _: {'state': 'bootstrap_start_unknown'}
        self.assertEqual(self.boot.tick(INTENT)['state'], 'blocked')
        self.assertEqual([s.closed for s in self.boot.slots], [0, 0])

    def test_idle_proof_requires_every_slot_and_same_instance(self):
        a, b = self.boot.slots
        a.proof = InferenceIdleProof(INSTANCE, 1004, 1000, True)
        b.proof = InferenceIdleProof(INSTANCE, 1005, 1002, False)
        proof = self.boot.idle_probe(INTENT, INSTANCE)
        self.assertFalse(proof.idle)
        self.assertEqual((proof.observed_at, proof.idle_since), (1004, 1002))
        b.proof = InferenceIdleProof(INSTANCE, 1005, 1002, True)
        self.assertTrue(self.boot.idle_probe(INTENT, INSTANCE).idle)
        b.proof = InferenceIdleProof(SECOND, 1005, 1002, True)
        self.assertIsNone(self.boot.idle_probe(INTENT, INSTANCE))
        b.proof = None
        self.assertIsNone(self.boot.idle_probe(INTENT, INSTANCE))
        with self.assertRaisesRegex(BootError, 'identity_mismatch'):
            self.boot.idle_probe(INTENT, SECOND)

    def test_close_requires_destroyed_and_all_local_collectors_exited(self):
        with self.assertRaisesRegex(BootError, 'confirmed_destroyed'):
            self.boot.close()
        self.repo.intent['state'] = 'destroyed'
        child = NS(poll=lambda: None)
        self.boot.slots[1].fleet.children['worker'] = child
        with self.assertRaisesRegex(BootError, 'collection_still_running'):
            self.boot.close()
        self.assertEqual([s.closed for s in self.boot.slots], [0, 0])
        child.poll = lambda: 0
        self.boot.close()
        self.assertEqual([s.closed for s in self.boot.slots], [1, 1])

    def test_close_refuses_unowned_retained_fleet_or_worker_and_pending_upload(self):
        self.repo.intent['state'] = 'destroyed'
        slot = self.boot.slots[0]
        slot.preparation_pending = lambda: True
        with self.assertRaisesRegex(BootError, 'collection_still_running'):
            self.boot.close()
        slot.preparation_pending = lambda: False
        slot.fleet = None
        directory = slot.config.work_dir / INTENT
        directory.mkdir(parents=True)
        retained = directory / 'fleet.json'
        retained.write_text('{}')
        with self.assertRaisesRegex(BootError, 'child_ownership_unconfirmed'):
            self.boot.close()
        retained.unlink()
        self.repo.worker = {'current_job_id': None, 'drain_requested': True}
        with self.assertRaisesRegex(BootError, 'child_ownership_unconfirmed'):
            self.boot.close()
        self.assertEqual([s.closed for s in self.boot.slots], [0, 0])

    def test_close_requires_all_expected_owned_child_handles(self):
        self.repo.intent['state'] = 'destroyed'
        fleet = self.boot.slots[0].fleet
        fleet.config = NS(slots=[NS(enabled=True, spec=NS(worker_id='expected-child'))])
        fleet.children = {'different-child': NS(poll=lambda: 0)}
        with self.assertRaisesRegex(BootError, 'child_ownership_unconfirmed'):
            self.boot.close()
        self.assertEqual([s.closed for s in self.boot.slots], [0, 0])

    def test_worker_wrapper_keeps_original_fleet_hash_slot_and_intent(self):
        arguments = [
            sys.executable,
            '-m',
            'studio_platform.fleet',
            '--config',
            'fleet.json',
            '--slot',
            'lium-test-gpu0',
            '--config-hash',
            'c' * 64,
        ]
        self.boot._popen(arguments, stdout=subprocess.DEVNULL)
        command, kwargs = self.spawn[0]
        self.assertEqual(
            command[:4],
            [sys.executable, '-m', 'studio_platform.operator_boot', '--worker'],
        )
        self.assertEqual(
            command[4:8],
            ['--runtime-config', self.config['config_path'], '--intent', INTENT],
        )
        self.assertEqual(command[8:], arguments[3:])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        with self.assertRaisesRegex(BootError, 'command_invalid'):
            self.boot._popen([sys.executable, 'untrusted.py'])
        self.assertEqual(len(self.spawn), 1)

    def test_local_shutdown_keeps_collectors_and_does_not_claim_provider_destruction(
        self,
    ):
        for index, slot in enumerate(self.boot.slots):
            slot.fleet.children = {f'child-{index}': NS(poll=lambda: None)}
        self.boot.request_drain()
        self.assertEqual(
            self.boot.shutdown_status(),
            {'ownership_known': True, 'children_done': False},
        )
        with self.assertRaisesRegex(BootError, 'local_shutdown_unconfirmed'):
            self.boot.release_after_drain()
        for slot in self.boot.slots:
            for child in slot.fleet.children.values():
                child.poll = lambda: 0
        self.repo.worker = {'current_job_id': 'original-job', 'drain_requested': True}
        self.assertFalse(self.boot.shutdown_status()['children_done'])
        self.repo.worker['current_job_id'] = None
        self.assertEqual(
            self.boot.shutdown_status(),
            {'ownership_known': True, 'children_done': True},
        )
        self.boot.release_after_drain()
        self.assertEqual([s.closed for s in self.boot.slots], [1, 1])
        self.assertEqual(self.repo.intent['state'], 'starting')

    def test_local_shutdown_does_not_infer_child_exit_from_absent_handles(self):
        self.boot.request_drain()
        self.assertFalse(self.boot.shutdown_status()['ownership_known'])
        for slot in self.boot.slots:
            slot.fleet = None
        directory = self.boot.slots[0].config.work_dir / INTENT
        directory.mkdir(parents=True)
        (directory / 'fleet.json').write_text('{}')
        self.assertEqual(
            self.boot.shutdown_status(),
            {'ownership_known': False, 'children_done': False},
        )
        with self.assertRaisesRegex(BootError, 'local_shutdown_unconfirmed'):
            self.boot.release_after_drain()
        self.assertEqual([s.closed for s in self.boot.slots], [0, 0])

    def _run_worker(self, summary=None):
        worker_id = 'lium-' + INTENT.replace('-', '') + '-gpu0'
        expected_path = (
            Path(self.config['work_dir'])
            / 'boot'
            / INTENT
            / '0'
            / INTENT
            / 'fleet.json'
        )
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        identity = {
            'intent_id': INTENT,
            'instance_id': INSTANCE,
            'configuration_id': self.binding.configuration_id,
            'sources': self.binding.boot['source_sha256'][0],
            'backend': 'wangp-worker',
            'engine_manifest_digest': self.binding.engine_manifest_digest,
            'output_delivery': 'native-frames-v1',
        }
        state = {
            'identity': identity,
            'runtime_validation': {
                'profile': QUEUED_TASK_PROFILE,
                'state': 'runtime_ready',
                'generation_verified': False,
            },
            'phase': 'fleet_starting',
        }
        (expected_path.parent / 'bootstrap-state.json').write_text(json.dumps(state))
        spec = NS(
            configuration_id=self.binding.configuration_id,
            model_id=self.binding.model_id,
            pool=self.binding.pool,
            provider='lium',
            instance_id=INSTANCE,
            engine_manifest_digest=self.binding.engine_manifest_digest,
            output_delivery='native-frames-v1',
            recipe_ids=self.binding.recipe_ids,
        )
        fleet = NS(
            fingerprint=lambda: 'c' * 64,
            slots=[NS(spec=spec)],
            slot=lambda identity: NS(spec=spec),
        )
        fake_runtime = NS(
            load_runtime_config=lambda path: self.config,
            create_registry=lambda path: NS(get=lambda key: self.binding),
        )
        callbacks = {}
        drains = []

        def run_slot(config, worker, settings, **kwargs):
            callbacks.update(kwargs['runner_factory']())
            return None

        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    sys.modules, {'studio_platform.operator_runtime': fake_runtime}
                )
            )
            stack.enter_context(
                patch(
                    'studio_platform.settings.Settings.from_environment',
                    return_value=NS(database_url='unused'),
                )
            )
            stack.enter_context(
                patch.object(subject, 'Repository', return_value=self.repo)
            )
            stack.enter_context(patch.object(subject, 'read_fleet', return_value=fleet))
            stack.enter_context(
                patch.object(
                    subject,
                    'WorkerControl',
                    return_value=NS(drain=lambda value: drains.append(value)),
                )
            )
            runner = stack.enter_context(
                patch.object(subject, 'run_slot', side_effect=run_slot)
            )
            stack.enter_context(
                patch(
                    'studio_platform.queued_task_runner.QueuedTaskRunner',
                    side_effect=lambda **kwargs: kwargs,
                )
            )
            if summary is not None:
                stack.enter_context(
                    patch(
                        'studio_platform.queued_task_runner.read_verification_summary',
                        return_value=summary,
                    )
                )
            self.assertEqual(
                subject.run_worker(
                    'runtime', INTENT, str(expected_path), worker_id, 'c' * 64
                ),
                0,
            )
        return callbacks, runner.call_count, drains, identity

    def test_first_real_worker_retains_source_and_delivery_identity_without_smoke(self):
        callbacks, count, drains, identity = self._run_worker()
        self.assertEqual(count, 1)
        self.assertEqual(drains, [])
        self.assertEqual(
            callbacks['evidence_identity'],
            {**identity, 'qualification_profile': QUEUED_TASK_PROFILE},
        )
        self.assertFalse(callbacks['stop_new']())
        request = {
            'request': {'deployment_profile_id': self.binding.runtime_profile_id},
            'execution_plan': {'configuration_id': self.binding.configuration_id},
            'expected_runtime_s': 200,
        }
        self.assertTrue(callbacks['job_allowed'](request))
        request['request']['deployment_profile_id'] = PROFILE_IDS[0]
        self.assertFalse(callbacks['job_allowed'](request))
        self.repo.node['desired_state'] = 'stopped'
        self.assertTrue(callbacks['stop_new']())

    def test_worker_rechecks_shortened_deadline_and_stale_proof_without_stranding_collection(
        self,
    ):
        callbacks, _, _, _ = self._run_worker()
        request = {
            'request': {'deployment_profile_id': self.binding.runtime_profile_id},
            'execution_plan': {'configuration_id': self.binding.configuration_id},
            'expected_runtime_s': 500,
        }
        self.assertTrue(callbacks['job_allowed'](request))
        self.repo.intent['hard_deadline'] = 1600
        self.assertFalse(callbacks['job_allowed'](request))
        self.assertFalse(callbacks['stop_new']())
        request['expected_runtime_s'] = 100
        self.assertTrue(callbacks['job_allowed'](request))
        self.repo.node['payload']['lifetime']['observed_at'] = 969
        self.assertFalse(callbacks['job_allowed'](request))
        self.assertFalse(callbacks['stop_new']())
        self.repo.node['payload']['lifetime']['observed_at'] = 1000
        self.repo.intent['state'] = 'draining'
        self.assertTrue(callbacks['stop_new']())
        self.assertFalse(callbacks['job_allowed'](request))

    def test_drained_idle_worker_is_not_registered_ready_again(self):
        self.repo.worker = {'drain_requested': True, 'current_job_id': None}
        callbacks, count, drains, _ = self._run_worker()
        self.assertEqual((callbacks, count), ({}, 0))
        self.assertEqual(len(drains), 1)

    def test_quarantined_or_drained_bound_job_keeps_collection_runner(self):
        for drained, summary in ((False, {'runtime_quarantined': True}), (True, {})):
            with self.subTest(drained=drained):
                self.repo.worker = {
                    'drain_requested': drained,
                    'current_job_id': 'accepted-job',
                }
                callbacks, count, drains, _ = self._run_worker(summary)
                self.assertEqual(count, 1)
                self.assertEqual(len(drains), 1)
                self.assertTrue(callbacks['stop_new']())
                self.assertFalse(
                    callbacks['job_allowed'](
                        {'request': {}, 'execution_plan': {}, 'expected_runtime_s': 100}
                    )
                )

    def test_prior_evidence_cannot_belong_to_another_worker_or_model(self):
        for summary in ({'worker_id': 'another-worker'}, {'model_id': 'another-model'}):
            with (
                self.subTest(summary=summary),
                self.assertRaisesRegex(BootError, 'evidence_identity_conflict'),
            ):
                self._run_worker(summary)


if __name__ == '__main__':
    unittest.main()
