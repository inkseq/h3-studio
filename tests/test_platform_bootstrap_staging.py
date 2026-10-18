"""Fake blocked uploads and real isolated ledgers; no SSH/provider/model calls."""
import json
import contextlib
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import insert, select

from studio_platform.bootstrap_staging import PollableUpload, UploadCancelled
from studio_platform.autoscale import ScalePolicy
from studio_platform.repository import scaler_actions, scaler_leaders
from studio_platform.lium_bootstrap import BootError
from studio_platform.production_scaler_boot import ProductionBoot
from studio_platform.scaler import ProviderFact, ScaleCoordinator
from studio_platform.wangp_bootstrap import WanGPSSHHost
from test_platform_production_scaler import configuration, FakeProvider
import test_platform_wangp_bootstrap as boot_fixture
import test_platform_wangp_service_hold as service_fixture


class BlockedUpload:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0
        self.failure = False
        self.before = lambda: None

    def __call__(self, files=None, *, progress, should_stop):
        self.calls += 1
        self.before()
        progress(8, 100)
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("Test upload was not released")
        if should_stop():
            raise UploadCancelled
        if self.failure:
            raise OSError('Synthetic transfer interruption')
        progress(100, 100)

    def finish(self, operation):
        self.release.set()
        if operation._thread is not None:
            operation._thread.join(2)
            if operation._thread.is_alive():
                raise AssertionError("Test upload did not finish")


class PollableBootTests(unittest.TestCase):
    def setUp(self):
        # Reuse the explicit isolated fixture without inheriting/re-running its tests.
        self.f = boot_fixture.WanGPBootTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.boot = self.f.boot()
        self.boot.enable_pollable_upload()
        self.boot.start_guard = lambda: True
        self.upload = BlockedUpload()
        self.f.host.upload = self.upload
        self.addCleanup(lambda: self.upload.finish(self.boot._preparation))
        self.receipt = self.f.config.work_dir/self.f.intent['id']/'bootstrap-state.json'
        self.connect = patch('studio_platform.wangp_bootstrap.connect_backend', self.f.connect)
        self.connect.start()
        self.addCleanup(self.connect.stop)

    def tick(self, boot=None):
        return (boot or self.boot).tick(self.f.intent['id'])

    def start_upload(self):
        self.upload.before = lambda: self.assertEqual(json.loads(self.receipt.read_text())['phase'], 'staging')
        self.assertEqual(self.tick()['state'], 'staging')
        self.assertTrue(self.upload.entered.wait(1))

    def test_upload_is_persisted_once_and_polled_without_blocking_controller(self):
        self.start_upload()
        self.f.now += 240
        value = self.tick()
        self.assertEqual(value['state'], 'staging')
        self.assertEqual(value['transferred_bytes'], 8)
        self.assertEqual(self.upload.calls, 1)
        self.assertEqual(self.f.host.starts, 0)
        self.assertTrue(self.boot.preparation_pending())
        self.upload.finish(self.boot._preparation)
        self.assertEqual(self.tick()['state'], 'runtime_ready')
        self.assertEqual(self.f.host.starts, 1)
        self.assertEqual(self.upload.calls, 1)

    def test_second_controller_cannot_upload_while_first_operation_owns_lock(self):
        self.start_upload()
        other = self.f.boot()
        other.enable_pollable_upload()
        other.start_guard = lambda: True
        self.assertEqual(self.tick(other)['state'], 'staging_locked')
        self.assertEqual(self.upload.calls, 1)
        self.upload.finish(self.boot._preparation)
        self.assertEqual(self.tick()['state'], 'runtime_ready')
        self.assertEqual(self.tick(other)['state'], 'runtime_ready')
        self.assertEqual(self.upload.calls, 1)
        self.assertEqual(self.f.host.starts, 1)

    def test_authority_loss_after_upload_never_starts_setup(self):
        self.start_upload()
        self.upload.finish(self.boot._preparation)
        self.boot.start_guard = lambda: False
        self.assertEqual(self.tick()['state'], 'staging_cancelled')
        self.assertEqual(json.loads(self.receipt.read_text())['phase'], 'staging_cancelled')
        self.assertEqual(self.f.host.starts, 0)

    def test_authority_loss_while_upload_pending_records_stop_after_thread_exits(self):
        self.start_upload()
        self.boot.start_guard = lambda: False
        self.assertEqual(self.tick()['state'], 'staging_authority_unavailable')
        self.assertIsNone(self.boot.preparation_stopped_before_start(json.loads(self.receipt.read_text())))
        self.upload.finish(self.boot._preparation)
        self.assertEqual(self.tick()['state'], 'staging_cancelled')
        self.assertIsNotNone(self.boot.preparation_stopped_before_start(json.loads(self.receipt.read_text())))
        self.assertEqual(self.f.host.starts, 0)

    def test_cancel_during_transfer_keeps_original_receipt_without_start(self):
        self.start_upload()
        identity = json.loads(self.receipt.read_text())['identity']
        self.boot.cancel_preparation()
        self.upload.finish(self.boot._preparation)
        self.assertEqual(self.tick()['state'], 'staging_cancelled')
        self.assertEqual(json.loads(self.receipt.read_text())['identity'], identity)
        self.assertEqual(self.f.host.starts, 0)

    def test_restart_before_start_can_resume_bytes_but_unknown_start_never_reuploads(self):
        self.start_upload()
        self.upload.finish(self.boot._preparation)
        # Completed upload but controller lost before recording/starting. The
        # real uploader checks the exact hash/prefix; no setup was dispatched.
        other = self.f.boot()
        other.enable_pollable_upload()
        other.start_guard = lambda: True
        self.addCleanup(lambda: self.upload.finish(other._preparation))
        # Hold the resumed upload too: the first transfer left its event set,
        # so a fast thread could finish and start setup before lose_start was
        # enabled below. Exercise the intended restart boundary deterministically.
        self.upload.release.clear()
        self.assertEqual(self.tick(other)['state'], 'staging')
        self.upload.finish(other._preparation)
        self.f.host.lose_start = True
        self.assertEqual(self.tick(other)['state'], 'bootstrap_start_unknown')
        self.assertEqual(self.f.host.starts, 1)
        restarted = self.f.boot()
        restarted.enable_pollable_upload()
        self.assertEqual(self.tick(restarted)['state'], 'runtime_ready')
        self.assertEqual(self.upload.calls, 2)
        self.assertEqual(self.f.host.starts, 1)

    def test_failure_is_terminal_until_explicit_recovery_and_never_prints_details(self):
        def fail(files, **options):
            raise RuntimeError('private path and credential-like text must not be returned')
        self.f.host.upload = fail
        self.tick()
        self.boot._preparation._thread.join(1)
        result = self.tick()
        self.assertEqual(result['state'], 'staging_failed')
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(self.tick()['state'], 'staging_failed')
        self.assertEqual(self.f.host.starts, 0)


