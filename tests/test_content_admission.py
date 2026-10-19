"""New policy checks preserve accepted originals and never leak a money hold."""

import unittest
import uuid

from sqlalchemy import insert, select, update

from studio_platform.content_policy import (
    ContentPolicyError,
    check_input,
    require_generation_terms,
)
from studio_platform.identity_schema import users
from studio_platform.repository import documents
import test_credit_admission as fixtures


class ContentAdmissionTests(fixtures.CreditAdmissionTests):
    def set_policy(self, *, version="terms-v1", status="reviewed", keywords=()):
        value = {
            "revision": str(uuid.uuid4()),
            "version": version,
            "status": status,
            "moderation": {
                "enabled": bool(keywords),
                "keywords": list(keywords),
                "output_classifier": None,
            },
        }
        where = (
            documents.c.tenant_id == self.scope.tenant_id,
            documents.c.owner_id == "__content_policy",
            documents.c.project_id == "__content_policy",
            documents.c.kind == "content_policy",
            documents.c.document_id == "active",
        )
        with self.repo.transaction() as connection:
            old = connection.execute(
                select(documents.c.version).where(*where)
            ).scalar_one_or_none()
            if old is None:
                connection.execute(
                    insert(documents).values(
                        tenant_id=self.scope.tenant_id,
                        owner_id="__content_policy",
                        project_id="__content_policy",
                        kind="content_policy",
                        document_id="active",
                        version=1,
                        payload=value,
                        updated_at=self.now,
                    )
                )
            else:
                connection.execute(
                    update(documents)
                    .where(*where)
                    .values(version=old + 1, payload=value, updated_at=self.now)
                )
        return value

    def accept_fixture(self, version="terms-v1"):
        # Synthetic setup only. Live acceptance uses the human-only account API.
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == self.user_id)
                .values(terms_version=version, terms_accepted_at=self.now - 1)
            )

    def frozen_plan(self):
        original = self.plan()
        compiled, execution = original["request"], original["execution_plan"]
        receipt = check_input(
            self.repo, self.scope.tenant_id, self.scope.owner_id, compiled
        )
        if receipt is not None:
            compiled["content_moderation"] = receipt
        with self.repo.engine.connect() as connection:
            terms = require_generation_terms(
                connection,
                self.scope.tenant_id,
                self.scope.owner_id,
                execution,
                compiled,
            )
        if terms is not None:
            execution["terms_acceptance"] = terms
        return self.repo.create_plan(
            self.scope,
            compiled,
            execution,
            expires_at=original["expires_at"],
            estimated_cost_microusd=original["estimated_cost_microusd"],
        )

    def prepare_wallet(self):
        self.seed(100)
        self.policy()

    def test_terms_refuse_new_paid_work_before_any_credit_or_supplier_hold(self):
        self.prepare_wallet()
        self.set_policy()
        with self.assertRaises(ContentPolicyError):
            self.admit(self.plan())
        self.assert_no_supplier_admission()
        self.assertEqual(self.balance()["balance"], 100)
        self.assertEqual(self.balance()["held"], 0)
        self.assertEqual(self.entry_counts(), {"grant": 1})

    def test_terms_bump_preserves_original_replay_and_one_hold(self):
        self.prepare_wallet()
        self.set_policy()
        self.accept_fixture()
        plan = self.frozen_plan()
        original = self.admit(plan, key="original-before-policy-bump")
        self.set_policy(version="terms-v2")
        replay = self.admit(plan, key="original-before-policy-bump")
        self.assertEqual(replay["id"], original["id"])
        self.assertFalse(replay["created"])
        with self.assertRaises(ContentPolicyError):
            self.admit(plan, key="new-after-policy-bump")
        self.assertEqual(self.balance()["held"], 20)
        self.assertEqual(self.entry_counts(), {"grant": 1, "hold": 1})
        self.assert_invariants()

    def test_first_enqueue_checks_current_terms_without_changing_inert_job(self):
        self.prepare_wallet()
        self.set_policy()
        self.accept_fixture()
        plan = self.frozen_plan()
        original = self.admit(plan, status="planned")
        self.set_policy(version="terms-v2")
        with self.assertRaises(ContentPolicyError):
            self.repo.enqueue(
                self.scope,
                original["id"],
                budget_account_ids=("owner-budget",),
                spending_actor="agent:original-owner",
            )
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "planned"
        )
        self.assert_no_supplier_admission()
        self.assertEqual(self.balance()["held"], 0)

    def test_policy_change_cannot_admit_an_old_unsigned_prompt(self):
        self.prepare_wallet()
        plan = self.plan()
        # The old plan was created while moderation was OFF, not grandfathered
        # as an accepted job. Rechecking a new admission must not create a hold.
        self.set_policy(status="draft", keywords=("Synthetic original",))
        with self.assertRaises(ContentPolicyError):
            self.admit(plan)
        self.assert_no_supplier_admission()
        self.assertEqual(self.balance()["held"], 0)

    def test_moderation_diagnostics_are_specific_without_exposing_input_text(self):
        from studio_platform.generation_diagnostics import (
            preflight_diagnostic,
            safe_failure_summary,
        )

        diagnostic = preflight_diagnostic(
            ContentPolicyError("content_moderation_refused", 422)
        )
        self.assertEqual(diagnostic["error_code"], "content_moderation_refused")
        self.assertEqual(diagnostic["error_stage"], "moderation")
        self.assertIn("en", diagnostic["messages"])
        unknown = preflight_diagnostic(ValueError("synthetic-private-prompt"))
        self.assertNotIn("synthetic-private-prompt", str(unknown))
        job = {
            "request": {},
            "execution_plan": {},
            "error_code": "content_moderation_refused",
        }
        summary = safe_failure_summary(job)
        self.assertEqual(summary["reason_code"], "content_moderation_refused")
        self.assertIn("No generation credits", summary["messages"]["en"])

    def test_real_http_boundary_keeps_only_legal_metadata_public(self):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        settings = Settings(
            Path(self.temp.name) / "api",
            database_url=self.url,
            tenant_id=self.scope.tenant_id,
            deployment_target="staging",
            public_origin="https://staging.inkseq.com",
        )
        app = create_app(settings, repository=self.repo)
        with TestClient(app, base_url=settings.public_origin) as client:
            public = client.get("/v1/legal/policy")
            self.assertEqual(public.status_code, 200)
            self.assertEqual(public.json()["status"], "draft")
            self.assertNotIn("available_classifiers", public.json())
            self.assertNotIn("keywords", public.json()["moderation"])
            for method, path in (
                ("POST", "/v1/account/terms"),
                ("GET", "/v1/admin/content-policy"),
                ("POST", "/v1/admin/content-policy"),
            ):
                with self.subTest(method=method, path=path):
                    result = client.request(method, path, json={"version": "ignored"})
                    self.assertEqual(result.status_code, 401)
        self.assertEqual(self.entry_counts(), {})


def load_tests(_loader, _tests, _pattern):
    # Reuse the existing setup without rerunning its inherited acceptance cases.
    return unittest.TestSuite(
        ContentAdmissionTests(name)
        for name in ContentAdmissionTests.__dict__
        if name.startswith("test_")
    )
