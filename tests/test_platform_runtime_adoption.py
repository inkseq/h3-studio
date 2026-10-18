"""Existing-runtime adoption with fake transport; no rents or inference POSTs."""
from dataclasses import replace
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import select

from studio_platform import runtime_adoption
from studio_platform.lium_bootstrap import BootError, COMFY_REVISION, MODEL_REVISION
from studio_platform.production_scaler_boot import ProductionBoot
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.repository import registered_workers
from studio_platform.worker import Outcome
from test_platform_lium_bootstrap import FakeBackend, FakeHost
from test_platform_production_scaler import configuration, FakeProvider
from test_platform_repository import LedgerCase


class RuntimeAdoptionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.config = replace(configuration(Path(self.temp.name), self.now),
            configuration_id='adopted-config', qualification_profile=QUEUED_TASK_PROFILE,
            launches=[{**row, 'configuration_id': 'adopted-config'}
                for row in configuration(Path(self.temp.name), self.now).launches],
            manifests=[{**row, 'configuration_id': 'adopted-config'}
                for row in configuration(Path(self.temp.name), self.now).manifests])
        self.config.work_dir.mkdir()
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        self.intent = self.repo.reserve_instance_intent(self.scope, self.config.pool, 'existing-runtime',
            physical_gpus=1, slots=1, reserved_cost_microusd=2_000_000,
            hard_deadline=self.now+7200, budget_account_ids=['owner-budget'], dry_run=False, provider='lium')
        self.repo.update_instance(self.intent['id'], 'creating')
        self.repo.update_instance(self.intent['id'], 'starting', provider_instance_id=str(uuid.uuid4()))
        self.intent = self.repo.list_instance_intents()[0]
        self.host, self.backend = FakeHost(), FakeBackend()
        self.backend.kind = 'comfy-worker'
        self.provider = FakeProvider(lambda: self.now)
        self.provider.ssh_connection = lambda *args: {'host': '203.0.113.1', 'port': 22}
        self.boot = self.make_boot()
        files, self.host.manifest = self.boot._sources()
        self.sources = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
        self.new_identity = {'intent_id': self.intent['id'], 'instance_id': self.intent['provider_instance_id'],
            'configuration_id': self.config.configuration_id, 'sources': self.sources}
        self.old_identity = {**self.new_identity, 'configuration_id': 'old-synthetic-config'}
        self.host.identity = self.old_identity
        tag = 'boot-'+self.intent['id'].replace('-', '')
        tags = [tag, 'firstlast4-768p-5s-v1-'+tag, 'ref4-bounded-768p-5s-v1-'+tag]
        self.tasks = [{'tag': tag, 'task_id': 'prior-'+str(i), 'status': 'succeeded', 'upstream_stopped': True}
                      for i, tag in enumerate(tags)]
        self.polls = []
        def poll(tag, task):
            self.polls.append((tag, task))
            return Outcome('succeeded', task)
        self.backend.poll = poll
        self.proof = {'version': 1, 'profile': QUEUED_TASK_PROFILE,
            'old_identity': self.old_identity, 'new_identity': self.new_identity,
            'model_id': self.config.launches[0]['model_id'], 'model_revision': MODEL_REVISION,
            'comfyui_revision': COMFY_REVISION, 'physical_gpu_uuid': self.host.report()['gpus'][0]['uuid'],
            'prior_tasks': self.tasks, 'issued_at': self.now-1, 'expires_at': self.now+300,
            'operator_config_hash': self.config.fingerprint(), 'ledger_handoff_sha256': 'a'*64}
        self.proof_path = self.boot.config.work_dir/self.intent['id']/'runtime-adoption.json'
        self.proof_path.parent.mkdir(parents=True)
        self.write_proof()
        real_fstat = os.fstat
        def root_owned(fd):
            meta = real_fstat(fd)
            return SimpleNamespace(st_mode=meta.st_mode, st_nlink=meta.st_nlink, st_size=meta.st_size, st_uid=0)
        patcher = patch.object(runtime_adoption, 'fstat', side_effect=root_owned)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_boot(self):
        return ProductionBoot(self.repo, self.provider, self.config, self.intent, 19300,
            config_path=Path(self.temp.name)/'synthetic-config.json', ssh_factory=lambda *args: self.host,
            backend_factory=lambda **kwargs: self.backend,
            verify_smoke=lambda *args: (_ for _ in ()).throw(AssertionError('synthetic verification prohibited')))

    def write_proof(self, value=None):
        self.proof_path.write_text(json.dumps(value or self.proof, sort_keys=True), encoding='utf-8')
        self.proof_path.chmod(0o644)

    def start(self):
        process = SimpleNamespace(pid=4242, poll=lambda: None, send_signal=lambda value: None)
        with patch.object(self.boot, '_popen_impl', return_value=process):
            return self.boot.tick(self.intent['id'])

    def workers(self):
        with self.repo.engine.connect() as connection:
            return list(connection.execute(select(registered_workers.c.id)).scalars())

    def test_existing_runtime_serves_queue_after_all_three_prior_tasks_without_setup_or_inference(self):
        result = self.start()
        self.assertEqual(result['state'], 'fleet_running')
        self.assertFalse(result['generation_verified'])
        self.assertTrue(result['awaiting_real_task'])
        self.assertEqual(self.polls, [(task['tag'], task['task_id']) for task in self.tasks])
        self.assertEqual(self.host.starts, 0)
        self.assertEqual(self.host.uploads, 0)
        self.assertEqual(self.backend.submissions, 0)
        self.assertEqual(self.backend.fetches, 0)
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.host.identity, self.old_identity)
        self.assertEqual(len(self.workers()), 1)
        state = json.loads((self.proof_path.parent/'bootstrap-state.json').read_text())
        self.assertEqual(state['identity'], self.new_identity)
        self.assertFalse(state['runtime_adoption']['generation_verified'])
        self.assertEqual(state['runtime_validation'], {
            'profile': QUEUED_TASK_PROFILE, 'state': 'runtime_ready', 'generation_verified': False})
        self.assertNotIn('smoke_submission_started', state)
        self.assertNotIn('smoke_task_id', state)
        self.assertNotIn('evidence', state)

    def test_running_unknown_or_busy_prior_runtime_never_registers_or_submits(self):
        for outcome in ('running', 'unknown'):
            self.backend.poll = lambda tag, task, state=outcome: Outcome(state, task)
            with self.subTest(outcome=outcome):
                result = self.boot.tick(self.intent['id'])
                self.assertEqual(result['state'], 'runtime_adoption_waiting_prior_tasks')
                self.assertFalse(result['generation_verified'])
                self.assertEqual(self.workers(), [])
        self.backend.poll = lambda tag, task: Outcome('succeeded', task)
        self.backend.queue = {'queue_running': [[1, 'foreign']], 'queue_pending': []}
        self.assertEqual(self.boot.tick(self.intent['id'])['state'], 'runtime_adoption_upstream_busy')
        self.assertEqual(self.workers(), [])
        self.assertEqual(self.host.starts, 0)
        self.assertEqual(self.backend.submissions, 0)

    def test_bridge_rejects_other_pod_sources_revision_model_configuration_and_legacy_profile(self):
        for field, value in (('model_id', 'other-model'), ('model_revision', '0'*40),
                             ('comfyui_revision', '0'*40), ('operator_config_hash', '0'*64),
                             ('physical_gpu_uuid', 'GPU-other-device-0000')):
            changed = copy.deepcopy(self.proof)
            changed[field] = value
            self.write_proof(changed)
            with self.subTest(field=field), self.assertRaises(BootError):
                self.boot.tick(self.intent['id'])
        for identity, field, value in (('new_identity', 'instance_id', str(uuid.uuid4())),
                                      ('old_identity', 'intent_id', str(uuid.uuid4())),
                                      ('new_identity', 'configuration_id', 'other-config'),
                                      ('old_identity', 'sources', {**self.sources, 'bootstrap_cloud.py': '0'*64})):
            changed = copy.deepcopy(self.proof)
            changed[identity][field] = value
            self.write_proof(changed)
            with self.subTest(identity=identity, field=field), self.assertRaises(BootError):
                self.boot.tick(self.intent['id'])
        self.write_proof()
        with self.assertRaises(BootError):
            runtime_adoption.load_adoption(self.proof_path, finite=replace(self.config, qualification_profile='fl50'),
                intent=self.intent, sources=self.sources, now=self.now)
        self.assertEqual(self.workers(), [])

    def test_remote_marker_pinned_weights_and_gpu_still_need_fresh_dynamic_evidence(self):
        for changed in ({'identity': {**self.old_identity, 'configuration_id': 'foreign'}},
                        {'actual_comfy_revision': '0'*40}, {'files': {}},
                        {'gpus': []}, {'runtime': {'gpu_total_bytes': 1}}):
            self.host.patch = changed
            with self.subTest(changed=changed), self.assertRaises(BootError):
                self.boot.tick(self.intent['id'])
            self.assertEqual(self.workers(), [])
        self.assertEqual(self.host.starts, 0)
        self.assertEqual(self.backend.submissions, 0)

    def test_prior_task_manifest_is_bounded_unique_and_only_known_synthetic_tags(self):
        for tasks in ([], self.tasks+self.tasks[:1], [self.tasks[0], self.tasks[0]],
                      [{**self.tasks[0], 'tag': 'real-user-task'}],
                      [{**self.tasks[0], 'upstream_stopped': False}],
                      [{**self.tasks[0], 'status': 'running'}]):
            self.write_proof({**self.proof, 'prior_tasks': tasks})
            with self.subTest(tasks=tasks), self.assertRaises(BootError):
                self.boot.tick(self.intent['id'])
        self.assertEqual(self.workers(), [])

    def test_expired_bridge_only_reconnects_its_exact_previously_adopted_receipt(self):
        self.proof['expires_at'] = self.now+1
        self.write_proof()
        self.start()
        receipt = json.loads((self.proof_path.parent/'bootstrap-state.json').read_text())
        self.now += 2
        loaded, _ = runtime_adoption.load_adoption(self.proof_path, finite=self.config, intent=self.intent,
            sources=self.sources, now=self.now, receipt=receipt)
        self.assertEqual(loaded, self.proof)
        with self.assertRaises(BootError):
            runtime_adoption.load_adoption(self.proof_path, finite=self.config, intent=self.intent,
                sources=self.sources, now=self.now)
        changed = copy.deepcopy(receipt)
        changed['runtime_adoption']['proof_sha256'] = '0'*64
        with self.assertRaises(BootError):
            runtime_adoption.load_adoption(self.proof_path, finite=self.config, intent=self.intent,
                sources=self.sources, now=self.now, receipt=changed)
        self.backend.queue = {'queue_running': [[1, 'real-user-task']], 'queue_pending': []}
        self.polls.clear()
        again = self.boot.tick(self.intent['id'])
        self.assertEqual(again['state'], 'fleet_running')
        self.assertEqual(self.polls, [])
        self.assertEqual(self.backend.submissions, 0)
        self.assertEqual(self.host.identity, self.old_identity)
        # A new controller retains the bridge after authorization expiry but
        # still follows the existing no-automatic-fleet-restart protection.
        self.boot = self.make_boot()
        restarted = self.boot.tick(self.intent['id'])
        self.assertEqual(restarted['state'], 'fleet_recovery_required')
        self.assertFalse(restarted['generation_verified'])
        self.assertEqual(self.boot.host.report()['identity'], self.new_identity)
        self.assertEqual(self.host.starts, 0)
        self.assertEqual(self.backend.submissions, 0)

    def test_root_proof_permissions_duplicates_and_size_fail_closed(self):
        if os.name != 'nt':
            self.proof_path.chmod(0o666)
            with self.assertRaises(BootError):
                self.boot.tick(self.intent['id'])
        self.proof_path.write_text('{"version":1,"version":1}', encoding='utf-8')
        self.proof_path.chmod(0o644)
        with self.assertRaises(BootError):
            self.boot.tick(self.intent['id'])
        self.proof_path.write_text('x'*65537, encoding='utf-8')
        with self.assertRaises(BootError):
            self.boot.tick(self.intent['id'])


if __name__ == '__main__':
    unittest.main()
