"""Demand switch authorization, atomic audit/replay and owned creator readback."""

import copy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from studio_platform.dstack_demand_policy import policy_key, read_runtime
from studio_platform.dstack_workspace import DstackWorkspace
from studio_platform.operator_capacity import (
    OperatorError,
    operator_commands,
    operator_policy,
)
from studio_platform.repository import jobs, outbox
from test_platform_repository import LedgerCase
import test_dstack_operator as fixtures
import test_dstack_workspace as workspace_fixtures


class DemandAPITests(LedgerCase):
    def setUp(self):
        super().setUp()
        fixtures.DstackOperatorTests._setup_dstack(self)
        self.settings.operator_admin_owners = ("superdan",)
        self.workspace = DstackWorkspace(self.service)

    def body(self, *, enabled=True, revision=None):
        return {
            "enabled": enabled,
            "expected_revision": revision,
            "reason": "Bounded staging cold-start acceptance",
        }

    def counts(self):
        with self.repo.engine.connect() as connection:
            return tuple(
                connection.scalar(select(func.count()).select_from(table))
                for table in (operator_policy, operator_commands, outbox)
            )

    def app(self):
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        settings = Settings(
            Path(self.temp.name) / "demand-api",
            tenant_id=self.scope.tenant_id,
            auth_mode="local-test",
            database_url=self.url,
            operator_capacity_owners=("superdan", "supervan"),
            operator_admin_owners=("superdan",),
        )
        self.client.close = lambda: None
        return create_app(settings, repository=self.repo, dstack_operator=self.service)

    def test_absent_runtime_off_in_each_tenant_and_read_never_writes(self):
        before = self.counts()
        with self.repo.engine.connect() as connection:
            for tenant in (self.scope.tenant_id, "other-tenant"):
                self.assertEqual(
                    read_runtime(connection, tenant),
                    {"enabled": False, "revision": None},
                )
        self.assertEqual(self.counts(), before)

    def test_cas_idempotency_and_audit_preserve_original_replay(self):
        before = self.counts()
        body = self.body()
        first = self.workspace.set_demand_start(self.owner, body, "switch-on")
        self.assertEqual(set(first), {"enabled", "revision"})
        self.assertTrue(first["enabled"])
        self.assertEqual(len(first["revision"]), 64)
        second = self.workspace.set_demand_start(
            self.owner,
            self.body(enabled=False, revision=first["revision"]),
            "switch-off",
        )
        self.assertFalse(second["enabled"])
        self.assertNotEqual(second["revision"], first["revision"])
        self.assertEqual(
            self.workspace.set_demand_start(self.owner, body, "switch-on"), first
        )
        self.assertEqual(self.counts(), (before[0] + 1, before[1] + 2, before[2] + 2))
        with self.assertRaisesRegex(OperatorError, "operator_idempotency_conflict"):
            self.workspace.set_demand_start(
                self.owner, self.body(enabled=False), "switch-on"
            )
        with self.assertRaisesRegex(OperatorError, "dstack_demand_policy_conflict"):
            self.workspace.set_demand_start(self.owner, body, "stale-cas")
        with self.repo.engine.connect() as connection:
            self.assertEqual(read_runtime(connection, self.scope.tenant_id), second)
            self.assertEqual(
                read_runtime(connection, "other-tenant"),
                {"enabled": False, "revision": None},
            )
            audits = (
                connection.execute(
                    select(outbox).where(
                        outbox.c.event_type == "operator.demand_start_updated"
                    )
                )
                .mappings()
                .all()
            )
            self.assertEqual(len(audits), 2)
            first_audit = next(row for row in audits if row["payload"]["new"] == first)
            self.assertEqual(
                first_audit["payload"]["old"], {"enabled": False, "revision": None}
            )
            self.assertEqual(first_audit["payload"]["reason"], body["reason"])
            self.assertEqual(first_audit["payload"]["actor_owner"], self.owner.owner)

    def test_invalid_body_and_nonhuman_or_nonadmin_never_create_policy(self):
        before = self.counts()
        for body in (
            {**self.body(), "extra": True},
            {**self.body(), "enabled": 1},
            {**self.body(), "reason": " "},
            {**self.body(), "reason": "unsafe\nreason"},
            {**self.body(), "expected_revision": "not-a-revision"},
        ):
            with self.subTest(body=body):
                with self.assertRaisesRegex(
                    OperatorError, "dstack_demand_policy_invalid"
                ):
                    self.workspace.set_demand_start(self.owner, body, "invalid")
        for principal in (
            self.other_owner,
            replace(self.owner, machine=True, role="admin"),
        ):
            with self.assertRaises(OperatorError):
                self.workspace.set_demand_start(principal, self.body(), "unauthorized")
        self.assertEqual(self.counts(), before)

    def test_fresh_canonical_admin_check_prevents_demotion_before_write_or_replay(self):
        from studio_platform.identity_schema import users, admin_audit

        users.create(self.repo.engine)
        admin_audit.create(self.repo.engine)
        with self.repo.transaction() as connection:
            connection.execute(
                insert(users).values(
                    id="00000000-0000-4000-8000-000000000001",
                    tenant=self.scope.tenant_id,
                    owner=self.owner.owner,
                    display_name="Admin",
                    role="admin",
                    status="active",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        body = self.body()
        self.workspace.set_demand_start(self.owner, body, "before-demotion")
        before = self.counts()
        original = self.workspace._authorize

        def demote(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.repo.transaction() as connection:
                connection.execute(update(users).values(role="user"))
            return result

        with patch.object(self.workspace, "_authorize", side_effect=demote):
            with self.assertRaisesRegex(OperatorError, "admin_required"):
                self.workspace.set_demand_start(self.owner, body, "before-demotion")
        self.assertEqual(self.counts(), before)

    def test_failed_command_rolls_back_policy_and_audit_together(self):
        before = self.counts()
        with patch.object(
            self.workspace, "_command", side_effect=RuntimeError("fixture")
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                self.workspace.set_demand_start(self.owner, self.body(), "rollback")
        self.assertEqual(self.counts(), before)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                read_runtime(connection, self.scope.tenant_id),
                {"enabled": False, "revision": None},
            )

    def test_concurrent_null_cas_accepts_only_one_switch(self):
        def change(number):
            try:
                return self.workspace.set_demand_start(
                    self.owner, self.body(), "concurrent-" + str(number)
                )
            except OperatorError as error:
                return error.code

        results = self.parallel(change, count=4)
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(results.count("dstack_demand_policy_conflict"), 3)

    def test_corrupt_or_cross_tenant_runtime_fails_closed_without_repair(self):
        value = self.workspace.set_demand_start(self.owner, self.body(), "original")
        for payload in (
            {"tenant_id": "other-tenant", "enabled": True},
            {"tenant_id": self.scope.tenant_id, "enabled": 1},
            {"tenant_id": self.scope.tenant_id, "enabled": True, "extra": 1},
        ):
            with self.repo.transaction() as connection:
                connection.execute(
                    update(operator_policy)
                    .where(operator_policy.c.id == policy_key(self.scope.tenant_id))
                    .values(payload=payload)
                )
            with self.repo.engine.connect() as connection:
                with self.assertRaisesRegex(
                    OperatorError, "dstack_demand_policy_unavailable"
                ):
                    read_runtime(connection, self.scope.tenant_id)
            with self.assertRaisesRegex(
                OperatorError, "dstack_demand_policy_unavailable"
            ):
                self.workspace.set_demand_start(
                    self.owner, self.body(revision=value["revision"]), "no-repair"
                )

    def test_http_admin_only_summary_csrf_cas_and_replay(self):
        route = "/v1/operator/dstack/admin/demand-start"
        with TestClient(self.app()) as client:
            self.assertEqual(client.get(route).status_code, 401)
            client.post(
                "/api/auth/login", json={"username": "supervan"}
            ).raise_for_status()
            self.assertEqual(client.get(route).status_code, 403)
            self.assertEqual(
                client.put(
                    route, json=self.body(), headers={"Idempotency-Key": "denied"}
                ).status_code,
                403,
            )
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            summary = client.get(route)
            self.assertEqual(summary.status_code, 200, summary.text)
            self.assertEqual(summary.headers["cache-control"], "no-store")
            self.assertEqual(
                summary.json(),
                {
                    "enabled": False,
                    "revision": None,
                    "hard_max_machines": 2,
                    "running_machines": 0,
                    "starting_machines": 0,
                    "uncertain_machines": 0,
                    "queued_generation_jobs": 0,
                    "queued_by_mode": {"fl": 0, "ref": 0},
                    "blocked_reason": "disabled",
                    "last_start_blockers": {},
                },
            )
            self.assertEqual(client.put(route, json=self.body()).status_code, 422)
            self.assertEqual(
                client.put(
                    route,
                    json=self.body(),
                    headers={
                        "Idempotency-Key": "csrf",
                        "Origin": "https://untrusted.example",
                    },
                ).status_code,
                403,
            )
            headers = {"Idempotency-Key": "http-on"}
            response = client.put(route, json=self.body(), headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                client.put(route, json=self.body(), headers=headers).json(),
                response.json(),
            )
            self.assertEqual(
                client.get(route).json()["revision"], response.json()["revision"]
            )
            workspace = client.get("/v1/operator/dstack/admin/workspace")
            self.assertEqual(workspace.status_code, 200, workspace.text)
            self.assertEqual(workspace.json()["demand_start"], client.get(route).json())
            self.assertNotIn(
                "demand_start", client.get("/v1/operator/dstack/workspace").json()
            )

    def test_creator_stage_read_authorizes_owner_and_preserves_accepted_job(self):
        job = workspace_fixtures.WorkspaceTests.waiting_job(
            self, owner=self.other_owner
        )
        from studio_platform.repository import Scope

        self.repo.put_document(
            Scope(self.other.tenant_id, self.other.owner_id, "__projects"),
            "project",
            job["project_id"],
            {"id": job["project_id"], "schemaVersion": 4, "entities": [], "links": []},
        )
        with TestClient(self.app()) as client:
            client.post(
                "/api/auth/login", json={"username": "supervan"}
            ).raise_for_status()
            for status in ("queued", "waiting_capacity"):
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(jobs).where(jobs.c.id == job["id"]).values(status=status)
                    )
                before = copy.deepcopy(self.repo.get_job(self.other, job["id"]))
                response = client.get("/v1/jobs/" + job["id"])
                self.assertEqual(response.status_code, 200, response.text)
                visible = response.json()
                self.assertEqual(visible["status"], status)
                self.assertEqual(visible["phase"], status)
                self.assertEqual(
                    visible["wait_diagnostic"]["stage"], "queued_no_machine"
                )
                self.assertTrue(visible["wait_diagnostic"]["advisory_only"])
                self.assertNotIn("provider", visible["wait_diagnostic"])
                self.assertNotIn("model", visible["wait_diagnostic"])
                self.assertNotIn("microusd", str(visible["wait_diagnostic"]))
                self.assertEqual(self.repo.get_job(self.other, job["id"]), before)
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            with patch("studio_platform.dstack_demand.job_wait_stage") as stage:
                self.assertEqual(client.get("/v1/jobs/" + job["id"]).status_code, 404)
                stage.assert_not_called()
        self.assertEqual(self.repo.get_job(self.other, job["id"]), before)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(jobs)), 1
            )
