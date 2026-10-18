"""Read-only alerts from original events, accounting and provider obligations."""

import copy
import json
from datetime import datetime, timezone
from sqlalchemy import event, insert, select, update
from studio_platform.alert_snapshot import read_snapshot
from studio_platform.alert_conditions import evaluate
from studio_platform.dstack_test_budget import day_window, projected_cost
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import (
    attempts,
    budget_reservations,
    instance_intents,
    jobs,
    outbox,
    registered_workers,
)
from test_platform_repository import LedgerCase
import test_dstack_test_budget as budget_fixture


class AlertSnapshotTests(LedgerCase):
    def snapshot(self):
        return read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)

    def transition(self, job, status, event_type):
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(status=status, updated_at=self.now)
            )
            self.repo._emit(c, event_type, job["id"], {})

    def test_polling_updated_at_and_same_status_events_do_not_reset_state_age(self):
        job = self.job()
        self.now += 60
        self.transition(job, "running", "job.running")
        first = self.now
        for _ in range(4):
            self.now += 600
            self.transition(job, "running", "job.running")
        current = self.snapshot()["jobs"][0]
        self.assertEqual(current["state_since"], first)
        self.assertEqual(current["wake_count"], 0)
        alert = evaluate(self.snapshot(), None, environment="staging", now=self.now)
        self.assertEqual(alert["notifications"][0]["code"], "job_stuck")
        self.transition(job, "collecting", "job.collecting")
        self.assertEqual(self.snapshot()["jobs"][0]["state_since"], self.now)

    def test_missing_transition_history_is_unknown_not_updated_at(self):
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(status="running", updated_at=1)
            )
        self.assertIsNone(self.snapshot()["jobs"][0]["state_since"])

    def test_wake_counts_exclude_normal_poll_leases_and_are_windowed(self):
        job = self.job()
        for _ in range(12):
            self.transition(job, "queued", "job.queued")
        with self.repo.transaction() as c:
            for _ in range(50):
                self.repo._emit(c, "job.leased", job["id"], {"purpose": "reconcile"})
        self.assertEqual(self.snapshot()["jobs"][0]["wake_count"], 13)
        self.now += 1801
        self.assertEqual(self.snapshot()["jobs"][0]["wake_count"], 0)

    def test_failure_codes_are_closed_and_snapshot_has_no_request_or_sql_writes(self):
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    status="failed",
                    error_code="PRIVATE token prompt",
                    result={
                        "failure_diagnostic": {"reason_code": "PRIVATE token prompt"}
                    },
                )
            )
        statements = []

        def capture(c, cu, statement, *args):
            statements.append(statement)

        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            value = self.snapshot()
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(
            value["failures"][0]["kind"], "generation_failure_unclassified"
        )
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("request", json.dumps(value))
        self.assertTrue(
            all(
                s.lstrip().upper().startswith(("SELECT", "SET", "SHOW"))
                for s in statements
            )
        )

    def test_foreign_tenant_jobs_do_not_enter_snapshot(self):
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(tenant_id="foreign")
            )
        self.assertEqual(self.snapshot()["jobs"], [])

    def test_lifetime_history_over_limit_is_aggregated_without_hiding_active_jobs(self):
        import uuid

        active = self.job()
        with self.repo.transaction() as c:
            template = dict(
                c.execute(select(jobs).where(jobs.c.id == active["id"]))
                .mappings()
                .one()
            )
            history = []
            for i in range(10005):
                ident = str(uuid.uuid4())
                history.append(
                    {
                        **template,
                        "id": ident,
                        "idempotency_key": ident,
                        "status": "failed",
                        "error_code": "private-unclassified-" + str(i),
                        "result": {},
                    }
                )
            c.execute(insert(jobs), history)
            c.execute(
                insert(attempts),
                [
                    {
                        "id": str(uuid.uuid4()),
                        "job_id": row["id"],
                        "number": 1,
                        "worker_id": "retired-history",
                        "fence": 1,
                        "status": "failed",
                        "created_at": self.now,
                        "updated_at": self.now,
                        "upstream_stopped": 1,
                    }
                    for row in history
                ],
            )
        result = self.snapshot()
        self.assertTrue(result["complete"])
        self.assertEqual([row["id"] for row in result["jobs"]], [active["id"]])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(
            result["failures"][0]["kind"], "generation_failure_unclassified"
        )
        self.assertNotIn("private-unclassified", json.dumps(result))