class ProductionStagingIdleTests(unittest.TestCase):
    """Real ProductionBoot + ledger retirement, with owned fake SSH/upload only."""
    def setUp(self):
        self.f = boot_fixture.WanGPBootTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.finite = replace(configuration(self.f.root, self.f.now),
            source_dir=self.f.sources,
            source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.f.sources.iterdir()},
            execution_backend='wangp-worker', engine_manifest_digest=self.f.manifest.digest,
            qualification_profile=boot_fixture.QUEUED_TASK_PROFILE)
        self.finite.work_dir.mkdir()
        self.provider = FakeProvider(lambda: self.f.now)
        self.provider.ssh_connection = lambda *a: {}
        self.boot = self.make_boot()
        self.upload = BlockedUpload()
        self.f.host.upload = self.upload
        self.addCleanup(lambda: self.upload.finish(self.boot._preparation))
        self.f.host.close = lambda: None
        self.proof_changes = {}
        self.f.host.preparation_idle_report = lambda *, expected_prestart_identity: {
            'identity': expected_prestart_identity, 'state': 'not_started', 'setup_markers_absent': True,
            'process_visibility_complete': True, 'bootstrap_process_count': 0,
            'runtime_process_count': 0, 'runtime_port_listening': False, **self.proof_changes}
        self.receipt = self.boot.config.work_dir/self.f.intent['id']/'bootstrap-state.json'

    def make_boot(self):
        boot = ProductionBoot(self.f.repo, self.provider, self.finite, self.f.intent, 19300,
            config_path=self.f.root/'config.json', ssh_factory=lambda *a: self.f.host)
        boot.enable_pollable_upload()
        boot.start_guard = lambda: True
        return boot

    def tick(self, **kwargs):
        return self.boot.tick(self.f.intent['id'], **kwargs)

    def probe(self, boot=None):
        return (boot or self.boot).idle_probe(self.f.intent['id'], boot_fixture.POD)

    def start(self):
        self.assertEqual(self.tick()['state'], 'staging')
        self.assertTrue(self.upload.entered.wait(1))

    def fail(self):
        self.start()
        self.upload.failure = True
        self.upload.finish(self.boot._preparation)
        self.assertEqual(self.tick()['state'], 'staging_failed')

    def test_failed_stopped_upload_proves_idle_and_existing_ledger_destroys_once(self):
        self.fail()
        self.assertTrue(self.probe().idle)
        self.assertEqual(self.f.host.starts, 0)
        intent = self.f.repo.list_instance_intents()[0]
        self.f.repo.update_instance(intent['id'], 'draining')
        with self.f.repo.transaction() as conn:
            conn.execute(insert(scaler_actions).values(intent_id=intent['id'], pool=intent['pool'],
                launch_spec={}, create_started_at=self.f.now))
        def reconcile(tag, instance_id):
            if self.provider.destroys:
                return self.provider.facts[tag]
            proof = self.probe()
            return ProviderFact('running', instance_id, idle_confirmed=proof.idle, idle_since=proof.idle_since)
        self.provider.reconcile = reconcile
        scaler = ScaleCoordinator(self.f.repo, provider=self.provider, enabled=True)
        budget = self.f.repo.get_budget('owner-budget')
        for _ in range(2):
            scaler.tick('staging-retirement', self.f.scope, intent['pool'], [], [],
                policy=ScalePolicy(dry_run=False), budget_account_ids=['owner-budget'])
            self.f.now += 16
        self.assertEqual(self.provider.destroys, [intent['id']])
        self.assertEqual(self.f.repo.list_instance_intents()[0]['state'], 'destroyed')
        # Destruction is not a fabricated invoice or release of unknown cost.
        self.assertEqual(self.f.repo.get_budget('owner-budget'), budget)
        self.assertEqual(self.f.host.starts, 0)

    def test_stopping_pending_upload_waits_then_records_cancel_and_proves_idle(self):
        self.start()
        self.assertFalse(self.tick(stopping=True)['children_done'])
        with self.assertRaises(BootError):
            self.probe()
        self.upload.finish(self.boot._preparation)
        self.assertTrue(self.tick(stopping=True)['children_done'])
        self.assertEqual(json.loads(self.receipt.read_text())['phase'], 'staging_cancelled')
        self.assertTrue(self.probe().idle)
        self.assertEqual(self.f.host.starts, 0)

    def test_durable_phase_alone_or_new_controller_cannot_certify_stop(self):
        self.fail()
        with self.assertRaises(BootError):
            self.probe(self.make_boot())
        self.boot._setup_dispatched = True
        with self.assertRaises(BootError):
            self.probe()

    def test_remote_runtime_markers_or_incomplete_observation_prevents_retirement(self):
        self.fail()
        for change in ({'setup_markers_absent': False}, {'state': 'failed'}, {'identity': {}},
                {'process_visibility_complete': False}, {'bootstrap_process_count': 1},
                {'runtime_process_count': 1}, {'runtime_port_listening': True}):
            with self.subTest(change=change):
                self.proof_changes = change
                self.assertFalse(self.probe().idle)


