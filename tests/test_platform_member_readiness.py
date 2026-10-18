"""Current member projections; fake provider and isolated ledger, no cloud calls."""
import unittest
from unittest.mock import patch

from sqlalchemy import update

from studio_platform.control import WorkerSpec
from studio_platform.member_readiness import project_member
from studio_platform.production_scaler import FiniteController, MODEL
from studio_platform.repository import capacity_approvals, jobs, registered_workers, request_hash
from studio_platform.scaler import ProviderFact
import test_platform_pool_service as pair


class MemberReadinessTests(unittest.TestCase):
    def setUp(self):
        self.h = pair.PoolServiceTests("runTest")
        self.h.setUp()

    def tearDown(self):
        self.h.tearDown()

    def start(self, *, slow_a=False):
        h = self.h
        if slow_a:
            original = h.provider.create
            def create(tag, launch, **kwargs):
                value = original(tag, launch, **kwargs)
                if len(h.provider.creates) == 1:
                    value = ProviderFact("starting", value.instance_id,
                        provider_status="PENDING", preparation_stage="provider_preparing")
                    h.provider.facts[tag] = value
                return value
            h.provider.create = create
        result = h.start()
        self.cycle = h.controller.current
        self.mapping = h.mapping()
        return result

    def worker(self, member="b"):
        boot = self.cycle.boots[self.mapping[member]]
        return boot, boot.control.get(boot.worker)

    def mutate_worker(self, member="b", **changes):
        _, worker = self.worker(member)
        with self.h.repo.transaction() as connection:
            connection.execute(update(registered_workers).where(registered_workers.c.id == worker["id"]).values(**changes))

    def status(self):
        return self.cycle.status()

    def members(self, value):
        return {m["member_id"]: m for m in value["member_readiness"]["members"]}

    def test_first_ready_sibling_is_visible_without_rewriting_starting_or_budgets(self):
        scope, job = self.start(slow_a=True)
        before = (self.h.repo.list_instance_intents(pool=self.h.config.pool),
            self.h.repo.get_budget("finite-budget"), self.h.repo.get_job(scope, job["id"]))
        status = self.status()
        self.assertEqual(status["reason"], "gpu_ready")
        self.assertEqual(self.members(status)["a"]["state"], "preparing")
        self.assertEqual(self.members(status)["b"]["state"], "ready")
        self.assertTrue(all(r["state"] == "starting" for r in status["instances"]))
        self.assertEqual(before, (self.h.repo.list_instance_intents(pool=self.h.config.pool),
            self.h.repo.get_budget("finite-budget"), self.h.repo.get_job(scope, job["id"])))
        self.assertEqual(len(self.h.provider.creates), 2)

    def test_busy_worker_with_real_claim_is_not_reported_as_still_starting(self):
        scope, job = self.start(slow_a=True)
        boot, _ = self.worker()
        claim = boot.control.claim(boot.worker, self.h.config.pool)
        self.assertEqual(claim.job["id"], job["id"])
        status = self.status()
        self.assertEqual(status["reason"], "gpu_busy")
        self.assertEqual(self.members(status)["b"]["current_job_id"], job["id"])
        self.assertEqual(self.members(status)["b"]["state"], "busy")
        self.assertEqual(self.h.repo.get_job(scope, job["id"])["status"], "claimed")

    def test_runtime_quarantine_does_not_hide_healthy_sibling(self):
        self.start()
        a = next(r for r in self.h.repo.list_instance_intents(pool=self.h.config.pool) if r["id"] == self.mapping["a"])
        self.cycle._boot_failure(a, {"state": "bootstrap_failed"})
        status = self.status()
        self.assertEqual(status["reason"], "gpu_ready")
        self.assertEqual(self.members(status)["a"]["state"], "held")
        self.assertEqual(self.members(status)["b"]["state"], "ready")
        self.assertEqual(status["member_holds"], [{"intent_id": a["id"], "state": "repair_required"}])

    def test_unknown_rental_stays_explicit_while_healthy_sibling_count_is_retained(self):
        self.start(slow_a=True)
        from studio_platform.repository import instance_intents
        with self.h.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id == self.mapping["a"])
                .values(state="creation_unknown"))
        status = self.status()
        self.assertEqual(status["reason"], "creation_needs_reconciliation")
        self.assertEqual(status["recovery"]["intent_ids"], [self.mapping["a"]])
        self.assertEqual(status["member_readiness"]["counts"]["ready"], 1)
        self.assertEqual(self.members(status)["a"]["state"], "unknown")

    def test_stale_worker_and_unbound_same_config_worker_cannot_advertise_ready(self):
        self.start(slow_a=True)
        boot, worker = self.worker()
        self.mutate_worker(expires_at=self.h.now)
        spec = dict(worker["spec"])
        spec.update(worker_id="unbound-worker", instance_id="unbound-pod", physical_gpu_ids=("GPU-unbound",),
            recipe_ids=tuple(spec["recipe_ids"]))
        self.h.repo.configure_capacity(max_instances=3, max_physical_gpus=3)
        boot.control.register(WorkerSpec(**spec))
        boot.control.mark_ready("unbound-worker", upstream_idle_confirmed=True)
        status = self.status()
        self.assertEqual(status["member_readiness"]["counts"]["ready"], 0)
        self.assertEqual(self.members(status)["b"]["reason"], "worker_heartbeat_unconfirmed")
        self.assertEqual(status["reason"], "worker_readiness_unconfirmed")

    def test_wrong_spec_fields_or_digest_fail_closed_even_with_fresh_ready_label(self):
        self.start(slow_a=True)
        _, original = self.worker()
        for field, wrong in (("backend", "wangp-worker"), ("model_id", "wrong"),
                ("configuration_id", "wrong"), ("recipe_ids", ["wrong"]),
                ("engine_manifest_digest", "f"*64), ("output_delivery", "native-frames-v1"),
                ("provider", "other"), ("worker_id", "other"), ("pool", "other"),
                ("instance_id", "other"), ("physical_gpu_ids", ["GPU-a", "GPU-b"])):
            with self.subTest(field=field):
                spec = {**original["spec"], field: wrong}
                self.mutate_worker(spec=spec, spec_hash=request_hash(spec))
                self.assertEqual(self.members(self.status())["b"]["reason"], "worker_identity_unconfirmed")
        self.mutate_worker(spec=original["spec"], spec_hash="0"*64)
        self.assertEqual(self.members(self.status())["b"]["reason"], "worker_identity_unconfirmed")

    def test_draining_deadline_and_registered_states_do_not_become_ready(self):
        self.start(slow_a=True)
        _, worker = self.worker()
        for values, expected in (({"drain_requested": 1}, "unavailable"),
                ({"state": "registered"}, "preparing"), ({"state": "retired"}, "unavailable"),
                ({"state": "busy"}, "unknown"), ({"updated_at": self.h.now+1}, "unknown")):
            self.mutate_worker(**{**{k: worker[k] for k in ("drain_requested", "state", "updated_at")}, **values})
            self.assertEqual(self.members(self.status())["b"]["state"], expected)
        row = next(r for r in self.h.repo.list_instance_intents(pool=self.h.config.pool) if r["id"] == self.mapping["b"])
        row["hard_deadline"] = self.h.now+self.cycle.config.drain_margin_s
        value = project_member(self.cycle.config, "b", row, worker, now=self.h.now, model_id=MODEL)
        self.assertEqual(value["reason"], "member_deadline_margin")

    def test_unresolved_or_dangling_job_cannot_be_presented_as_free_or_ordinary_busy(self):
        scope, job = self.start(slow_a=True)
        boot, _ = self.worker()
        boot.control.claim(boot.worker, self.h.config.pool)
        with self.h.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="submission_unknown"))
        self.assertEqual(self.members(self.status())["b"]["state"], "unknown")
        self.mutate_worker(state="ready", current_job_id=None)
        self.assertEqual(self.members(self.status())["b"]["reason"], "worker_attempt_unresolved")
        self.mutate_worker(state="busy", current_job_id="absent-job")
        self.assertEqual(self.members(self.status())["b"]["reason"], "worker_job_unconfirmed")
        self.assertEqual(self.h.repo.get_job(scope, job["id"])["status"], "submission_unknown")

    def test_ready_wait_feedback_is_scoped_and_does_not_claim_per_job_eligibility(self):
        self.start()
        scope, waiting = self.h.submit("supervan", "waiting-readiness")
        before = self.h.repo.get_job(scope, waiting["id"])
        with patch.object(self.cycle.cold, "advance_once", return_value=([], [])):
            status = self.h.tick()
        after = self.h.repo.get_job(scope, waiting["id"])
        self.assertEqual(status["reason"], "gpu_ready")
        self.assertEqual(after["status"], "waiting_capacity")
        self.assertEqual(after["error_code"], "capacity_matching_slot_pending")
        self.assertEqual(after["execution_plan"], before["execution_plan"])
        self.assertEqual(after["attempt_no"], 0)
        for state, code in (("waiting_capacity", "specific_operator_block"),
                ("running", "existing_running"), ("submission_unknown", "existing_unknown"),
                ("succeeded", "existing_terminal")):
            with self.h.repo.transaction() as connection:
                connection.execute(update(jobs).where(jobs.c.id == waiting["id"]).values(status=state, error_code=code))
            self.cycle._record_wait_reason("gpu_ready")
            unchanged = self.h.repo.get_job(scope, waiting["id"])
            self.assertEqual((unchanged["status"], unchanged["error_code"]), (state, code))

    def test_specific_budget_repair_ttl_and_revocation_are_not_hidden_by_ready_slots(self):
        self.start()
        rows, _ = self.cycle._managed()
        for reason in ("ledger_capacity_or_budget_limit", "queued_task_repair_required", "provider_ttl_unconfirmed"):
            self.assertEqual(self.cycle._wait_reason({"reason": reason}, rows, {}), reason)
        with self.h.repo.transaction() as connection:
            connection.execute(update(capacity_approvals).where(capacity_approvals.c.id == self.cycle.config.capacity_approval_id)
                .values(enabled=0))
        status = self.status()
        self.assertEqual(status["reason"], "capacity_approval_or_cycle_conflict")
        self.assertFalse(status["member_readiness"]["approval_available"])
        self.assertEqual(status["member_readiness"]["counts"]["ready"], 2)

    def test_current_replacement_binding_never_falls_back_to_old_ready_worker(self):
        self.start(slow_a=True)
        _, old_worker = self.worker()
        old = next(r for r in self.h.repo.list_instance_intents(pool=self.h.config.pool) if r["id"] == self.mapping["b"])
        replacement = {**old, "id": "00000000-0000-0000-0000-000000000099", "state": "creation_unknown"}
        result = project_member(self.cycle.config, "b", replacement, old_worker, now=self.h.now, model_id=MODEL)
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["worker_id"])

    def test_legacy_finite_reason_and_projection_are_unchanged(self):
        self.start()
        legacy = FiniteController(self.h.repo, self.h.settings, self.cycle.config, provider=self.h.provider)
        rows, _ = self.cycle._managed()
        self.assertEqual(legacy._wait_reason({}, rows, {}), "gpu_starting")
        value = {"reason": "gpu_starting", "instances": []}
        self.assertIs(legacy._project_status(value), value)
        self.assertNotIn("member_readiness", value)


if __name__ == "__main__":
    unittest.main()
