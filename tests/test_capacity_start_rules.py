"""Prospective rules preserve authorization, CAS and transactional audit."""

import copy
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from studio_platform.capacity_start_rules import StartRules, DEFAULTS
from studio_platform.dstack_demand_policy import read_runtime
from studio_platform.dstack_workspace import DstackWorkspace
from studio_platform.operator_capacity import OperatorError, operator_policy
from test_platform_repository import LedgerCase
import test_dstack_operator as fixtures
import test_dstack_demand_api as api_fixtures


class StartRulesTests(LedgerCase):
    def setUp(self):
        super().setUp()
        fixtures.DstackOperatorTests._setup_dstack(self)
        self.settings.operator_admin_owners = ("superdan",)
        self.workspace = DstackWorkspace(self.service)
        self.rules = StartRules(self.workspace)

    def body(self, revision=None, **changes):
        return {
            "profile_id": "5090-fl",
            "expected_revision": revision,
            "value": {**DEFAULTS, **changes},
        }

    def confirmed(self, body):
        preview = self.rules.preview(self.owner, body)
        return {
            **body,
            "confirmation_digest": preview["confirmation_digest"],
            "reason": "Prepare staging lookahead",
        }

    def counts(self):
        return api_fixtures.DemandAPITests.counts(self)

    def test_preview_and_defaults_do_not_write_or_call_capacity(self):
        before = self.counts()
        with patch.object(
            self.capacity, "plan", side_effect=AssertionError("no provider")
        ):
            read = self.rules.read(self.owner, "5090-fl")
            preview = self.rules.preview(self.owner, self.body(boot_seconds=700))
        self.assertEqual(read["value"], DEFAULTS)
        self.assertIsNone(read["revision"])
        self.assertFalse(preview["applied"])
        self.assertFalse(preview["execution_connected"])
        self.assertEqual(preview["keep_alive_seconds"], 700)
        self.assertEqual(self.counts(), before)

    def test_cas_confirmation_replay_history_and_demand_unchanged(self):
        before = self.counts()
        body = self.confirmed(self.body())
        first = self.rules.write(self.owner, body, "first")
        second = self.rules.write(
            self.owner,
            self.confirmed(self.body(first["revision"], minimum_jobs=25)),
            "second",
        )
        self.assertEqual(self.rules.write(self.owner, body, "first"), first)
        self.assertEqual(second["version"], 2)
        self.assertFalse(second["execution_connected"])
        self.assertEqual(self.counts(), (before[0] + 1, before[1] + 2, before[2] + 2))
        with self.assertRaisesRegex(OperatorError, "start_rules_conflict"):
            self.rules.write(self.owner, body, "stale")
        with self.assertRaisesRegex(OperatorError, "operator_idempotency_conflict"):
            self.rules.write(self.owner, {**body, "reason": "changed"}, "first")
        history = self.rules.history(self.owner, "5090-fl")["items"]
        self.assertEqual(len(history), 2)
        self.assertEqual({x["after"]["version"] for x in history}, {1, 2})
        self.assertTrue(all(x["reason"] == body["reason"] for x in history))
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                read_runtime(connection, self.scope.tenant_id),
                {"enabled": False, "revision": None},
            )
            self.assertTrue(
                all(
                    len(key) <= 30
                    for key in connection.scalars(select(operator_policy.c.id))
                )
            )

    def test_confirmation_binds_candidate_actor_and_config(self):
        body = self.confirmed(self.body())
        for changed in (
            {**body, "value": {**DEFAULTS, "boot_seconds": 700}},
            {**body, "confirmation_digest": "f" * 64},
        ):
            with self.assertRaisesRegex(
                OperatorError, "start_rules_confirmation_changed"
            ):
                self.rules.write(self.owner, changed, "changed")
        with patch.object(self.service, "fingerprint", "changed"):
            with self.assertRaisesRegex(
                OperatorError, "start_rules_confirmation_changed"
            ):
                self.rules.write(self.owner, body, "config")
        with self.assertRaisesRegex(OperatorError, "start_rules_confirmation_changed"):
            self.rules.write(
                replace(self.owner, actor_id="another-admin-session"), body, "actor"
            )

    def test_strict_values_unknown_profiles_and_privileged_calls(self):
        before = self.counts()
        for value in (True, 0, -1, 1.5, float("nan"), float("inf"), 86401):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(OperatorError, "start_rules_invalid"),
            ):
                self.rules.preview(self.owner, self.body(boot_seconds=value))
        with self.assertRaisesRegex(OperatorError, "start_rules_invalid"):
            self.rules.preview(self.owner, self.body(max_machines=10))
        with self.assertRaisesRegex(OperatorError, "start_rules_profile_invalid"):
            self.rules.preview(self.owner, {**self.body(), "profile_id": "missing"})
        for actor in (
            self.other_owner,
            replace(self.owner, machine=True, role="admin"),
        ):
            for call in (
                lambda: self.rules.read(actor, "5090-fl"),
                lambda: self.rules.preview(actor, self.body()),
                lambda: self.rules.history(actor, "5090-fl"),
                lambda: self.rules.write(actor, {}, "invalid"),
            ):
                with self.assertRaises(OperatorError):
                    call()
        self.assertEqual(self.counts(), before)

    def test_atomic_rollback_and_scoped_policy_history(self):
        body = self.confirmed(self.body())
        before = self.counts()
        with patch.object(
            self.workspace, "_command", side_effect=RuntimeError("fixture")
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                self.rules.write(self.owner, body, "rollback")
        self.assertEqual(before, self.counts())
        self.rules.write(self.owner, body, "ok")
        isolated = StartRules(self.workspace)
        isolated.tenant = "another-tenant"
        self.assertEqual(isolated.read(self.owner, "5090-fl")["version"], 0)
        self.assertEqual(isolated.history(self.owner, "5090-fl")["items"], [])
        profile = copy.deepcopy(self.service.config["profiles"][0])
        profile.update(id="other-config", configuration_id="another-configuration")
        self.service.config["profiles"].append(profile)
        self.assertEqual(self.rules.read(self.owner, "other-config")["version"], 0)
        self.assertEqual(self.rules.history(self.owner, "other-config")["items"], [])

    def test_fresh_demotion_blocks_original_replay(self):
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
        body = self.confirmed(self.body())
        self.rules.write(self.owner, body, "before-demotion")
        authorize = self.workspace._authorize

        def demote(*args, **kwargs):
            result = authorize(*args, **kwargs)
            with self.repo.transaction() as connection:
                connection.execute(update(users).values(role="user"))
            return result

        before = self.counts()
        with patch.object(self.workspace, "_authorize", side_effect=demote):
            with self.assertRaisesRegex(OperatorError, "admin_required"):
                self.rules.write(self.owner, body, "before-demotion")
        self.assertEqual(before, self.counts())

    def test_http_auth_csrf_preview_confirm_and_history(self):
        route = "/v1/operator/dstack/admin/start-rules"
        with TestClient(api_fixtures.DemandAPITests.app(self)) as client:
            self.assertEqual(
                client.get(route, params={"profile_id": "5090-fl"}).status_code, 401
            )
            client.post(
                "/api/auth/login", json={"username": "supervan"}
            ).raise_for_status()
            self.assertEqual(
                client.get(route, params={"profile_id": "5090-fl"}).status_code, 403
            )
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            preview = client.post(route + "/preview", json=self.body())
            self.assertEqual(preview.status_code, 200, preview.text)
            body = {
                **self.body(),
                "reason": "Staging preparation",
                "confirmation_digest": preview.json()["confirmation_digest"],
            }
            self.assertEqual(
                client.put(
                    route,
                    json=body,
                    headers={
                        "Idempotency-Key": "x",
                        "Origin": "https://untrusted.example",
                    },
                ).status_code,
                403,
            )
            saved = client.put(route, json=body, headers={"Idempotency-Key": "http"})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.headers["cache-control"], "no-store")
            self.assertFalse(saved.json()["applied"])
            history = client.get(route + "/history", params={"profile_id": "5090-fl"})
            self.assertEqual(len(history.json()["items"]), 1)