class RemotePrestartProofTests(unittest.TestCase):
    def test_authenticated_script_requires_absent_markers_and_full_process_socket_observation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); proc = root/'proc'; (proc/'1').mkdir(parents=True); (proc/'net').mkdir()
            (proc/'1'/'cmdline').write_bytes(b'/usr/bin/python3\0-c\0read-only-probe\0')
            for name in ('tcp', 'tcp6'):
                (proc/'net'/name).write_text('sl local_address rem_address st\n')
            host = WanGPSSHHost.__new__(WanGPSSHHost)
            def run(script, **kwargs):
                script = script.replace('/workspace/h3-studio', root.as_posix()).replace(
                    "Path('/proc')", 'Path('+repr(proc.as_posix())+')')
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(compile(script, '<offline-prestart-proof>', 'exec'), {})
                return json.loads(output.getvalue())
            host.run = run
            identity = {'backend': 'wangp-worker', 'intent_id': 'synthetic', 'instance_id': 'synthetic-pod'}
            observe = lambda: host.preparation_idle_report(expected_prestart_identity=identity)
            self.assertEqual(observe()['state'], 'not_started')
            for name in ('sixnine-bootstrap-identity.json', 'setup-status.json', 'sixnine-bootstrap.lock', 'wangp-token'):
                marker = root/name; marker.write_text('synthetic')
                self.assertFalse(observe()['process_visibility_complete'])
                marker.unlink()
            (proc/'1'/'cmdline').write_bytes(b'python\0-m\0studio_platform.runtime_hosts.wangp_launcher\0')
            self.assertEqual(observe()['runtime_process_count'], 1)
            (proc/'1'/'cmdline').write_bytes(b'python\0-m\0studio_platform.runtime_hosts.wangp_download\0')
            self.assertEqual(observe()['bootstrap_process_count'], 1)
            self.assertEqual(observe()['runtime_process_count'], 0)
            (proc/'1'/'cmdline').unlink()
            self.assertFalse(observe()['process_visibility_complete'])


