"""Original-ledger usage projections; no provider, Grafana or paid generation."""
from datetime import datetime, timezone
import uuid

from sqlalchemy import event, insert, update

from studio_platform.dstack_metrics import machine_metrics
from studio_platform.repository import (artifacts, attempts, budget_reservations,
    instance_intents, jobs, outbox, registered_workers, scaler_actions, scaler_receipts)
from test_platform_repository import LedgerCase


class DstackMetricsTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 12, 2, tzinfo=timezone.utc).timestamp()
        self.node_id, self.run_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.binding = {"run_id": self.run_id, "provider_instance_id": "vast-123",
            "state": "ready", "first_runtime_ready_at": self.now - 1800,
            "runtime_incarnation": "a" * 32, "observed_at": self.now,
            "estimated_cost_microusd": 410000}
        self.node = {"intent_id": self.node_id, "runtime_state": "ready", "payload": {
            "capacity_backend": "dstack-v1", "tenant_id": self.scope.tenant_id,
            "owner_id": self.scope.owner_id, "project_id": self.scope.project_id,
            "hourly_cost_microusd": 850000, "dstack": self.binding}}
        self.intent = {"id": self.node_id, "intent_key": "test-node", "pool": "test-pool",
            "request_hash": "a" * 64, "state": "ready", "physical_gpus": 1, "slots": 1,
            "reserved_cost_microusd": 1000000, "provider_instance_id": "vast-123",
            "provider": "vast", "hard_deadline": None, "created_at": self.now - 4000,
            "updated_at": self.now}
        self.worker = {"id": "dstack-" + self.node_id.replace("-", ""),
            "pool": "test-pool", "provider": "vast", "instance_id": "vast-123",
            "spec": {}, "spec_hash": "a" * 64, "state": "retired", "current_job_id": None,
            "drain_requested": 0, "fence": 0, "expires_at": self.now - 1, "updated_at": self.now}
        with self.repo.transaction() as connection:
            connection.execute(insert(instance_intents).values(**self.intent))
            connection.execute(insert(scaler_actions).values(intent_id=self.node_id, pool="test-pool",
                launch_spec={}, create_started_at=self.now - 3600))
            connection.execute(insert(registered_workers).values(**self.worker))

    def projection(self, *, workers=None, timezone_name="Australia/Sydney"):
        with self.repo.engine.connect() as connection:
            return machine_metrics(connection, self.node, self.intent,
                [self.worker] if workers is None else workers, self.now, timezone=timezone_name)

    def success(self, *, scope=None, worker_id=None, completed=None, submitted=None,
                validated=True, event_record=True, duplicate_video=False, status="succeeded"):
        job = self.job(scope=scope, cost=0)
        completed = self.now - 100 if completed is None else completed
        submitted = completed - 200 if submitted is None else submitted
        attempt = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(
                status=status, current_attempt_id=attempt, attempt_no=1, updated_at=self.now))
            connection.execute(insert(attempts).values(id=attempt, job_id=job["id"], number=1,
                status=status, fence=1, worker_id=worker_id or self.worker["id"],
                created_at=submitted - 10, submission_started_at=submitted,
                upstream_task_id="native-task", upstream_stopped=1, collection_failures=0,
                updated_at=self.now))
            for kind in ("video", "audio") + (("video",) if duplicate_video else ()):
                connection.execute(insert(artifacts).values(id=str(uuid.uuid4()), job_id=job["id"],
                    attempt_id=attempt, metadata={"kind": kind, "validated": validated,
                    "object_key": "private-object", "size_bytes": 10, "sha256": "a" * 64},
                    created_at=completed))
            if event_record:
                connection.execute(insert(outbox).values(id=str(uuid.uuid4()), event_type="job.succeeded",
                    aggregate_id=job["id"], payload={"job_id": job["id"]}, created_at=completed))
        return job["id"], attempt

    def settle(self, costs):
        for index, value in enumerate(costs):
            account = "metrics-budget-" + str(index)
            self.repo.configure_budget(account, tenant_id=self.scope.tenant_id,
                owner_id=self.scope.owner_id, limit_microusd=10000000)
            with self.repo.transaction() as connection:
                connection.execute(insert(budget_reservations).values(id=str(uuid.uuid4()),
                    account_id=account, reference_type="instance", reference_id=self.node_id,
                    amount_microusd=1000000, state="settled" if value else "released",
                    actual_cost_microusd=value, created_at=self.now - 3600))

    def test_modeled_total_and_today_ceiling_are_different_bases(self):
        value = self.projection()
        self.assertEqual(value["usage"]["runtime_seconds"], 3600)
        self.assertEqual(value["usage"]["total_cost_microusd"], 410000)
        self.assertEqual(value["usage"]["cost_basis"], "dstack_estimate")
        self.assertEqual(value["usage"]["today_cost_microusd"], 850000)
        self.assertEqual(value["usage"]["today_cost_basis"], "approved_ceiling_estimate")
        self.assertEqual(value["usage"]["video_count"], 0)

    def test_first_terminal_latch_freezes_runtime_when_observation_changes(self):
        self.node["runtime_state"] = "stopped"
        self.binding.update(state="stopped", first_stopped_at=self.now - 600)
        before = self.projection()
        self.now += 3600
        self.binding["observed_at"] = self.now
        self.intent["updated_at"] = self.now
        after = self.projection()
        self.assertEqual(before["usage"]["runtime_seconds"], 3000)
        self.assertEqual(after["usage"], before["usage"])

    def test_ended_record_without_terminal_evidence_does_not_grow_runtime(self):
        self.node["runtime_state"] = "stopped"
        self.binding.update(state="stopped", observed_at=self.now - 100)
        value = self.projection()
        self.assertIsNone(value["usage"]["runtime_seconds"])
        self.assertIsNone(value["usage"]["today_cost_microusd"])
        self.assertEqual(value["usage"]["total_cost_microusd"], 410000)

    def test_first_exact_terminal_receipt_ignores_requests_and_other_run(self):
        self.node["runtime_state"] = "stopped"
        self.binding["state"] = "stopped"
        with self.repo.transaction() as connection:
            for elapsed, facts in ((800, {"state": "journaled", "run_id": self.run_id}),
                    (700, {"state": "stopped", "run_id": str(uuid.uuid4())}),
                    (600, {"state": "stopped", "run_id": self.run_id}),
                    (100, {"state": "stopped", "run_id": self.run_id})):
                connection.execute(insert(scaler_receipts).values(id=str(uuid.uuid4()),
                    intent_id=self.node_id, operation="destroy", observed_at=self.now - elapsed,
                    facts=facts))
        self.assertEqual(self.projection()["usage"]["runtime_seconds"], 3000)

    def test_layered_settlement_is_single_charge_not_sum(self):
        self.binding.update(state="stopped", first_stopped_at=self.now - 100)
        self.node["runtime_state"] = "stopped"
        self.settle([420000, 420000])
        usage = self.projection()["usage"]
        self.assertEqual(usage["total_cost_microusd"], 420000)
        self.assertEqual(usage["today_cost_microusd"], 420000)
        self.assertEqual(usage["cost_basis"], "settled")
        self.assertEqual(usage["today_cost_basis"], "settled")

    def test_conflicting_or_missing_costs_are_not_free(self):
        self.settle([420000, 430000])
        self.binding.pop("estimated_cost_microusd")
        self.node["payload"].pop("hourly_cost_microusd")
        usage = self.projection()["usage"]
        self.assertIsNone(usage["total_cost_microusd"])
        self.assertIsNone(usage["today_cost_microusd"])
        self.assertEqual(usage["cost_basis"], "unavailable")

    def test_today_uses_sydney_day_overlap_not_lifetime_total(self):
        # At 02:00 UTC Sydney is 13:00; midnight was 13:00 UTC yesterday.
        with self.repo.transaction() as connection:
            connection.execute(update(scaler_actions).values(create_started_at=self.now - 16 * 3600))
        self.settle([1800000, 1800000])
        usage = self.projection()["usage"]
        self.assertEqual(usage["runtime_seconds"], 16 * 3600)
        self.assertEqual(usage["total_cost_microusd"], 1800000)
        self.assertEqual(usage["today_cost_microusd"], 13 * 850000)
        self.assertEqual(usage["today_cost_basis"], "approved_ceiling_estimate")

    def test_dedup_validated_success_uses_original_completion_not_settlement(self):
        first, attempt = self.success(duplicate_video=True)
        self.success(submitted=self.now - 550, completed=self.now - 150)
        self.success(validated=False)
        self.success(status="failed")
        self.success(scope=self.other)
        self.success(worker_id="unrelated-worker")
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == first).values(updated_at=self.now + 5000))
            connection.execute(update(attempts).where(attempts.c.id == attempt).values(updated_at=self.now + 5000))
        value = self.projection()
        self.assertEqual(value["usage"]["video_count"], 2)
        self.assertEqual(value["usage"]["today_video_count"], 2)
        self.assertEqual(value["metrics"]["samples"], 2)
        self.assertEqual(value["metrics"]["average_completion_seconds"], 300)
        self.assertEqual(value["metrics"]["basis"], "ledger_submit_to_validated_result")
        self.assertIsNone(value["metrics"]["average_generate_seconds"])
        self.assertIsNone(value["metrics"]["average_generation_cost_microusd"])

    def test_missing_success_timestamp_retains_video_but_not_today_or_duration(self):
        self.success(event_record=False)
        value = self.projection()
        self.assertEqual(value["usage"]["video_count"], 1)
        self.assertIsNone(value["usage"]["today_video_count"])
        self.assertIsNone(value["metrics"]["average_completion_seconds"])
        self.assertEqual(value["metrics"]["samples"], 0)

    def test_detail_cloud_reader_receives_only_exact_successes_and_labels_cost(self):
        profile = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3"
        self.binding.update(profile_id=profile, mode="fl")
        job_id, attempt_id = self.success()
        self.success(validated=False)
        self.success(scope=self.other)
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job_id).values(
                request={"deployment_profile_id": profile, "request": {"mode": "fl"}}))
        calls = []
        class Reader:
            def generate(self, **kwargs):
                calls.append(kwargs)
                return {"average_generate_seconds": 100.0, "generate_samples": 1,
                    "generation_basis": "grafana_loki_submit_to_upstream_terminal",
                    "stage_query_status": "ok", "raw_private_log": "must-not-leak"}
        with self.repo.engine.connect() as connection:
            result = machine_metrics(connection, self.node, self.intent, [self.worker], self.now,
                stage_reader=Reader())
        self.assertEqual(calls[0]["node_id"], self.node_id)
        self.assertEqual([(row["job_id"], row["attempt_id"]) for row in calls[0]["attempts"]],
            [(job_id, attempt_id)])
        self.assertEqual(result["metrics"]["average_generation_cost_microusd"], 23612)
        self.assertEqual(result["metrics"]["generation_cost_basis"], "approved_ceiling_estimate")
        self.assertNotIn("raw_private_log", result["metrics"])

    def test_optional_cloud_failure_cannot_replace_ledger_usage(self):
        self.success()
        class Reader:
            def generate(self, **_kwargs):
                raise RuntimeError("Authorization: private-token")
        with self.repo.engine.connect() as connection:
            result = machine_metrics(connection, self.node, self.intent, [self.worker], self.now,
                stage_reader=Reader())
        self.assertEqual(result["usage"]["video_count"], 1)
        self.assertEqual(result["metrics"]["stage_query_status"], "query_unavailable")
        self.assertEqual(result["metrics"]["average_completion_seconds"], 200)

    def test_no_retained_worker_for_qualified_allocation_is_unknown(self):
        usage = self.projection(workers=[])["usage"]
        self.assertIsNone(usage["video_count"])
        self.assertIsNone(usage["today_video_count"])
        self.binding.pop("first_runtime_ready_at")
        self.binding.pop("runtime_incarnation")
        self.assertEqual(self.projection(workers=[])["usage"]["video_count"], 0)

    def test_reused_provider_instance_does_not_recount_other_allocation(self):
        old_worker = {**self.worker, "id": "dstack-" + uuid.uuid4().hex}
        self.success(worker_id=old_worker["id"])
        usage = self.projection(workers=[self.worker, old_worker])["usage"]
        self.assertEqual(usage["video_count"], 0)
        self.assertEqual(usage["today_video_count"], 0)

    def test_same_day_settled_zero_is_explicit_free_charge(self):
        self.binding.update(state="stopped", first_stopped_at=self.now - 100)
        self.node["runtime_state"] = "stopped"
        self.settle([0, 0])
        usage = self.projection()["usage"]
        self.assertEqual(usage["total_cost_microusd"], 0)
        self.assertEqual(usage["today_cost_microusd"], 0)
        self.assertEqual(usage["cost_basis"], "settled")

    def test_unknown_timezone_does_not_silently_use_utc(self):
        self.success()
        value = self.projection(timezone_name="Invalid/Zone")
        self.assertIsNone(value["usage"]["today_cost_microusd"])
        self.assertIsNone(value["usage"]["today_video_count"])
        self.assertEqual(value["usage"]["video_count"], 1)

    def test_projection_only_selects_and_does_not_expose_original_metadata(self):
        self.success()
        statements = []
        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            value = self.projection()
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
        self.assertNotIn("private-object", str(value))
        self.assertNotIn(self.run_id, str(value))
        self.assertNotIn("sha256", str(value))


if __name__ == "__main__":
    import unittest
    unittest.main()
