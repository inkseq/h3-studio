"""WanGP failure holds using compiled requests, SQLite and fake cloud/boot only."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import unittest

from sqlalchemy import select

from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.on_demand_scaler import OnDemandConfig, OnDemandController
from studio_platform.production_scaler import MODEL, RECIPE, save
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE, MULTIMODAL_INPUT_LIMITS
from studio_platform.queue import TaskQueue
from studio_platform.repository import Scope, attempts, capacity_waiters, request_hash
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_preparation_recovery import PreparationFailureBoot
from test_platform_production_scaler import configuration, FakeBoot, FakeProvider
from test_platform_repository import LedgerCase


DIGEST = "a" * 64


def identity(config, intent):
    return {"intent_id": intent["id"], "instance_id": intent["provider_instance_id"],
        "configuration_id": config.configuration_id, "sources": config.source_sha256,
        "backend": "wangp-worker", "engine_manifest_digest": DIGEST}


class WanGPQueuedBoot(FakeBoot):
    def __init__(self, repo, provider, config, intent, port, **kwargs):
        self.repo, self.config, self.intent, self.port = repo, config, intent, port
        self.drained = self.closed = False
        self.worker = "lium-" + intent["id"].replace("-", "")
        self.control = WorkerControl(repo)
        self.control.register(WorkerSpec(self.worker, config.pool, "lium", intent["provider_instance_id"],
            ("GPU-" + intent["id"],), (RECIPE,), MODEL, config.configuration_id, "wangp-worker", DIGEST))
        self.control.mark_ready(self.worker, upstream_idle_confirmed=True)
        directory = config.work_dir / "boot" / intent["id"]
        directory.mkdir(parents=True, exist_ok=True)
        save(directory / "bootstrap-state.json", {"identity": identity(config, intent),
            "phase": "fleet_started", "qualification_profile": QUEUED_TASK_PROFILE,
            "runtime_validation": {"profile": QUEUED_TASK_PROFILE, "state": "runtime_ready",
                "generation_verified": False}})

    def tick(self, *args, **kwargs):
        value = super().tick(*args, **kwargs)
        if self.control.get(self.worker)["drain_requested"] and not kwargs.get("stopping"):
            return {"state": "fleet_attention_required"}
        return value


class WanGPPreparationFailureBoot(PreparationFailureBoot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        save(self.config.work_dir / "boot" / self.intent["id"] / "bootstrap-state.json",
            {"phase": "bootstrap_failed", "identity": identity(self.config, self.intent)})


class WanGPServiceHoldTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.repo.engine.dispose)
        root = Path(self.temp.name)
        base = configuration(root, self.now)
        scale = {**base.scale_policy, "max_instances": 1, "max_physical_gpus": 1, "idle_before_drain_s": 600}
        self.config = OnDemandConfig(**{**asdict(base), "work_dir": root / "service",
            "execution_backend": "wangp-worker", "engine_manifest_digest": DIGEST,
            "qualification_profile": QUEUED_TASK_PROFILE, "allowed_owners": ["superdan", "supervan"],
            "scale_policy": scale, "launches": base.launches[:1], "manifests": base.manifests[:1], "max_cycles": 2,
            "source_sha256": {name: "b" * 64 for name in
                ("wangp-bootstrap.py", "wangp-manifest.json", "wangp-runtime.json", "wangp-package.tar.gz")}})
        value = policy(self.now)
        value.update(backend="wangp-worker", engine_manifest_digest=DIGEST, pool=self.config.pool,
            configuration_id=self.config.configuration_id, recipe_ids=[RECIPE], budget_accounts=["job-budget"])
        value["qualification"].update(status="runtime_required", profile=QUEUED_TASK_PROFILE,
            evidence_id=self.config.qualification_evidence_id, expires_at=self.now + 7000)
        value["reservation"].update(expected_runtime_s=1800, expires_at=self.now + 7000)
        value["envelope"].update(max_duration_seconds=6, max_reference_files=0, max_guides=0,
            allow_first_last=True, input_limits={**MULTIMODAL_INPUT_LIMITS,
                "max_images": 0, "max_videos": 0, "max_audios": 0,
                "guide_kinds": [], "guide_recipe_ids": [], "allow_video_audio": False})
        value["envelope"]["controls"] = {"sampler_name": ["euler"], "scheduler": ["auto"],
            "video_decode": ["tiled"], "audio_decode": ["normal"], "encoder_device": ["default"]}
        path = root / "policy.json"
        path.write_text(json.dumps(value)); path.chmod(0o600)
        self.config = replace(self.config, execution_policy_sha256=request_hash(value))
        self.settings = Settings(self.config.data_dir, database_url=self.url, auth_mode="password",
            public_origin="https://www.sixnine.art", generation_enabled=True, execution_backend="wangp-worker",
            execution_policy_file=path)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(self.config.pool, max_instances=1, max_physical_gpus=1)
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=6_000_000)
        self.repo.configure_budget("job-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        self.provider = FakeProvider(lambda: self.now)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=WanGPQueuedBoot)
        self.controller.initialize()

    def submit(self, owner, story):
        scope = Scope("sixnine", owner, story)
        request = generation_request()
        request["controls"] = {"duration": 5, "steps": 50, "resolution": "768P"}
        compiled, fingerprint = compile_request(request, lambda _: None, backend="wangp-worker")
        admission = ExecutionPolicies(self.settings, self.repo).evaluate(compiled, scope, fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        self.assertEqual(admission.execution["backend"], "wangp-worker")
        plan = self.repo.create_plan(scope, compiled, admission.execution, expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost)
        return scope, self.repo.create_job(scope, plan["id"], owner + story,
            initial_status=admission.execution["admission_state"],
            budget_account_ids=admission.execution["budget_account_ids"])

    def tick(self):
        self.now += 16
        return self.controller.tick()

    def wait_deadline(self, job):
        with self.repo.engine.connect() as conn:
            return conn.execute(select(capacity_waiters.c.deadline).where(
                capacity_waiters.c.job_id == job["id"])).scalar_one()

    def assert_preserved(self, scoped, before, budget, deadline):
        after = self.repo.get_job(scoped[0], scoped[1]["id"])
        for field in ("id", "request", "request_hash", "execution_plan", "plan_id", "idempotency_key", "attempt_no"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(self.repo.get_budget("job-budget"), budget)
        self.assertEqual(self.wait_deadline(scoped[1]), deadline)

    def test_failed_setup_preserves_accepted_backlog_in_repair_hold(self):
        self.controller.current.boot_factory = self.controller.boot_factory = WanGPPreparationFailureBoot
        scoped = self.submit("supervan", "accepted-before-setup-fails")
        before = self.repo.get_job(scoped[0], scoped[1]["id"])
        budget, deadline = self.repo.get_budget("job-budget"), self.wait_deadline(scoped[1])
        for _ in range(8):
            status = self.tick()
        self.assertEqual(status["phase"], "awaiting_repair")
        self.assertEqual(self.controller.current.preparation_hold()["reason"], "bootstrap_repair_required")
        self.assertFalse(self.controller.stopping())
        self.assert_preserved(scoped, before, budget, deadline)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.controller.sequence, 1)

    def start_two(self):
        first = self.submit("superdan", "first")
        self.now += 1
        second = self.submit("supervan", "second")
        for _ in range(3):
            self.tick()
        boot = next(iter(self.controller.current.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        self.assertEqual(claim.job["id"], first[1]["id"])
        return first, second, boot, claim

    def test_failed_first_task_preserves_second_task_and_repair_hold_across_restart(self):
        first, second, boot, claim = self.start_two()
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "original-wangp-operation")
        queue.fail(claim.lease, "upstream_generation_failed", actual_cost_microusd=None, upstream_stopped=True)
        boot.control.observe(boot.worker, first[1]["id"], quarantine_failures=True)
        before = self.repo.get_job(second[0], second[1]["id"])
        budget, deadline = self.repo.get_budget("job-budget"), self.wait_deadline(second[1])
        for _ in range(8):
            status = self.tick()
        self.assertEqual(status["phase"], "awaiting_repair")
        self.assertEqual(self.controller.current.preparation_hold()["reason"], "queued_task_repair_required")
        self.assert_preserved(second, before, budget, deadline)
        self.assertEqual(self.repo.get_job(first[0], first[1]["id"])["status"], "failed")
        recovered = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=WanGPQueuedBoot)
        recovered.leader_id = self.controller.leader_id
        recovered.initialize()
        self.assertEqual(recovered.tick()["phase"], "awaiting_repair")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.provider.destroys), 1)

    def test_changed_engine_receipt_and_unknown_submission_do_not_prove_failure(self):
        _, second, boot, claim = self.start_two()
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.mark_submission_unknown(claim.lease)
        boot.control.drain(boot.worker)
        current = self.controller.current
        self.assertFalse(current._queued_task_failure_hold(boot.intent))
        path = current.config.work_dir / "boot" / boot.intent["id"] / "bootstrap-state.json"
        evidence = json.loads(path.read_text())
        evidence["identity"]["engine_manifest_digest"] = "f" * 64
        save(path, evidence)
        self.assertFalse(current._queued_task_failure_hold(boot.intent, allow_unconfirmed=True))
        self.assertIsNone(current.preparation_hold())
        with self.repo.engine.connect() as conn:
            attempt = conn.execute(select(attempts).where(attempts.c.id == claim.lease.attempt_id)).mappings().one()
        self.assertEqual(attempt["upstream_stopped"], 0)
        self.assertEqual(self.repo.get_job(second[0], second[1]["id"])["status"], "queued")
        self.assertEqual(self.provider.destroys, [])


if __name__ == "__main__":
    unittest.main()