class StagingControllerTests(unittest.TestCase):
    def setUp(self):
        self.f = service_fixture.WanGPServiceHoldTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.upload = BlockedUpload()
        upload = self.upload

        class FakeStagingBoot:
            def __init__(self, repo, provider, config, intent, port, **kwargs):
                self.config, self.intent = config, intent
                self._preparation = PollableUpload()
                self.directory = config.work_dir/'boot'/intent['id']
                self.directory.mkdir(parents=True, exist_ok=True)

            def enable_pollable_upload(self):
                pass

            def tick(self, intent_id, *, stopping=False):
                if stopping:
                    self.request_drain()
                value = self._preparation.poll(self.directory, intent_id, upload)
                (self.directory/'bootstrap-state.json').write_text(json.dumps({
                    'identity': service_fixture.identity(self.config, self.intent), 'phase': value['state']}))
                return value

            def request_drain(self):
                self._preparation.cancel()

            def children_done(self):
                return not self._preparation.pending()

            def close_if_safe(self, **options):
                return self.children_done()

        self.f.controller.current.boot_factory = self.f.controller.boot_factory = FakeStagingBoot
        self.scoped = self.f.submit('superdan', 'blocked-upload')
        self.deadline = self.f.wait_deadline(self.scoped[1])
        for _ in range(4):
            self.f.tick()
            if self.f.controller.current.boots:
                break
        self.boot = next(iter(self.f.controller.current.boots.values()))
        self.addCleanup(lambda: self.upload.finish(self.boot._preparation))
        self.assertTrue(self.upload.entered.wait(1))

    def test_long_upload_keeps_leader_and_status_fresh_without_second_rental_or_job(self):
        budget = self.f.repo.get_budget('finite-budget')
        for _ in range(5):
            self.f.now += 60
            status = self.f.controller.tick()
            self.assertEqual(status['observed_at'], self.f.now)
            self.assertTrue(status['admission_ready'])
            with self.f.repo.engine.connect() as conn:
                expiry = conn.execute(select(scaler_leaders.c.expires_at).where(
                    scaler_leaders.c.pool == self.f.config.pool)).scalar_one()
            self.assertGreater(expiry, self.f.now)
        self.assertEqual(self.upload.calls, 1)
        self.assertEqual(len(self.f.provider.creates), 1)
        self.assertEqual(self.f.repo.get_budget('finite-budget'), budget)
        self.assertEqual(self.f.wait_deadline(self.scoped[1]), self.deadline)

    def test_start_guard_rechecks_fence_cancellation_and_approval(self):
        current = self.f.controller.current
        intent_id = self.boot.intent['id']
        lease = current.scaler.acquire(current.config.pool, current.leader_id)
        self.assertTrue(current._bootstrap_start_allowed(intent_id, lease))
        self.f.now += 181
        other = current.scaler.acquire(current.config.pool, 'competing-controller')
        self.assertIsNotNone(other)
        self.assertFalse(current._bootstrap_start_allowed(intent_id, lease))
        self.f.now += 181
        lease = current.scaler.acquire(current.config.pool, current.leader_id)
        self.f.repo.request_cancel(self.scoped[0], self.scoped[1]['id'])
        self.assertFalse(current._bootstrap_start_allowed(intent_id, lease))
        self.f.repo.set_capacity_approval_enabled(current.config.capacity_approval_id, enabled=False)
        self.assertFalse(current._bootstrap_start_allowed(intent_id, lease))

    def test_upload_failure_preserves_original_backlog_and_financial_reservation(self):
        before = self.f.repo.get_job(self.scoped[0], self.scoped[1]['id'])
        budget = self.f.repo.get_budget('job-budget')
        self.upload.failure = True
        self.upload.finish(self.boot._preparation)
        status = self.f.tick()
        self.assertEqual(status['phase'], 'awaiting_repair')
        self.assertFalse(self.f.controller.stopping())
        self.f.assert_preserved(self.scoped, before, budget, self.deadline)
        self.assertEqual(len(self.f.provider.creates), 1)

    def test_rental_deadline_or_revocation_never_authorizes_setup(self):
        from sqlalchemy import update
        from studio_platform.repository import instance_intents
        current = self.f.controller.current
        intent_id = self.boot.intent['id']
        lease = current.scaler.acquire(current.config.pool, current.leader_id)
        self.f.repo.set_capacity_approval_enabled(current.config.capacity_approval_id, enabled=False)
        self.assertFalse(current._bootstrap_start_allowed(intent_id, lease))
        self.f.repo.set_capacity_approval_enabled(current.config.capacity_approval_id, enabled=True)
        with self.f.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == intent_id).values(
                hard_deadline=self.f.now+current.config.drain_margin_s-1))
        self.assertFalse(current._bootstrap_start_allowed(intent_id, lease))


if __name__ == '__main__':
    unittest.main()
