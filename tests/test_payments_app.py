"""Billing paths retain the application's real authentication boundary."""

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from studio_platform.api import create_app
from studio_platform.payment_schema import TABLE_NAMES
from studio_platform.settings import Settings
from studio_platform.stripe_client import MAX_BODY
from test_platform_repository import LedgerCase


class PaymentApplicationTests(LedgerCase):
    def settings(self):
        return Settings(
            Path(self.temp.name) / "api",
            database_url=self.url,
            tenant_id=self.scope.tenant_id,
            deployment_target="staging",
            public_origin="https://staging.inkseq.com",
        )

    def test_only_packs_and_signed_webhook_enter_the_public_boundary(self):
        app = create_app(self.settings(), repository=self.repo)
        before = set(inspect(self.repo.engine).get_table_names())
        self.assertFalse(TABLE_NAMES & before)
        with TestClient(app, base_url="https://staging.inkseq.com") as client:
            packs = client.get("/v1/billing/packs")
            self.assertEqual(packs.status_code, 200)
            self.assertEqual(packs.json(), {"packs": []})
            self.assertEqual(client.get("/v1/billing/payments").status_code, 401)
            self.assertEqual(
                client.post(
                    "/v1/billing/checkout", json={"pack_id": "starter"}
                ).status_code,
                401,
            )
            self.assertEqual(
                client.post("/v1/billing/stripe/webhook", content=b"{}").status_code,
                503,
            )  # No test secret configured: never acknowledge an unverified event.
            self.assertEqual(
                client.post(
                    "/v1/billing/stripe/webhook",
                    content=b"x" * (MAX_BODY + 1),
                ).status_code,
                413,
            )
        after = set(inspect(self.repo.engine).get_table_names())
        self.assertEqual(before, after)
        self.assertFalse(TABLE_NAMES & after)

    def test_worker_boot_reaches_its_own_token_check_without_a_session(self):
        app = create_app(self.settings(), repository=self.repo)
        with TestClient(app, base_url="https://staging.inkseq.com") as client:
            result = client.post(
                "/v1/worker/boot", json={"intent_id": "x", "token": "y"}
            )
            # The route's own refusal, not the login wall's 401.
            self.assertEqual(
                (result.status_code, result.json()),
                (403, {"error": "worker_boot_denied"}),
            )
            self.assertEqual(client.get("/v1/worker/boot").status_code, 401)

    def test_production_refuses_payment_processing_without_creating_schema(self):
        settings = replace(
            self.settings(),
            deployment_target="production",
            public_origin="https://inkseq.com",
        )
        app = create_app(settings, repository=self.repo)
        with TestClient(app, base_url="https://inkseq.com") as client:
            result = client.post("/v1/billing/stripe/webhook", content=b"{}")
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.json()["code"], "billing_unavailable")
        self.assertFalse(TABLE_NAMES & set(inspect(self.repo.engine).get_table_names()))
