"""Engine routing with temporary SQL ledgers and fake adapters only."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import uuid
import sys
import types
from unittest.mock import patch

from studio_platform.control import WorkerControl, WorkerSpec, worker_spec_payload
from studio_platform.execution_policy import ExecutionPolicies, validate_policy
from studio_platform.fleet import FleetConfig, SlotConfig, read_config, run_slot
from studio_platform.inference.protocol import Outcome
from studio_platform.queue import TaskQueue
from studio_platform.repository import BudgetExceeded, Conflict, NotFound, request_hash
from studio_platform.settings import Settings
from test_platform_repository import LedgerCase
from test_platform_execution_policy import policy, synthetic_input_limits


MANIFEST = "a" * 64
REVISION = "b" * 40
MODEL = "MiniMax-H3-Base-BF16"
RECIPE = "h3-base-fl2va-v1"


class EngineRoutingTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.root = Path(self.temp.name)

    def spec(self, backend="wangp-worker", worker="new", **changes):
        return WorkerSpec(worker, "engines", "test-only", "instance-" + worker,
            ("gpu-" + worker,), (RECIPE,), MODEL, "config-" + backend, backend,
            **({"engine_manifest_digest": MANIFEST} if backend == "wangp-worker" else {}), **changes)

    def make_job(self, backend="wangp-worker", digest=MANIFEST):
        execution = {"pool": "engines", "backend": backend, "enabled": True,
            "configuration_id": "config-" + backend}
        if backend == "wangp-worker":
            execution["engine_manifest_digest"] = digest
        plan = self.repo.create_plan(self.scope, {"recipe_id": RECIPE, "request": {"model": MODEL}},
            execution, expires_at=self.now + 1000)
        return self.repo.create_job(self.scope, plan["id"], uuid.uuid4().hex)

    def ready(self, spec):
        self.control.register(spec)
        self.control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)

    def slot(self, spec, **kw):
        endpoint = "http://127.0.0.1:18000"
        return SlotConfig(spec, enabled=True, endpoint=endpoint, allowed_origins=(endpoint,),
            comfy_revision=REVISION if spec.backend == "comfy-worker" else "",
            runtime_config_file=str(self.root / "runtime.json") if spec.backend == "wangp-worker" else "", **kw)

    def config(self, *slots):
        return FleetConfig(self.root / "fleet", slots, enabled=True, max_children=len(slots))

    def test_legacy_worker_hash_and_fleet_fingerprint_remain_identical(self):
        spec = self.spec("comfy-worker", "old")
        old = asdict(spec)
        old.pop("engine_manifest_digest")
        old.pop("output_delivery")
        old.pop("dispatch_backend")
        worker = self.control.register(spec)
        self.assertEqual(worker["spec"], json.loads(json.dumps(old)))
        self.assertEqual(worker["spec_hash"], request_hash(old))
        fleet = self.config(self.slot(spec))
        old_fleet = asdict(fleet)
        old_fleet["work_dir"] = str(fleet.work_dir)
        old_fleet["slots"][0]["spec"].pop("engine_manifest_digest")
        old_fleet["slots"][0]["spec"].pop("output_delivery")
        old_fleet["slots"][0]["spec"].pop("dispatch_backend")
        old_fleet["slots"][0].pop("recovery_only")
        old_fleet["slots"][0].pop("runtime_config_file")
        self.assertEqual(fleet.fingerprint(), request_hash(old_fleet))

    def test_wangp_requires_manifest_and_cannot_bypass_global_gpu_limits(self):
        with self.assertRaisesRegex(ValueError, "explicit_engine_manifest_required"):
            replace(self.spec(), engine_manifest_digest="")
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.control.register(self.spec("comfy-worker", "old"))
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            self.control.register(self.spec())
        self.assertEqual(self.control.capacity(), {"instances": 1, "physical_gpus": 1})

    def test_mixed_queue_matches_frozen_backend_and_manifest(self):
        old = self.make_job("comfy-worker")
        wrong = self.make_job(digest="c" * 64)
        wanted = self.make_job()
        self.ready(self.spec())
        status = self.control.pool_status("engines", model_id=MODEL, configuration_id="config-wangp-worker",
            backend="wangp-worker", recipe_id=RECIPE, engine_manifest_digest="c" * 64)
        self.assertEqual(status["matched_slots"], 0)
        claimed = self.control.claim("new", "engines")
        self.assertEqual(claimed.job["id"], wanted["id"])
        self.assertEqual(claimed.job["execution_plan"]["engine_manifest_digest"], MANIFEST)
        for job in (old, wrong):
            self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")

    def test_wangp_drain_repreflight_guard_is_not_comfy_only(self):
        spec = self.spec()
        self.ready(spec)
        self.repo.configure_pool("engines", max_instances=1, max_physical_gpus=1)
        intent = self.repo.reserve_instance_intent(self.scope, "engines", "shutdown", provider="test-only",
            physical_gpus=1, reserved_cost_microusd=100_000, hard_deadline=self.now + 2000,
            budget_account_ids=("owner-budget",), dry_run=False)
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(intent["id"], "starting", provider_instance_id=spec.instance_id)
        self.repo.update_instance(intent["id"], "draining")
        with self.assertRaisesRegex(Conflict, "capacity_drain_repreflight"):
            self.make_job()

    def test_policy_manifest_and_backend_match_without_mutating_legacy_policy(self):
        original = policy(self.now)
        original_hash = request_hash(original)
        self.assertIs(validate_policy(original), original)
        self.assertEqual(request_hash(original), original_hash)
        value = copy.deepcopy(original)
        value.update(backend="wangp-worker", engine_manifest_digest=MANIFEST,
            pool="engines", configuration_id="config-wangp-worker", recipe_ids=[RECIPE])
        value["envelope"]["controls"].pop("ref_image_size")
        value["envelope"]["controls"]["sampler_name"] = ["euler"]
        value["envelope"]["input_limits"] = synthetic_input_limits()
        value["envelope"]["input_limits"].update(guide_kinds=[], guide_recipe_ids=[],
            max_images=0, max_videos=0, max_audios=0)
        self.assertIs(validate_policy(value), value)
        with self.assertRaises(ValueError):
            validate_policy({**original, "engine_manifest_digest": MANIFEST})
        with self.assertRaises(ValueError):
            validate_policy({k: v for k, v in value.items() if k != "engine_manifest_digest"})
        path = self.root / "policy.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        settings = Settings(self.root, generation_enabled=True, execution_backend="wangp-worker", execution_policy_file=path)
        self.ready(self.spec())
        for ident in ("test-tenant:test-tenant", "test-owner:test-tenant:superdan"):
            self.repo.configure_budget(ident, tenant_id=self.scope.tenant_id, limit_microusd=1000000)
        from studio_platform.capabilities import compile_request
        from test_platform_api import generation_request
        request = generation_request()
        request["controls"]["seed"] = "424242"
        compiled, fingerprint = compile_request(request, lambda _: None, backend="wangp-worker")
        self.assertNotIn("ref_image_size", compiled["request"])
        self.assertNotIn("guides", compiled["request"])
        evaluator = ExecutionPolicies(settings, self.repo)
        admission = evaluator.evaluate(compiled, self.scope, fingerprint)
        self.assertTrue(admission.execution["enabled"])
        self.assertEqual(admission.execution["engine_manifest_digest"], MANIFEST)
        plan = self.repo.create_plan(self.scope, compiled, admission.execution,
            expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
        job = self.repo.create_job(self.scope, plan["id"], "frozen-engine",
            budget_account_ids=admission.execution["budget_account_ids"])
        self.assertTrue(evaluator.activation_allowed(job))
        tampered = copy.deepcopy(job)
        tampered["execution_plan"]["engine_manifest_digest"] = "d" * 64
        self.assertFalse(evaluator.activation_allowed(tampered))
        mismatch = ExecutionPolicies(replace(settings, execution_backend="comfy-worker"), self.repo)
        self.assertFalse(mismatch.evaluate(compiled, self.scope, fingerprint).execution["enabled"])
        self.assertFalse(mismatch.activation_allowed(job))

    def test_v2_explicit_manifest_and_recovery_config_never_fallback(self):
        slot = self.slot(self.spec())
        fleet = self.config(slot)
        path = self.root / "fleet.json"
        item = {**worker_spec_payload(slot.spec), "enabled": True, "endpoint": slot.endpoint,
            "allowed_origins": slot.allowed_origins, "comfy_revision": "", "confirmed_idle": False,
            "runtime_config_file": slot.runtime_config_file}
        value = {"version": 1, "work_dir": str(fleet.work_dir), "enabled": True, "max_children": 1,
            "shutdown_grace_s": 210, "slots": [item]}
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "requires_fleet_v2"):
            read_config(path)
        value["version"] = 2
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(read_config(path).fingerprint(), fleet.fingerprint())
        settings = Settings(self.root, execution_backend="wangp-worker", generation_enabled=True)
        fake = types.ModuleType("studio_platform.inference.wangp_factory")
        def unavailable(slot, directory):
            raise ValueError("wangp_runtime_configuration_unavailable")
        fake.create_backend = unavailable
        with patch.dict(sys.modules, {fake.__name__: fake}), \
                patch("studio_platform.fleet.ComfyBackend", side_effect=AssertionError("no Comfy fallback")):
            with self.assertRaisesRegex(ValueError, "wangp_runtime_configuration_unavailable"):
                run_slot(fleet, "new", settings, repository=self.repo, once=True)
        with self.assertRaisesRegex(ValueError, "duplicate_fleet_endpoint"):
            self.config(slot, self.slot(self.spec("comfy-worker", "old")))

    def test_explicit_recovery_keeps_old_attempt_when_new_default_is_wangp(self):
        spec = self.spec("comfy-worker", "old")
        self.ready(spec)
        original = self.make_job("comfy-worker")
        claimed = self.control.claim("old", "engines")
        queue = TaskQueue(self.repo)
        queue.begin_submission(claimed.lease)
        queue.mark_submission_unknown(claimed.lease)
        self.control.observe("old", original["id"])
        next_job = self.make_job("comfy-worker")
        calls = []

        class RecoveryBackend:
            kind, enabled, slot_key = "comfy-worker", True, "test-recovery-slot"
            def is_idle(inner):
                raise AssertionError("never revive from readiness")
            def prepare(inner, *args):
                raise AssertionError("never prepare new work")
            def submit(inner, *args):
                raise AssertionError("never submit")
            def reconcile(inner, tag, task_id=None):
                calls.append((tag, task_id))
                return Outcome("running", "original-upstream-task")
            def poll(inner, tag, task_id):
                return Outcome("running", task_id)

        fleet = self.config(self.slot(spec, recovery_only=True))
        settings = Settings(self.root, execution_backend="wangp-worker", generation_enabled=True,
            recovery_backends=("comfy-worker",))
        before_spec = self.control.get("old")["spec_hash"]
        self.repo.configure_capacity()  # No new GPU/registration admission remains.
        outcome = run_slot(fleet, "old", settings, repository=self.repo, store_factory=lambda _: object(),
            backend_factory=lambda *_: RecoveryBackend(), once=True)
        self.assertEqual(outcome["job_id"], original["id"])
        recovered = self.repo.get_job(self.scope, original["id"])
        self.assertEqual(recovered["current_attempt_id"], claimed.lease.attempt_id)
        self.assertEqual(recovered["attempt_no"], 1)
        self.assertEqual(recovered["request_hash"], original["request_hash"])
        self.assertEqual(calls, [(claimed.lease.attempt_id, None)])
        self.assertEqual(self.repo.get_job(self.scope, next_job["id"])["status"], "queued")
        self.assertEqual(self.control.get("old")["spec_hash"], before_spec)
        self.assertTrue(self.control.get("old")["drain_requested"])
        with self.assertRaisesRegex(ValueError, "not_explicitly_allowed"):
            run_slot(fleet, "old", replace(settings, recovery_backends=()), repository=self.repo, once=True)

    def test_recovery_refuses_new_registration_or_changed_engine_binding(self):
        with self.assertRaises(NotFound):
            self.control.require_recovery_binding(self.spec("comfy-worker", "missing"))
        self.ready(self.spec("comfy-worker", "old"))
        with self.assertRaisesRegex(Conflict, "recovery_worker_binding_required"):
            self.control.require_recovery_binding(self.spec("comfy-worker", "old"))
