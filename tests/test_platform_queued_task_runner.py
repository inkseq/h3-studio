"""Offline real queue-state tests with a fake backend; no model or cloud calls."""

import json
from pathlib import Path
from unittest.mock import patch
import uuid

from sqlalchemy import select, update

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.queued_task_runner import (
    QueuedTaskRunner,
    read_verification_summary,
    QUEUED_TASK_PROFILE,
)
from studio_platform.repository import budget_reservations, jobs
from studio_platform.worker import (
    BackendError,
    NotReady,
    Outcome,
    SubmissionRejected,
    WorkerRunner,
)
from test_platform_repository import LedgerCase


class Backend:
    enabled, kind, slot_key = True, "comfy-worker", "synthetic-queued-slot"

    def __init__(self):
        self.submits = 0
        self.state = "running"
        self.prepare_failure = False
        self.uncertain = False
        self.reject = False
        self.cancelled = False

    def prepare(self, job, tag, store, heartbeat):
        heartbeat()
        if self.prepare_failure:
            raise NotReady("synthetic preparation failure")
        return {}

    def submit(self, prepared, tag):
        self.submits += 1
        if self.uncertain:
            raise TimeoutError("synthetic ambiguous POST")
        if self.reject:
            raise SubmissionRejected("synthetic rejected POST")
        return "synthetic-upstream"

    def reconcile(self, tag):
        return (
            Outcome("unknown")
            if self.uncertain
            else Outcome(self.state, "synthetic-upstream")
        )

    def poll(self, tag, task):
        return Outcome("cancelled" if self.cancelled else self.state, task)

    def cancel(self, tag, task):
        self.cancelled = True


def verified_collect(runner, job, lease, tag, task, heartbeat):
    """Stand-in collector writes validated artifact evidence to the real ledger."""
    heartbeat()
    completed = runner.queue.complete(
        lease,
        [
            {
                "kind": "video",
                "validated": True,
                "object_key": "owners/superdan/assets/synthetic/PRIVATE-PATH.mp4",
                "size_bytes": 17,
                "sha256": "f" * 64,
                "width": 832,
                "height": 480,
                "duration_s": 5,
                "fps": 24,
                "has_audio": False,
            }
        ],
        actual_cost_microusd=None,
    )
    return runner._summary(completed)


class QueuedTaskRunnerTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.work = Path(self.temp.name) / "worker"
        self.evidence_file = Path(self.temp.name) / "boot" / "queued-task-evidence.json"
        self.identity = {
            "intent_id": "synthetic-intent",
            "instance_id": "synthetic-instance",
            "configuration_id": "synthetic-config",
            "qualification_profile": QUEUED_TASK_PROFILE,
            "sources": {
                "bootstrap_cloud.py": "a" * 64,
                "model_manifest.json": "b" * 64,
            },
        }
        self.backend = Backend()
        self.control = WorkerControl(self.repo)
        self.control.register(
            WorkerSpec(
                "worker",
                "finite-pool",
                "synthetic-provider",
                "synthetic-instance",
                ("synthetic-device",),
                ("synthetic-fl", "synthetic-ref"),
                "synthetic-model",
                "synthetic-config",
            )
        )
        self.control.mark_ready("worker", upstream_idle_confirmed=True)

    def job_for(self, *, recipe="synthetic-fl", steps=4):
        request = {
            "recipe_id": recipe,
            "request": {
                "model": "synthetic-model",
                "prompt": "PRIVATE-PROMPT",
                "mode": "fl" if recipe.endswith("fl") else "ref",
                "duration": 5,
                "resolution": "480P",
                "steps": steps,
                "generate_audio": False,
            },
            "assets": {
                "private-asset": {
                    "metadata": {"kind": "image"},
                    "model": {"key": "PRIVATE-ASSET-KEY"},
                }
            },
        }
        plan = self.repo.create_plan(
            self.scope,
            request,
            {
                "pool": "finite-pool",
                "backend": "comfy-worker",
                "enabled": True,
                "configuration_id": "synthetic-config",
                "expected_runtime_s": 10,
            },
            expires_at=self.now + 3600,
            estimated_cost_microusd=100_000,
        )
        job = self.repo.create_job(
            self.scope,
            plan["id"],
            uuid.uuid4().hex,
            budget_account_ids=("owner-budget",),
        )
        self.now += 0.1
        return job

    def runner(self):
        return QueuedTaskRunner(
            self.repo,
            None,
            self.work,
            backend=self.backend,
            control=self.control,
            retry_after_s=0,
            submission_guard=lambda _: True,
            stop_new=lambda: False,
            job_allowed=lambda _: True,
            collection_lock_dir=Path(self.temp.name) / "collection",
            qualification_evidence_file=self.evidence_file,
            evidence_identity=self.identity,
        )

    def test_first_real_job_is_result_and_exact_scope_proof_one_submission(self):
        job = self.job_for()
        original_hash, original_request = job["request_hash"], job["request"]
        self.backend.state = "succeeded"
        runner = self.runner()
        with patch.object(WorkerRunner, "_collect", verified_collect):
            result = runner.run_once("worker", "finite-pool")
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(
            (
                result["job_id"],
                result["state"],
                current["attempt_no"],
                self.backend.submits,
            ),
            (job["id"], "succeeded", 1, 1),
        )
        self.assertEqual(
            (current["request_hash"], current["request"]),
            (original_hash, original_request),
        )
        self.assertEqual(current["result"]["billing_status"], "pending")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        with self.repo.engine.connect() as conn:
            invoice = (
                conn.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_type == "job",
                        budget_reservations.c.reference_id == job["id"],
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(
            (
                invoice["state"],
                invoice["amount_microusd"],
                invoice["actual_cost_microusd"],
            ),
            ("invoice_pending", 100_000, None),
        )
        proof = runner.verification_summary()
        self.assertTrue(proof["generation_verified"])
        self.assertEqual(proof["verified_job_scopes"][0]["recipe_id"], "synthetic-fl")
        self.assertEqual(
            proof["verified_job_scopes"][0]["attempt_id"], current["current_attempt_id"]
        )
        self.assertEqual(proof["verified_job_scopes"][0]["request_hash"], original_hash)
        self.assertEqual(
            proof["verified_job_scopes"][0]["reference_kind_counts"],
            {"image": 1, "video": 0, "audio": 0},
        )
        text = self.evidence_file.read_text(encoding="utf-8")
        for private in (
            "PRIVATE-PROMPT",
            "PRIVATE-PATH",
            "PRIVATE-ASSET-KEY",
            "object_key",
            "upstream_task_id",
        ):
            self.assertNotIn(private, text)
        self.assertEqual(self.control.get("worker")["state"], "ready")

    def test_confirmed_generation_failure_atomically_quarantines_and_preserves_backlog(
        self,
    ):
        first, waiting = self.job_for(), self.job_for()
        self.backend.state = "failed"
        runner = self.runner()
        result = runner.run_once("worker", "finite-pool")
        self.assertEqual((result["job_id"], result["state"]), (first["id"], "failed"))
        failed = self.repo.get_job(self.scope, first["id"])
        self.assertEqual(failed["error_code"], "upstream_generation_failed")
        self.assertEqual(
            (
                self.control.get("worker")["state"],
                self.control.get("worker")["drain_requested"],
            ),
            ("draining", 1),
        )
        self.assertIsNone(self.control.get("worker")["current_job_id"])
        self.assertFalse(runner.verification_summary()["generation_verified"])
        self.assertTrue(runner.verification_summary()["runtime_quarantined"])
        self.assertEqual(runner.run_once("worker", "finite-pool")["state"], "idle")
        self.assertEqual(self.repo.get_job(self.scope, waiting["id"])["attempt_no"], 0)
        self.assertEqual(self.backend.submits, 1)

    def test_unknown_post_only_reconciles_original_attempt_and_can_return_success(self):
        first, waiting = self.job_for(), self.job_for()
        runner = self.runner()
        self.backend.uncertain = True
        self.assertEqual(
            runner.run_once("worker", "finite-pool")["state"], "submission_unknown"
        )
        self.now += 31
        self.assertEqual(
            runner.run_once("worker", "finite-pool")["state"], "submission_unknown"
        )
        self.assertFalse(runner.verification_summary()["generation_verified"])
        self.assertEqual(self.control.get("worker")["current_job_id"], first["id"])
        self.backend.uncertain, self.backend.state = False, "succeeded"
        self.now += 31
        with patch.object(WorkerRunner, "_collect", verified_collect):
            result = runner.run_once("worker", "finite-pool")
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.backend.submits, 1)
        self.assertEqual(self.repo.get_job(self.scope, first["id"])["attempt_no"], 1)
        self.assertEqual(self.repo.get_job(self.scope, waiting["id"])["attempt_no"], 0)
        self.assertTrue(runner.verification_summary()["generation_verified"])

    def test_preparation_failure_is_not_retried_on_same_failed_runtime(self):
        job = self.job_for()
        self.backend.prepare_failure = True
        runner = self.runner()
        result = runner.run_once("worker", "finite-pool")
        self.assertEqual(result["state"], "queued")
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["error_code"],
            "worker_preparation_not_ready",
        )
        self.assertEqual(self.backend.submits, 0)
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        self.now += 31
        self.assertEqual(runner.run_once("worker", "finite-pool")["state"], "idle")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_collection_failure_keeps_same_attempt_and_recovers_output_without_resubmit(
        self,
    ):
        first, waiting = self.job_for(), self.job_for()
        self.backend.state = "succeeded"
        runner = self.runner()
        with patch.object(
            WorkerRunner,
            "_collect",
            side_effect=BackendError("synthetic media validation failed"),
        ):
            self.assertEqual(
                runner.run_once("worker", "finite-pool")["state"], "collecting"
            )
        self.assertEqual(
            self.repo.get_job(self.scope, first["id"])["error_code"],
            "collection_failed",
        )
        self.assertEqual(self.control.get("worker")["current_job_id"], first["id"])
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        self.assertFalse(runner.verification_summary()["generation_verified"])
        self.now += 31
        with patch.object(WorkerRunner, "_collect", verified_collect):
            self.assertEqual(
                runner.run_once("worker", "finite-pool")["state"], "succeeded"
            )
        self.assertEqual(self.backend.submits, 1)
        self.assertEqual(self.repo.get_job(self.scope, waiting["id"])["attempt_no"], 0)
        self.assertTrue(runner.verification_summary()["generation_verified"])
        self.assertTrue(runner.verification_summary()["runtime_quarantined"])

    def test_user_cancellation_is_neither_verification_nor_runtime_failure(self):
        job = self.job_for()
        runner = self.runner()
        self.assertEqual(runner.run_once("worker", "finite-pool")["state"], "running")
        self.repo.request_cancel(self.scope, job["id"])
        self.assertEqual(runner.run_once("worker", "finite-pool")["state"], "cancelled")
        self.assertFalse(runner.verification_summary()["generation_verified"])
        self.assertEqual(self.control.get("worker")["drain_requested"], 0)
        self.assertEqual(self.backend.submits, 1)
        self.assertFalse(runner.stopped())

    def test_persisted_failure_survives_restart_even_if_old_ready_initializer_runs(
        self,
    ):
        job = self.job_for()
        self.backend.state = "failed"
        self.runner().run_once("worker", "finite-pool")
        self.control.mark_ready(
            "worker", upstream_idle_confirmed=True
        )  # Legacy initializer cannot authorize this profile.
        next_job = self.job_for()
        self.now += 31
        fresh = self.runner()
        self.assertEqual(fresh.run_once("worker", "finite-pool")["state"], "idle")
        self.assertEqual(self.repo.get_job(self.scope, next_job["id"])["attempt_no"], 0)
        self.assertEqual(self.backend.submits, 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "failed")

    def test_crash_before_failure_file_still_blocks_restart_from_sql_history(self):
        job = self.job_for()
        self.backend.state = "failed"
        runner = self.runner()
        with patch.object(
            runner,
            "_save_evidence",
            side_effect=OSError("synthetic unavailable control disk"),
        ):
            self.assertEqual(
                runner.run_once("worker", "finite-pool")["state"], "failed"
            )
        self.assertFalse(self.evidence_file.exists())
        self.control.mark_ready("worker", upstream_idle_confirmed=True)
        next_job = self.job_for()
        fresh = self.runner()
        self.assertEqual(fresh.run_once("worker", "finite-pool")["state"], "idle")
        self.assertEqual(self.repo.get_job(self.scope, next_job["id"])["attempt_no"], 0)
        self.assertEqual(self.backend.submits, 1)

    def test_later_other_recipe_failure_does_not_erase_success_or_qualify_that_recipe(
        self,
    ):
        self.job_for()
        runner = self.runner()
        self.backend.state = "succeeded"
        with patch.object(WorkerRunner, "_collect", verified_collect):
            runner.run_once("worker", "finite-pool")
        reference = self.job_for(recipe="synthetic-ref")
        self.backend.state = "failed"
        runner.run_once("worker", "finite-pool")
        proof = runner.verification_summary()
        self.assertTrue(proof["generation_verified"])
        self.assertTrue(proof["runtime_quarantined"])
        self.assertEqual(
            [scope["recipe_id"] for scope in proof["verified_job_scopes"]],
            ["synthetic-fl"],
        )
        self.assertEqual(proof["failures"][0]["job_id"], reference["id"])

    def test_evidence_identity_and_nonsecret_schema_are_strict(self):
        self.job_for()
        self.backend.state = "succeeded"
        runner = self.runner()
        with patch.object(WorkerRunner, "_collect", verified_collect):
            runner.run_once("worker", "finite-pool")
        wrong = {**self.identity, "instance_id": "other-instance"}
        with self.assertRaises(ValueError):
            read_verification_summary(self.evidence_file, expected_identity=wrong)
        value = json.loads(self.evidence_file.read_text(encoding="utf-8"))
        value["evidence"][0]["controls"]["prompt"] = "PRIVATE-PROMPT"
        self.evidence_file.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            read_verification_summary(
                self.evidence_file, expected_identity=self.identity
            )

    def test_successful_result_survives_evidence_write_failure_but_worker_drains(self):
        job = self.job_for()
        self.backend.state = "succeeded"
        runner = self.runner()
        with (
            patch.object(WorkerRunner, "_collect", verified_collect),
            patch.object(runner, "_save_evidence", side_effect=OSError),
        ):
            result = runner.run_once("worker", "finite-pool")
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(
            self.repo.get_job(self.scope, job["id"])["result"]["artifact_ids"]
        )
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        self.assertFalse(runner.verification_summary()["generation_verified"])

    def test_wrong_instance_identity_cannot_submit_before_evidence_check(self):
        job = self.job_for()
        self.identity = {**self.identity, "instance_id": "other-instance"}
        with self.assertRaisesRegex(ValueError, "queued_task_worker_identity_conflict"):
            self.runner().run_once("worker", "finite-pool")
        self.assertEqual(self.backend.submits, 0)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_many_later_jobs_do_not_grow_first_success_scope_or_quarantine_healthy_worker(
        self,
    ):
        runner = self.runner()
        self.backend.state = "succeeded"
        first = self.job_for(steps=4)
        with patch.object(WorkerRunner, "_collect", verified_collect):
            runner.run_once("worker", "finite-pool")
            original = self.evidence_file.read_bytes()
            for steps in range(5, 37):
                job = self.job_for(steps=steps)
                self.assertEqual(
                    runner.run_once("worker", "finite-pool")["state"], "succeeded"
                )
                self.assertTrue(
                    self.repo.get_job(self.scope, job["id"])["result"]["artifact_ids"]
                )
                self.assertEqual(self.evidence_file.read_bytes(), original)
        proof = runner.verification_summary()
        self.assertEqual(len(proof["verified_job_scopes"]), 1)
        self.assertEqual(proof["verified_job_scopes"][0]["job_id"], first["id"])
        self.assertEqual(proof["verified_job_scopes"][0]["controls"]["steps"], 4)
        self.assertFalse(proof["runtime_quarantined"])
        self.assertEqual(
            (self.backend.submits, self.control.get("worker")["state"]), (33, "ready")
        )

    def test_two_recipes_keep_separate_first_success_scopes(self):
        runner = self.runner()
        self.backend.state = "succeeded"
        with patch.object(WorkerRunner, "_collect", verified_collect):
            for recipe, steps in (
                ("synthetic-fl", 4),
                ("synthetic-ref", 8),
                ("synthetic-ref", 12),
                ("synthetic-fl", 20),
            ):
                self.job_for(recipe=recipe, steps=steps)
                runner.run_once("worker", "finite-pool")
        scopes = runner.verification_summary()["verified_job_scopes"]
        self.assertEqual(
            [(item["recipe_id"], item["controls"]["steps"]) for item in scopes],
            [("synthetic-fl", 4), ("synthetic-ref", 8)],
        )
        self.assertEqual(self.backend.submits, 4)

    def test_success_crash_before_observe_recovers_same_output_and_releases_binding(
        self,
    ):
        job = self.job_for()
        self.backend.state = "succeeded"
        runner = self.runner()
        with (
            patch.object(WorkerRunner, "_collect", verified_collect),
            patch.object(runner, "_observe", side_effect=RuntimeError),
        ):
            with self.assertRaises(RuntimeError):
                runner.run_once("worker", "finite-pool")
        completed = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(self.control.get("worker")["current_job_id"], job["id"])
        fresh = self.runner()
        self.assertEqual(fresh.run_once("worker", "finite-pool")["state"], "succeeded")
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["result"], completed["result"]
        )
        self.assertIsNone(self.control.get("worker")["current_job_id"])
        self.assertTrue(fresh.verification_summary()["generation_verified"])
        self.assertEqual(self.backend.submits, 1)

    def test_failed_crash_before_observe_recovers_quarantine_and_releases_terminal_binding(
        self,
    ):
        job, backlog = self.job_for(), self.job_for()
        self.backend.state = "failed"
        runner = self.runner()
        with patch.object(runner, "_observe", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                runner.run_once("worker", "finite-pool")
        self.assertEqual(self.control.get("worker")["current_job_id"], job["id"])
        fresh = self.runner()
        self.assertEqual(fresh.run_once("worker", "finite-pool")["state"], "failed")
        self.assertIsNone(self.control.get("worker")["current_job_id"])
        self.assertEqual(self.control.get("worker")["drain_requested"], 1)
        self.assertTrue(fresh.verification_summary()["runtime_quarantined"])
        self.assertEqual(self.repo.get_job(self.scope, backlog["id"])["attempt_no"], 0)
        self.assertEqual(self.backend.submits, 1)

    def test_deferred_preparation_crash_recovers_observation_without_submission(self):
        job = self.job_for()
        self.backend.prepare_failure = True
        runner = self.runner()
        with patch.object(runner, "_observe", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                runner.run_once("worker", "finite-pool")
        fresh = self.runner()
        self.assertEqual(fresh.run_once("worker", "finite-pool")["state"], "queued")
        self.assertIsNone(self.control.get("worker")["current_job_id"])
        self.assertTrue(fresh.verification_summary()["runtime_quarantined"])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)
        self.assertEqual(self.backend.submits, 0)

    def test_manual_terminal_label_with_unresolved_attempt_never_clears_binding(self):
        job = self.job_for()
        self.backend.uncertain = True
        runner = self.runner()
        self.assertEqual(
            runner.run_once("worker", "finite-pool")["state"], "submission_unknown"
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
        self.assertEqual(runner.run_once("worker", "finite-pool")["state"], "idle")
        self.assertEqual(self.control.get("worker")["current_job_id"], job["id"])
        self.assertFalse(runner.verification_summary()["generation_verified"])
        self.assertEqual(self.backend.submits, 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
