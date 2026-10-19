"""Canonical admin writes, original cancellation and audit; disposable state only."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import base64
import json
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import (
    Column,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    func,
    insert,
    select,
    update,
)

from studio_platform.auth import Auth, Principal
from studio_platform.identity_admin import AdminError, IdentityAdmin
from studio_platform.identity_schema import admin_audit, metadata, users
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    attempts,
    budget_reservations,
    documents,
    jobs,
    outbox,
)
from test_platform_repository import LedgerCase


class IdentityAdminTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.auth = Auth(
            self.repo.engine, mode="local-test", tenant=self.scope.tenant_id
        )
        self.version = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), nullable=False),
            PrimaryKeyConstraint("version_num", name="alembic_version_pkc"),
        )
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            self.version.create(connection)
            connection.execute(insert(self.version).values(version_num="20261019_0003"))
            self.ids = {}
            for owner, role in (("superdan", "admin"), ("supervan", "user")):
                self.ids[owner] = str(uuid.uuid4())
                connection.execute(
                    insert(users).values(
                        id=self.ids[owner],
                        tenant=self.scope.tenant_id,
                        owner=owner,
                        role=role,
                        status="active",
                        display_name=owner,
                        email=None,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.repo.configure_budget(
            "target-budget",
            tenant_id=self.scope.tenant_id,
            owner_id="supervan",
            limit_microusd=10_000_000,
        )
        self.settings = SimpleNamespace(tenant_id=self.scope.tenant_id)
        self.service = IdentityAdmin(self.repo, self.settings)
        self.admin = Principal("superdan", "browser:superdan")
        self.user = Principal("supervan", "browser:supervan")

    def count(self, table):
        with self.repo.engine.connect() as connection:
            return connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()

    def target_job(self, *, cost=100_000):
        return self.repo.create_job(
            self.other,
            self.plan(self.other, cost=cost)["id"],
            uuid.uuid4().hex,
            budget_account_ids=["target-budget"],
        )

    def test_search_pagination_and_credit_fields_do_not_expose_credentials(self):
        first = self.service.list_users(self.admin, limit=1)
        second = self.service.list_users(
            self.admin, cursor=first["next_cursor"], limit=1
        )
        self.assertEqual(len({first["items"][0]["id"], second["items"][0]["id"]}), 2)
        self.assertIsNone(second["next_cursor"])
        self.assertIsNone(first["items"][0]["balance"])
        self.assertIsNone(first["items"][0]["used_30d"])
        self.assertNotIn("password_hash", first["items"][0])
        self.assertEqual(
            self.service.list_users(self.admin, q="VAN")["items"][0]["id"],
            self.ids["supervan"],
        )
        with self.assertRaisesRegex(AdminError, "cursor"):
            self.service.list_users(self.admin, q="van", cursor=first["next_cursor"])

    def test_admin_authority_is_fresh_never_inherited_by_machine_or_other_tenant(self):
        for actor in (self.user, Principal("superdan", "key:admin", machine=True)):
            with self.assertRaisesRegex(AdminError, "admin_required"):
                self.service.list_users(actor)
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.owner == "superdan")
                .values(status="suspended")
            )
        with self.assertRaisesRegex(AdminError, "admin_required"):
            self.service.patch(
                self.admin,
                self.ids["supervan"],
                {"role": "admin", "reason": "stale actor"},
            )
        self.assertEqual(self.count(admin_audit), 0)

    def test_legacy_or_other_revision_endpoints_are_not_ready(self):
        with self.repo.transaction() as connection:
            connection.execute(update(self.version).values(version_num="20261019_0002"))
        with self.assertRaisesRegex(AdminError, "not ready") as failure:
            self.service.list_users(self.admin)
        self.assertEqual(failure.exception.status, 503)

    def test_patch_is_audited_once_replayed_and_noop_does_not_audit(self):
        body = {"role": "admin", "reason": "Owner approved admin access"}
        first = self.service.patch(
            self.admin, self.ids["supervan"], body, key="promote"
        )
        self.assertEqual(first["role"], "admin")
        self.assertEqual(
            self.service.patch(self.admin, self.ids["supervan"], body, key="promote"),
            first,
        )
        self.assertEqual(self.count(admin_audit), 1)
        with self.assertRaisesRegex(AdminError, "another request"):
            self.service.patch(
                self.admin,
                self.ids["supervan"],
                {**body, "role": "user"},
                key="promote",
            )
        self.service.patch(self.admin, self.ids["supervan"], body, key="already-admin")
        self.assertEqual(self.count(admin_audit), 1)
        entry = self.service.audit(self.admin)["items"][0]
        self.assertEqual(
            (entry["actor"], entry["action"], entry["target"]),
            ("superdan", "role → admin", "supervan"),
        )
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(admin_audit)).mappings().one()
        self.assertEqual(row["before"], {"role": "user", "status": "active"})

    def test_concurrent_idempotent_requests_create_one_change_and_audit(self):
        body = {"role": "admin", "reason": "one original request"}
        with ThreadPoolExecutor(max_workers=3) as executor:
            rows = list(
                executor.map(
                    lambda _: self.service.patch(
                        self.admin, self.ids["supervan"], body, key="concurrent-key"
                    ),
                    range(3),
                )
            )
        self.assertTrue(all(row == rows[0] for row in rows))
        self.assertEqual(self.count(admin_audit), 1)
        self.assertEqual(self.count(documents), 1)

    def test_target_ids_and_search_are_scoped_to_the_current_tenant(self):
        foreign_id = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(
                insert(users).values(
                    id=foreign_id,
                    tenant="other-tenant",
                    owner="supervan",
                    display_name="Foreign person",
                    role="admin",
                    status="active",
                    email=None,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        self.assertEqual(self.service.list_users(self.admin, q="Foreign")["items"], [])
        with self.assertRaisesRegex(AdminError, "User not found"):
            self.service.patch(
                self.admin, foreign_id, {"role": "user", "reason": "wrong scope"}
            )
        self.assertEqual(self.count(admin_audit), 0)

    def test_last_active_admin_protection_includes_concurrent_self_demotion(self):
        for body in ({"role": "user"}, {"status": "suspended"}):
            with self.assertRaisesRegex(AdminError, "last active admin"):
                self.service.patch(
                    self.admin, self.ids["superdan"], {**body, "reason": "test"}
                )
        self.service.patch(
            self.admin,
            self.ids["supervan"],
            {"role": "admin", "reason": "second admin"},
        )

        def demote(owner):
            try:
                self.service.patch(
                    Principal(owner, "browser:" + owner),
                    self.ids[owner],
                    {"role": "user", "reason": "concurrent"},
                    key="demote-self",
                )
                return "done"
            except AdminError as error:
                return error.detail

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(demote, ("superdan", "supervan")))
        self.assertEqual(results.count("done"), 1)
        self.assertTrue(any("last active admin" in result for result in results))
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count())
                    .select_from(users)
                    .where(
                        users.c.role == "admin",
                        users.c.status == "active",
                    )
                ).scalar_one(),
                1,
            )

    def test_suspend_cancels_only_proven_original_jobs_and_releases_only_their_holds(
        self,
    ):
        queue = TaskQueue(self.repo)
        claimed = self.target_job()
        queue.claim("claimed-worker", "test-pool")
        submitted = self.target_job()
        paid = queue.claim("paid-worker", "test-pool")
        queue.begin_submission(paid.lease)
        queue.record_submitted(paid.lease, "original-paid-task")
        unsafe = self.target_job()
        bad = queue.claim("unsafe-worker", "test-pool")
        queue.begin_submission(bad.lease)
        queue.record_submitted(bad.lease, "old-upstream-task")
        queued, waiting, malformed = (
            self.target_job(),
            self.target_job(),
            self.target_job(),
        )
        other = self.job()
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == unsafe["id"])
                .values(status="queued", lease_worker_id=None, lease_expires_at=None)
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == waiting["id"])
                .values(status="waiting_capacity")
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == malformed["id"])
                .values(current_attempt_id=str(uuid.uuid4()))
            )
        self.service.patch(
            self.admin,
            self.ids["supervan"],
            {"status": "suspended", "reason": "pause account"},
            key="suspend",
        )
        for job in (claimed, queued, waiting):
            current = self.repo.get_job(self.other, job["id"])
            self.assertEqual(current["status"], "cancelled")
            self.assertEqual(
                current["cancellation"]["reason_code"], "account_suspended"
            )
        for job, state in (
            (submitted, "running"),
            (unsafe, "queued"),
            (malformed, "queued"),
        ):
            self.assertEqual(self.repo.get_job(self.other, job["id"])["status"], state)
        self.assertEqual(self.repo.get_job(self.scope, other["id"])["status"], "queued")
        self.assertEqual(
            self.repo.get_budget("target-budget")["reserved_microusd"], 300_000
        )
        self.assertEqual(self.repo.get_budget("target-budget")["spent_microusd"], 0)
        self.assertEqual(self.count(jobs), 7)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(attempts.c.upstream_task_id).where(
                        attempts.c.id == bad.lease.attempt_id
                    )
                ).scalar_one(),
                "old-upstream-task",
            )

    def test_suspension_handles_more_than_one_cancellation_batch(self):
        for _ in range(105):
            self.target_job(cost=1)
        self.service.patch(
            self.admin,
            self.ids["supervan"],
            {"status": "suspended", "reason": "bulk pause"},
        )
        self.assertEqual(self.repo.get_budget("target-budget")["reserved_microusd"], 0)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                set(connection.execute(select(jobs.c.status)).scalars()), {"cancelled"}
            )

    def test_cancellation_failure_rolls_back_account_audit_journal_and_money(self):
        first, second = self.target_job(), self.target_job()
        budget = self.repo.get_budget("target-budget")
        original = self.repo._cancel_locked
        calls = []

        def fail_after_first(connection, job, cancellation):
            calls.append(job["id"])
            if len(calls) == 2:
                raise RuntimeError("synthetic cancellation failure")
            return original(connection, job, cancellation)

        with patch.object(self.repo, "_cancel_locked", side_effect=fail_after_first):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                self.service.patch(
                    self.admin,
                    self.ids["supervan"],
                    {"status": "suspended", "reason": "atomic pause"},
                    key="failed-pause",
                )
        self.assertEqual(self.repo.get_budget("target-budget"), budget)
        self.assertEqual(self.count(admin_audit), 0)
        self.assertEqual(self.count(documents), 0)
        self.assertEqual(
            [
                self.repo.get_job(self.other, row["id"])["status"]
                for row in (first, second)
            ],
            ["queued", "queued"],
        )
        self.assertEqual(
            next(
                row
                for row in self.service.list_users(self.admin)["items"]
                if row["id"] == self.ids["supervan"]
            )["status"],
            "active",
        )

    def test_audit_combines_existing_financial_events_with_tenant_safe_projection(self):
        self.service.patch(
            self.admin, self.ids["supervan"], {"role": "admin", "reason": "review"}
        )
        with self.repo.transaction() as connection:
            for event, payload in (
                (
                    "operator.budget_limit_updated",
                    {
                        "tenant_id": self.scope.tenant_id,
                        "actor": {"owner_id": "superdan"},
                        "owner_id": "supervan",
                    },
                ),
                (
                    "operator.billing_applied",
                    {
                        "tenant_id": self.scope.tenant_id,
                        "actor_owner": "superdan",
                        "request": {
                            "action": "waive",
                            "owner_id": "supervan",
                            "reason": "Internal testing",
                            "evidence_note": "DO-NOT-EXPOSE",
                        },
                    },
                ),
                (
                    "operator.billing_applied",
                    {
                        "tenant_id": "other-tenant",
                        "actor_owner": "PRIVATE-OWNER",
                        "request": {"action": "settle", "owner_id": "PRIVATE-TARGET"},
                    },
                ),
                (
                    "job.billing_settled",
                    {"tenant_id": self.scope.tenant_id, "private": "DO-NOT-EXPOSE"},
                ),
            ):
                connection.execute(
                    insert(outbox).values(
                        id=str(uuid.uuid4()),
                        event_type=event,
                        aggregate_id="test",
                        payload=payload,
                        created_at=self.now,
                    )
                )
        seen, cursor = [], None
        for _ in range(4):
            page = self.service.audit(self.admin, cursor=cursor, limit=1)
            seen.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(
            {row["action"] for row in seen},
            {"role → admin", "budget limit changed", "waived"},
        )
        self.assertEqual(len(seen), len({row["id"] for row in seen}))
        self.assertNotIn("DO-NOT-EXPOSE", str(seen))
        self.assertNotIn("PRIVATE-", str(seen))
        cursor = self.service.audit(self.admin, limit=1)["next_cursor"]
        malformed = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        malformed["position"]["source"] = []
        cursor = (
            base64.urlsafe_b64encode(json.dumps(malformed).encode())
            .decode()
            .rstrip("=")
        )
        with self.assertRaisesRegex(AdminError, "cursor"):
            self.service.audit(self.admin, cursor=cursor)

    def test_http_contract_uses_details_and_blocks_admin_created_agent(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import COOKIE, create_app
        from studio_platform.settings import Settings

        app = create_app(
            Settings(
                Path(self.temp.name) / "api",
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
                database_url=self.url,
            ),
            repository=self.repo,
        )
        with TestClient(app) as client:
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            response = client.get("/v1/admin/users")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()), {"items", "next_cursor"})
            self.assertEqual(response.headers["cache-control"], "no-store")
            key = app.state.auth.create_key(
                "superdan",
                authenticated_session=app.state.auth.session(
                    client.cookies.get(COOKIE)
                ),
                name="admin-agent",
                scopes=["jobs:read"],
                all_projects=True,
            )["api_key"]
            self.assertEqual(
                client.get(
                    "/v1/admin/users", headers={"Authorization": "Bearer " + key}
                ).json(),
                {"detail": "admin_required"},
            )
            response = client.patch(
                "/v1/admin/users/" + self.ids["supervan"],
                headers={"Idempotency-Key": "ui-change"},
                json={"role": "admin", "reason": "Owner approved"},
            )
            self.assertEqual(
                (response.status_code, response.json()["role"]), (200, "admin")
            )
            response = client.patch(
                "/v1/admin/users/" + self.ids["supervan"],
                json={"role": [], "reason": "bad"},
            )
            self.assertEqual(response.status_code, 422)
            self.assertIn("detail", response.json())


if __name__ == "__main__":
    unittest.main()
