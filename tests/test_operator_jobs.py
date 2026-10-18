"""Human-admin command replay, whole-batch fences and existing budget refunds."""

import secrets
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from test_platform_repository import LedgerCase
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.dstack_operator import _actor
from studio_platform.job_events import JobEvents
from studio_platform.operator_capacity import OperatorError, operator_commands
from studio_platform.operator_jobs import OperatorJobs
from studio_platform.repository import Conflict, jobs, outbox
from studio_platform.settings import Settings


class OperatorJobTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.repo.close)
        self.settings = Settings(
            Path(self.temp.name),
            tenant_id=self.scope.tenant_id,
            auth_mode="local-test",
            database_url=self.url,
            operator_capacity_owners=("superdan",),
            operator_admin_owners=("superdan",),
        )
        self.service = OperatorJobs(self.repo, self.settings)
        self.admin = Principal("superdan", "browser:superdan")

    def body(self, *identities):
        values = {r["id"]: r for r in self.service.list(self.admin)["jobs"]}
        return {
            "jobs": [
                {"id": value, "expected_snapshot": values[value]["snapshot"]}
                for value in identities
            ],
            "reason_code": "operator_queue_cleared",
        }

    def count(self, table):
        with self.repo.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(table))

    def test_cross_owner_atomic_cancel_replay_retains_original_budgets_and_events(self):
        own, foreign = self.job(), self.job(scope=self.other, cost=0)
        body = self.body(own["id"], foreign["id"])
        result = self.service.cancel(self.admin, body, "one-bulk-command")
        self.assertEqual(set(result["cancelled_job_ids"]), {own["id"], foreign["id"]})
        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            self.service.cancel(self.admin, body, "one-bulk-command"), result
        )
        self.assertEqual(self.count(operator_commands), 1)
        with self.repo.engine.connect() as connection:
            audits = list(
                connection.execute(
                    select(outbox).where(
                        outbox.c.event_type == "operator.jobs_cancelled"
                    )
                ).mappings()
            )
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["payload"]["actor_id"], "browser:superdan")
        feed = JobEvents(self.repo, self.scope.tenant_id)
        event = feed.read(
            Principal("supervan", "agent", True, (), ("jobs:read",), True)
        )["events"][-1]
        self.assertEqual(event["job_id"], foreign["id"])
        self.assertEqual(event["cancelled_by"], "operator")
        self.assertEqual(event["reason_code"], "operator_queue_cleared")
        self.assertTrue(event["retry_advice"]["safe_to_retry"])
        self.assertIs(event["retry_advice"]["charged"], False)
        self.assertEqual(
            self.repo.get_job(self.scope, own["id"])["status"], "cancelled"
        )
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 0)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        changed = {**body, "reason_code": "capacity_unavailable"}
        with self.assertRaises(Conflict):
            self.service.cancel(self.admin, changed, "one-bulk-command")

    def test_stale_or_running_member_rejects_entire_batch_before_refund(self):
        first, second = self.job(), self.job()
        body = self.body(first["id"], second["id"])
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == second["id"])
                .values(status="running", updated_at=self.now + 1)
            )
        with self.assertRaises(Conflict):
            self.service.cancel(self.admin, body, "stale-batch")
        self.assertEqual(self.repo.get_job(self.scope, first["id"])["status"], "queued")
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 200000
        )
        self.assertEqual(self.count(operator_commands), 0)
        current = self.body(first["id"], second["id"])
        with self.assertRaises(Conflict):
            self.service.cancel(self.admin, current, "running-batch")
        self.assertFalse(
            next(
                r
                for r in self.service.list(self.admin)["jobs"]
                if r["id"] == second["id"]
            )["cancellable"]
        )

    def test_machine_and_nonadmin_forbidden_before_read_or_write(self):
        for actor in (
            Principal("supervan", "browser:supervan"),
            Principal("superdan", "pat", True, (), ("jobs:read", "jobs:write"), True),
        ):
            with self.assertRaises(OperatorError):
                self.service.list(actor)
            with self.assertRaises(OperatorError):
                self.service.cancel(actor, {}, "denied")

    def test_admin_stuck_diagnostic_projects_only_latest_closed_delivery_and_original_attempt(
        self,
    ):
        from test_postgres_dispatch import PostgresDispatchTests
        from studio_platform.repository import dispatch_receipts, outbox
        from sqlalchemy import insert
        import uuid

        original = PostgresDispatchTests.make_job(self)
        # Historical unknown delivery remains diagnosable after transport removal.
        with self.repo.transaction() as connection:
            event_id = str(uuid.uuid4())
            connection.execute(
                insert(outbox).values(
                    id=event_id,
                    event_type="job.dispatch_requested",
                    aggregate_id=original["id"],
                    payload={},
                    created_at=self.now,
                )
            )
            connection.execute(
                insert(dispatch_receipts).values(
                    event_id=event_id,
                    job_id=original["id"],
                    sequence=1,
                    state="unknown",
                    publish_attempts=1,
                    not_before=self.now,
                    error_code="dispatch_response_unknown",
                    updated_at=self.now,
                )
            )
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == original["id"])
                .values(
                    status="cancel_requested",
                    error_code="private raw message must not escape",
                )
            )
        budget = self.repo.get_budget("owner-budget")
        row = next(
            value
            for value in self.service.list(self.admin)["jobs"]
            if value["id"] == original["id"]
        )
        self.assertEqual(row["status"], "cancel_requested")
        self.assertFalse(row["cancellable"])
        diagnostic = row["diagnostic"]
        self.assertEqual(diagnostic["reason_code"], "job_cancel_pending")
        self.assertEqual(diagnostic["error_code"], "generation_failure_unclassified")
        self.assertEqual(diagnostic["dispatch"]["state"], "unknown")
        self.assertEqual(
            diagnostic["dispatch"]["reason_code"], "dispatch_response_unknown"
        )
        self.assertEqual(diagnostic["attempt_no"], 0)
        self.assertIsNone(diagnostic["current_attempt_id"])
        self.assertNotIn("private raw message", str(row))
        self.assertNotIn("prompt", str(row))
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_http_admin_pat_csrf_account_and_reason_validation(self):
        original = self.job()
        app = create_app(self.settings, repository=self.repo)
        key = secrets.token_urlsafe(32)
        app.state.auth.register_client(
            "no-admin",
            key,
            "superdan",
            [self.scope.project_id],
            ["jobs:read", "jobs:write"],
        )
        with TestClient(app) as client:
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            listed = client.get("/v1/operator/admin/jobs")
            self.assertEqual(listed.status_code, 200, listed.text)
            body = self.body(original["id"])
            headers = {"Idempotency-Key": "route-bulk"}
            self.assertEqual(
                client.get(
                    "/v1/operator/admin/jobs",
                    headers={"Authorization": "Bearer " + key},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/v1/operator/admin/jobs/cancel",
                    json=body,
                    headers={**headers, "Origin": "https://untrusted.example"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/v1/operator/admin/jobs/cancel",
                    json=body,
                    headers={**headers, "X-Expected-Account": "supervan"},
                ).status_code,
                409,
            )
            for invalid in (
                {**body, "reason_code": "private raw error"},
                {**body, "reason_code": []},
                {**body, "jobs": body["jobs"] * 2},
                {**body, "owner": "supervan"},
            ):
                self.assertEqual(
                    client.post(
                        "/v1/operator/admin/jobs/cancel", json=invalid, headers=headers
                    ).status_code,
                    422,
                )
            response = client.post(
                "/v1/operator/admin/jobs/cancel", json=body, headers=headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                client.post(
                    "/v1/operator/admin/jobs/cancel", json=body, headers=headers
                ).json(),
                response.json(),
            )

    def test_original_operator_key_cannot_be_reused_across_command_kinds(self):
        original = self.job()
        with self.repo.transaction() as connection:
            connection.execute(
                insert(operator_commands).values(
                    id=str(uuid.uuid4()),
                    actor=_actor(self.scope.tenant_id, self.admin.owner),
                    idempotency_key="old-start-key",
                    request_hash="0" * 64,
                    kind="dstack_start",
                    state="accepted",
                    payload={"node_id": "existing"},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        with self.assertRaises(Conflict):
            self.service.cancel(self.admin, self.body(original["id"]), "old-start-key")
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "queued"
        )
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100000
        )
