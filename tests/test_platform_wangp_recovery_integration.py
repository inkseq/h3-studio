"""Joined CS5/6/7 paths: real ledger/worker/media; fake Session/provider, no GPU."""

from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import subprocess
import unittest
from sqlalchemy import select
from studio_platform.repository import budget_reservations

from comfy_workflow import native_output_spec
from studio_platform.artifact_writer import ArtifactWriter
from studio_platform.autoscale import Demand, ScalePolicy
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.inference.outputs import NATIVE_DELIVERY, native_delivery_spec
from studio_platform.inference.wangp import WanGPBackend
from studio_platform.inference.wangp_contract import (
    PreparedRequest,
    RuntimeObservation,
    RuntimeOutput,
    canonical_json,
)
from studio_platform.queued_task_runner import QueuedTaskRunner, QUEUED_TASK_PROFILE
from studio_platform.runtime_hosts.wangp import WanGPHost
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from studio_platform.scaler import LaunchSpec, ProviderFact, ScaleCoordinator
from studio_platform.storage import LocalObjectStore
from test_platform_repository import LedgerCase
import test_platform_scaler as scaler_fixture
from test_platform_wangp_host import FakeSession
from test_platform_wangp_receipts import manifest


class Transport:
    """Only injected network faults; all receipt and output logic is real."""

    def __init__(self, host):
        self.host = host
        self.fail_audio_once = False
        self.reads = []

    def __getattr__(self, name):
        return getattr(self.host, name)

    def read_artifact(self, operation, kind):
        self.reads.append((operation, kind))
        for chunk in self.host.read_artifact(operation, kind):
            yield chunk
            if kind == "audio" and self.fail_audio_once:
                self.fail_audio_once = False
                raise OSError("synthetic interrupted audio response")


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU media tools required"
)
class WanGPRecoveryIntegrationTests(LedgerCase):
    # Reuse helper methods, not an inherited suite or a replacement collector.
    tick = scaler_fixture.ScalerTests.tick
    create = scaler_fixture.ScalerTests.create

    def setUp(self):
        super().setUp()
        self.provider = scaler_fixture.FakeProvider()
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.policy = ScalePolicy(
            dry_run=False,
            max_instances=1,
            max_physical_gpus=1,
            cold_start_s=30,
            min_improvement_s=1,
            cooldown_s=0,
            queue_target_s=60,
            approved_remaining_microusd=10_000_000,
            instance_reservation_microusd=100_000,
            hard_deadline=5000,
        )
        self.demands = [
            Demand("synthetic-demand-" + str(i), "superdan", 900, 120)
            for i in range(12)
        ]
        self.repo.configure_pool("scale-test", max_instances=1, max_physical_gpus=1)
        self.root = Path(self.temp.name)
        self.engine = manifest()
        self.launch = LaunchSpec(
            "test-only", "joined-config", "MiniMax-H3-Base-BF16", region="local-test"
        )
        self.instance = self.create()
        self.repo.update_instance(self.instance["id"], "ready")
        self.control = WorkerControl(self.repo)
        self.spec = WorkerSpec(
            "joined-worker",
            "scale-test",
            "test-only",
            self.instance["provider_instance_id"],
            ("joined-gpu",),
            ("h3-base-fl2va-v1",),
            self.launch.model_id,
            self.launch.configuration_id,
            "wangp-worker",
            self.engine.digest,
            output_delivery=NATIVE_DELIVERY,
        )
        self.control.register(self.spec)
        self.control.mark_ready(self.spec.worker_id, upstream_idle_confirmed=True)
        self.output = self.root / "runtime-output"
        self.output.mkdir()
        self.journal_path = self.root / "receipts.sqlite"
        self.journal = ReceiptJournal(
            self.journal_path,
            slot_key="joined-slot",
            manifest_digest=self.engine.digest,
            create=True,
        )
        self.session = FakeSession()
        self.host = self.new_host(self.journal, self.session)
        self.transport = Transport(self.host)
        self.backend = WanGPBackend(
            enabled=True,
            slot_key="joined-slot",
            manifest=self.engine,
            transport=self.transport,
            compiler=self.compile,
            expected_incarnation=self.host.readiness().incarnation,
        )
        self.store = LocalObjectStore(self.root / "objects")
        self.identity = {
            "intent_id": self.instance["id"],
            "instance_id": self.instance["provider_instance_id"],
            "configuration_id": self.launch.configuration_id,
            "qualification_profile": QUEUED_TASK_PROFILE,
            "sources": {"model_manifest.json": "a" * 64},
            "backend": "wangp-worker",
            "engine_manifest_digest": self.engine.digest,
            "output_delivery": NATIVE_DELIVERY,
        }
        self.runner = self.new_runner()
        self.original = self.new_job("original")
        self.initial_budget = self.repo.get_budget("owner-budget")

    def new_host(self, journal, session):
        host = WanGPHost(
            session=session,
            journal=journal,
            manifest=self.engine,
            output_root=self.output,
            sealed_root=self.root / "sealed",
        )
        self.addCleanup(host.close)
        if not hasattr(self, "hosts"):
            self.hosts = []
        self.hosts.append(host)
        return host

    def tearDown(self):
        # Windows cannot remove the test directory while the journal lock FD is open.
        for host in reversed(getattr(self, "hosts", [])):
            host.close()
        super().tearDown()

    def compile(self, job, tag, store, heartbeat):
        heartbeat()
        return PreparedRequest(
            job["id"],
            tag,
            job["request_hash"],
            self.engine.digest,
            canonical_json({"steps": 50}),
            canonical_json(job["request"]["output_spec"]),
            True,
        )

    def new_runner(self):
        return QueuedTaskRunner(
            self.repo,
            self.store,
            self.root / "worker",
            backend=self.backend,
            control=self.control,
            retry_after_s=0,
            submission_guard=lambda _: True,
            stop_new=lambda: False,
            job_allowed=lambda _: True,
            collection_lock_dir=self.root / "collection-lock",
            qualification_evidence_file=self.root
            / "boot"
            / "queued-task-evidence.json",
            evidence_identity=self.identity,
        )

    def new_job(self, key):
        request = {
            "model": "MiniMax-H3-Base-BF16",
            "prompt": "synthetic offline fixture",
            "duration": 5,
            "resolution": "custom",
            "width": 256,
            "height": 256,
            "steps": 50,
            "generate_audio": True,
            "export_crf": 18,
        }
        compiled = {
            "recipe_id": "h3-base-fl2va-v1",
            "request": request,
            "output_spec": native_output_spec(request),
            "assets": {},
        }
        execution = {
            "pool": self.spec.pool,
            "backend": "wangp-worker",
            "enabled": True,
            "configuration_id": self.spec.configuration_id,
            "engine_manifest_digest": self.engine.digest,
            "expected_runtime_s": 120,
            "output_delivery": NATIVE_DELIVERY,
            "delivery_spec": native_delivery_spec(compiled),
        }
        plan = self.repo.create_plan(
            self.scope,
            compiled,
            execution,
            expires_at=self.now + 3600,
            estimated_cost_microusd=100_000,
        )
        return self.repo.create_job(
            self.scope, plan["id"], key, budget_account_ids=("owner-budget",)
        )

    def run_worker(self):
        return self.runner.run_once(self.spec.worker_id, self.spec.pool)

    def start(self):
        self.assertEqual(self.run_worker()["state"], "running")
        self.started = self.repo.get_job(self.scope, self.original["id"])
        self.attempt = self.started["current_attempt_id"]
        self.assertEqual(self.session.calls, 1)

    def assert_identity(self):
        job = self.repo.get_job(self.scope, self.original["id"])
        for key in ("id", "request", "request_hash", "execution_plan"):
            self.assertEqual(job[key], self.original[key])
        self.assertEqual(job["current_attempt_id"], self.attempt)
        self.assertEqual(job["attempt_no"], 1)
        self.assertEqual(self.session.calls, 1)
        self.assertEqual(len(self.provider.creates), 1)
        return job

    def try_destroy(self):
        # Adversarial provider idle fact cannot override unresolved ledger work.
        self.provider.facts[self.instance["id"]] = ProviderFact(
            "running",
            self.instance["provider_instance_id"],
            idle_confirmed=True,
            idle_since=0,
        )
        self.now += 16
        return self.tick(demands=[], policy=replace(self.policy, idle_before_drain_s=0))

    def assert_held(self):
        job = self.assert_identity()
        self.try_destroy()
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(
            self.control.get(self.spec.worker_id)["current_job_id"], job["id"]
        )
        self.assertEqual(self.control.capacity(), {"instances": 1, "physical_gpus": 1})
        self.assertIsNone(
            self.control.claim(self.spec.worker_id, self.spec.pool, purpose="generate")
        )
        self.assertEqual(self.repo.get_budget("owner-budget"), self.initial_budget)
        self.assertEqual(self.repo.list_artifacts(self.scope, job["id"]), [])
        return job

    def complete_runtime(self, *, include_audio=True):
        video, audio = self.output / "fixture.mp4", self.output / "fixture.wav"
        if not video.exists():
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=navy:s=256x256:r=24",
                    "-frames:v",
                    "124",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        if not audio.exists():
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=32000",
                    "-t",
                    str(124 / 24),
                    "-ac",
                    "2",
                    str(audio),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        outputs = {"video": RuntimeOutput(video, "video/mp4")}
        if include_audio:
            outputs["audio"] = RuntimeOutput(audio, "audio/wav")
        self.session.handle.observation = RuntimeObservation("succeeded", True, outputs)

    def assert_complete_then_destroy(self):
        job = self.assert_identity()
        self.assertEqual(job["status"], "succeeded", job["error_code"])
        writer = ArtifactWriter(
            self.repo.engine,
            self.store,
            self.root / "worker",
            tenant=self.scope.tenant_id,
        )
        receipt = writer.get(job, self.attempt, self.attempt)
        self.assertEqual(receipt["phase"], "settled")
        self.assertEqual(set(receipt["roles"]), {"video", "audio"})
        artifacts = self.repo.list_artifacts(self.scope, job["id"])
        self.assertEqual({a["metadata"]["kind"] for a in artifacts}, {"video", "audio"})
        before = {}
        for artifact in artifacts:
            evidence = artifact["metadata"]
            self.assertTrue(evidence["validated"])
            with self.store.open(evidence["object_key"]) as source:
                before[evidence["object_key"]] = hashlib.sha256(
                    source.read()
                ).hexdigest()
            self.assertEqual(before[evidence["object_key"]], evidence["sha256"])
        self.try_destroy()
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(
            self.provider.destroys[0],
            (self.instance["id"], self.instance["provider_instance_id"]),
        )
        # Destroyed runtime outputs are no longer the business artifact source.
        for path in self.output.iterdir():
            path.unlink()
        for key, digest in before.items():
            with self.store.open(key) as source:
                self.assertEqual(hashlib.sha256(source.read()).hexdigest(), digest)
        # Terminal job admission is released; its invoice stays unknown and the
        # rental reservation remains held until its separate physical closure.
        self.assertEqual(
            self.repo.get_budget("owner-budget"),
            {
                **self.initial_budget,
                "reserved_microusd": self.initial_budget["reserved_microusd"] - 100_000,
            },
        )
        with self.repo.engine.connect() as conn:
            rows = list(conn.execute(select(budget_reservations)).mappings())
        job_invoice = next(row for row in rows if row["reference_type"] == "job")
        instance_hold = next(row for row in rows if row["reference_type"] == "instance")
        self.assertEqual(
            (
                job_invoice["state"],
                job_invoice["amount_microusd"],
                job_invoice["actual_cost_microusd"],
            ),
            ("invoice_pending", 100_000, None),
        )
        self.assertEqual(
            (
                instance_hold["state"],
                instance_hold["amount_microusd"],
                instance_hold["actual_cost_microusd"],
            ),
            ("reserved", 100_000, None),
        )

    def damaged_journal(self, *, replacement):
        self.start()
        self.journal_path.rename(self.root / "retained-original.sqlite")
        if replacement:
            self.journal_path.write_bytes(b"not the original receipt journal")
        self.runner = self.new_runner()
        self.now += 31
        self.assertEqual(self.run_worker()["state"], "running")
        self.assertFalse(self.backend.is_idle())
        job = self.assert_held()
        self.assertEqual(job["error_code"], "upstream_status_unknown")
        self.assertTrue((self.root / "retained-original.sqlite").exists())

    def test_missing_journal_keeps_original_attempt_slot_money_and_provider(self):
        self.damaged_journal(replacement=False)

    def test_replaced_journal_keeps_original_attempt_slot_money_and_provider(self):
        self.damaged_journal(replacement=True)

    def test_changed_host_incarnation_cannot_replay_or_release_original(self):
        self.start()
        self.host.close()
        replacement_session = FakeSession()
        reopened = ReceiptJournal(
            self.journal_path,
            slot_key="joined-slot",
            manifest_digest=self.engine.digest,
        )
        self.transport.host = self.new_host(reopened, replacement_session)
        self.runner = self.new_runner()
        self.now += 31
        self.assertEqual(self.run_worker()["state"], "running")
        self.assertFalse(self.backend.is_idle())
        self.assert_held()
        self.assertEqual(replacement_session.calls, 0)

    def test_cancel_ack_holds_then_late_success_collects_original_before_destroy(self):
        self.start()
        self.repo.request_cancel(self.scope, self.original["id"])
        self.assertEqual(self.run_worker()["state"], "cancel_requested")
        self.assertGreaterEqual(self.session.handle.cancel_calls, 1)
        self.assertFalse(self.backend.is_idle())
        self.assert_held()
        self.complete_runtime()
        self.runner = self.new_runner()
        self.now += 31
        self.assertEqual(self.run_worker()["state"], "succeeded")
        self.assertTrue(
            self.repo.get_job(self.scope, self.original["id"])["result"][
                "completed_after_cancel_request"
            ]
        )
        self.assert_complete_then_destroy()

    def test_missing_required_audio_holds_then_collects_same_runtime_result(self):
        self.start()
        self.complete_runtime(include_audio=False)
        self.assertEqual(self.run_worker()["state"], "running")
        self.assertFalse(self.backend.is_idle())
        self.assert_held()
        self.complete_runtime()
        self.now += 31
        self.assertEqual(self.run_worker()["state"], "succeeded")
        self.assert_complete_then_destroy()

    def test_interrupted_audio_collection_resumes_same_sealed_outputs_before_destroy(
        self,
    ):
        self.start()
        self.complete_runtime()
        self.transport.fail_audio_once = True
        self.assertEqual(self.run_worker()["state"], "collecting")
        self.assertTrue(
            self.backend.is_idle()
        )  # Runtime stop is not business collection completion.
        self.assert_held()
        self.runner = self.new_runner()
        self.now += 31
        self.assertEqual(self.run_worker()["state"], "succeeded")
        self.assertEqual(sum(kind == "video" for _, kind in self.transport.reads), 1)
        self.assertEqual(sum(kind == "audio" for _, kind in self.transport.reads), 2)
        self.assertEqual(len({operation for operation, _ in self.transport.reads}), 1)
        self.assert_complete_then_destroy()


if __name__ == "__main__":
    unittest.main()