class BudgetAlertSnapshotTests(budget_fixture.DailyTestBudgetTests):
    def snapshot(self):
        return read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)

    def test_alert_projection_uses_original_unknown_hold_once_across_sydney_midnight(
        self,
    ):
        self.start()
        with self.repo.engine.connect() as c:
            before = [
                dict(row) for row in c.execute(select(budget_reservations)).mappings()
            ]
        first = self.snapshot()
        self.assertEqual(first["test_spend"]["scope"], "acceptance_task")
        self.assertEqual(
            first["test_spend"]["tasks"][0]["projected_microusd"], 1_812_500
        )
        self.now = day_window(self.now)[1] + 1
        next_day = self.snapshot()
        with self.repo.engine.connect() as c:
            expected = projected_cost(
                c, self.rows()[1]['payload']['lifecycle']['test_gpu_guard'], self.now
            )
            self.assertEqual(
                [
                    dict(row)
                    for row in c.execute(select(budget_reservations)).mappings()
                ],
                before,
            )
        self.assertEqual(
            next_day["test_spend"]["tasks"][0]["projected_microusd"], expected
        )
        self.assertGreater(expected, 0)
        self.assertEqual(
            first["test_spend"]["tasks"][0]["resource_id"],
            next_day["test_spend"]["tasks"][0]["resource_id"],
        )

    def test_settled_task_actuals_do_not_reset_by_calendar(self):
        self.history(amount=1_812_500, actual=19_000_000)
        self.assertEqual(
            self.snapshot()["test_spend"]["tasks"][0]["projected_microusd"], 19_000_000
        )
        self.now = day_window(self.now)[1] + 1
        self.assertEqual(
            self.snapshot()["test_spend"]["tasks"][0]["projected_microusd"], 19_000_000
        )

    def test_legacy_guard_snapshot_remains_readable_until_scoped_tasks_exist(self):
        self.history(amount=1_812_500, actual=19_000_000)
        intent, node = self.rows()
        payload = copy.deepcopy(node['payload'])
        payload['lifecycle']['test_gpu_guard'] = copy.deepcopy(budget_fixture.GUARD)
        payload['request'].pop('test_task_id')
        with self.repo.transaction() as c:
            c.execute(update(operator_nodes).values(payload=payload))
        self.assertEqual(
            self.snapshot()['test_spend']['projected_microusd'], 19_000_000
        )
        self.now = day_window(self.now)[1] + 1
        self.assertEqual(self.snapshot()['test_spend']['projected_microusd'], 0)

    def test_gpu_original_idle_setting_and_unsafe_attempt_obligations(self):
        node_id = self.start()["node_id"]
        intent, node = self.rows()
        payload = copy.deepcopy(node["payload"])
        payload["dstack"]["first_runtime_ready_at"] = self.now
        with self.repo.transaction() as c:
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node_id)
                .values(state="ready", provider_instance_id="original-123")
            )
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(runtime_state="ready", payload=payload)
            )
        value = self.snapshot()["gpus"][0]
        self.assertEqual(value["idle_limit_seconds"], 600)
        self.assertFalse(value["has_obligations"])
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
            c.execute(
                insert(registered_workers).values(
                    id="original-worker",
                    pool=intent["pool"],
                    provider="vast",
                    instance_id="original-123",
                    spec={},
                    spec_hash="a" * 64,
                    state="retired",
                    current_job_id=None,
                    drain_requested=1,
                    fence=1,
                    expires_at=self.now,
                    updated_at=self.now,
                )
            )
            c.execute(
                insert(attempts).values(
                    id=__import__("uuid").uuid4().hex,
                    job_id=job["id"],
                    number=1,
                    worker_id="original-worker",
                    fence=1,
                    status="failed",
                    created_at=self.now,
                    updated_at=self.now,
                    submission_started_at=self.now,
                    upstream_task_id="original",
                    upstream_stopped=0,
                )
            )
        self.assertTrue(self.snapshot()["gpus"][0]["has_obligations"])


class TerminalSnapshot254Tests(LedgerCase):
    def test_terminal_hold_age_uses_original_event_not_reconciliation_updates(self):
        job = self.job()
        first = self.now
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(status="failed", updated_at=self.now)
            )
            self.repo._emit(c, "job.failed", job["id"], {})
        self.now += 7200
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(updated_at=self.now)
            )
        value = read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)
        self.assertEqual(
            value["terminal_reservations"], [{"id": job["id"], "terminal_since": first}]
        )
        with self.repo.transaction() as c:
            self.repo._settle(c, "job", job["id"], 0)
        self.assertEqual(
            read_snapshot(self.repo, now=self.now, tenant_id=self.scope.tenant_id)[
                "terminal_reservations"
            ],
            [],
        )
