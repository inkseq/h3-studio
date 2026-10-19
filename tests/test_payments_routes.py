"""Human ownership, pack CAS/audit and webhook admission boundaries."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, func, insert, select, update

from studio_platform.auth import Principal
from studio_platform.identity_schema import admin_audit, metadata, users
from studio_platform.credit_schema import metadata as credit_metadata
from studio_platform.payment_schema import metadata as payment_metadata, payments
from studio_platform.schema_version import PAYMENT_REVISION
from studio_platform.payments_routes import BillingError, PaymentAPI, register_routes
from studio_platform.repository import budget_accounts
from studio_platform.stripe_client import StripeGateway, StripeGatewayError
from test_platform_repository import LedgerCase


PACK = {
    "id": "starter",
    "price_id": "price_teststarter",
    "credits": 500,
    "amount_cents": 500,
    "currency": "usd",
    "bonus_percent": 0,
}


class PaymentRouteTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ids = {
            owner: str(uuid.uuid4()) for owner in ("superdan", "supervan", "other")
        }
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            credit_metadata.create_all(connection)
            payment_metadata.create_all(connection)
            marker = Table(
                "alembic_version",
                MetaData(),
                Column("version_num", String(32), primary_key=True),
            )
            marker.create(connection)
            connection.execute(insert(marker).values(version_num=PAYMENT_REVISION))
            for owner, ident in self.ids.items():
                connection.execute(
                    insert(users).values(
                        id=ident,
                        tenant="sixnine",
                        owner=owner,
                        role="admin" if owner == "superdan" else "user",
                        status="active",
                        display_name=owner,
                        created_at=self.now,
                        updated_at=self.now,
                        terms_version="reviewed-fixture-v1",
                        terms_accepted_at=self.now,
                    )
                )
        self.settings = SimpleNamespace(tenant_id="sixnine", stripe_mode="off")
        self.admin = Principal("superdan", "browser:superdan")
        self.user = Principal("supervan", "browser:supervan")
        self.core, self.gateway = Mock(), Mock()
        self.gateway.configuration.return_value = SimpleNamespace(
            account_id="acct_testaccount"
        )
        self.terms = Mock(
            return_value={"version": "reviewed-fixture-v1", "accepted_at": self.now}
        )
        self.api = PaymentAPI(
            self.repo,
            self.settings,
            core=self.core,
            gateway=self.gateway,
            terms_check=self.terms,
        )

    def save(self, **changes):
        body = {
            "expected_revision": None,
            "packs": [dict(PACK)],
            "reason": "Disposable test catalog",
            **changes,
        }
        return self.api.set_packs(self.admin, body, key="catalog-original")

    def test_public_catalog_defaults_empty_and_never_exposes_provider_ids(self):
        self.assertEqual(self.api.packs(), {"packs": []})
        saved = self.save()
        self.assertEqual(
            self.api.packs()["packs"],
            [{key: value for key, value in PACK.items() if key != "price_id"}],
        )
        self.assertEqual(self.api.admin_packs(self.admin), saved)
        self.gateway.assert_not_called()

    def test_canonical_admin_not_principal_role_or_machine_can_edit(self):
        for actor in (
            replace(self.user, role="admin"),
            replace(self.admin, machine=True),
        ):
            with self.subTest(actor=actor), self.assertRaises(BillingError) as caught:
                self.api.set_packs(
                    actor,
                    {"expected_revision": None, "packs": [], "reason": "Forbidden"},
                    key="denied",
                )
            self.assertEqual(caught.exception.status, 403)
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.owner == "superdan")
                .values(status="suspended")
            )
        with self.assertRaises(BillingError):
            self.save()

    def test_admin_catalog_cas_idempotency_audit_and_usd_unchanged(self):
        with self.repo.engine.connect() as connection:
            before = [
                dict(row)
                for row in connection.execute(select(budget_accounts)).mappings()
            ]
        saved = self.save()
        self.assertEqual(self.save(), saved)
        with self.assertRaisesRegex(BillingError, "idempotency_conflict"):
            self.save(packs=[])
        with self.assertRaisesRegex(BillingError, "billing_packs_changed"):
            self.api.set_packs(
                self.admin,
                {"expected_revision": None, "packs": [], "reason": "Stale editor"},
                key="other-key",
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(admin_audit)
                ).scalar_one(),
                1,
            )
            self.assertEqual(
                [
                    dict(row)
                    for row in connection.execute(select(budget_accounts)).mappings()
                ],
                before,
            )

    def test_pack_validation_rejects_injected_or_inconsistent_catalog(self):
        for pack in (
            {**PACK, "secret_key": "do-not-store"},
            {**PACK, "credits": True},
            {**PACK, "currency": "aud"},
            {**PACK, "amount_cents": 499},
            {**PACK, "price_id": "http://wrong"},
        ):
            with self.subTest(pack=pack), self.assertRaises(BillingError):
                self.save(packs=[pack])
        with self.assertRaises(BillingError):
            self.save(packs=[PACK, dict(PACK)])

    def test_missing_reviewed_terms_and_agent_never_dispatch(self):
        self.save()
        self.api.terms_check = None
        with self.assertRaisesRegex(BillingError, "billing_terms_unavailable"):
            self.api.checkout(self.user, {"pack_id": "starter"}, key="buy")
        with self.assertRaises(BillingError) as caught:
            self.api.checkout(
                replace(self.user, machine=True),
                {"pack_id": "starter"},
                key="agent-buy",
            )
        self.assertEqual(caught.exception.status, 403)
        self.core.begin_checkout.assert_not_called()
        self.gateway.checkout.assert_not_called()

    def test_checkout_requires_key_and_uses_server_pack_only(self):
        saved = self.save()
        for body, key in (
            ({"pack_id": "starter"}, None),
            ({"pack_id": "starter", "credits": 999999}, "injected"),
        ):
            with self.subTest(body=body), self.assertRaises(BillingError):
                self.api.checkout(self.user, body, key=key)
        self.core.begin_checkout.return_value = {
            "payment": {"id": str(uuid.uuid4()), "request_fingerprint": "f" * 64},
            "dispatch": True,
            "stripe_idempotency_key": "inkseq-original",
        }
        self.gateway.checkout.return_value = {"closed": "provider-proof"}
        self.core.bind_checkout.return_value = {
            "checkout_url": "https://checkout.stripe.com/c/pay/original"
        }
        self.assertEqual(
            self.api.checkout(self.user, {"pack_id": "starter"}, key="buy"),
            {"url": "https://checkout.stripe.com/c/pay/original"},
        )
        self.core.begin_checkout.assert_called_once_with(
            self.user,
            {**PACK, "revision": saved["revision"]},
            "buy",
            account_scope="acct_testaccount",
        )
        self.gateway.checkout.assert_called_once_with(
            self.core.begin_checkout.return_value["payment"],
            idempotency_key="inkseq-original",
        )

    def test_original_bound_checkout_replay_has_no_second_provider_request(self):
        self.save()
        self.core.begin_checkout.return_value = {
            "payment": {"checkout_url": "https://checkout.stripe.com/c/pay/original"},
            "dispatch": False,
        }
        self.assertIn(
            "url", self.api.checkout(self.user, {"pack_id": "starter"}, key="same")
        )
        self.gateway.checkout.assert_not_called()

    def test_unknown_provider_outcome_does_not_bind_or_invent_success(self):
        self.save()
        payment_id = str(uuid.uuid4())
        self.core.begin_checkout.return_value = {
            "payment": {"id": payment_id, "request_fingerprint": "f" * 64},
            "dispatch": True,
            "stripe_idempotency_key": "inkseq-original",
        }
        self.gateway.checkout.side_effect = StripeGatewayError(
            "billing_checkout_unknown", unknown=True
        )
        with self.assertRaises(StripeGatewayError):
            self.api.checkout(self.user, {"pack_id": "starter"}, key="buy")
        self.core.bind_checkout.assert_not_called()

    def test_webhook_bad_signature_rejects_before_core_persistence(self):
        root = Path(self.temp.name)
        config = {
            "version": 1,
            "account_id": "acct_testaccount",
            "secret_key": "sk_test_" + "s" * 32,
            "publishable_key": "pk_test_" + "p" * 32,
            "webhook_secret": "whsec_" + "w" * 32,
        }
        path = root / "stripe.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        settings = SimpleNamespace(
            **{
                **vars(self.settings),
                "stripe_config_file": path,
                "deployment_target": "staging",
                "public_origin": "https://staging.inkseq.com",
                "public_origin_aliases": (),
            }
        )
        app = FastAPI()
        # The transport/core is injected; the real signing boundary remains in use.
        register_routes(
            app,
            settings,
            self.repo,
            lambda _request: self.user,
            core=self.core,
            gateway=StripeGateway(settings),
            terms_check=self.terms,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/billing/stripe/webhook",
                content=b"not JSON",
                headers={"Stripe-Signature": "t=1,v1=bad"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["code"], "billing_signature_invalid")
            self.assertEqual(client.get("/v1/billing/packs").json(), {"packs": []})
            response = client.post(
                "/v1/billing/stripe/webhook", content=b"x" * (512 * 1024 + 1)
            )
            self.assertEqual(response.status_code, 413)
        self.core.receive_event.assert_not_called()

    def test_supported_verified_event_is_only_persisted_not_processed_inline(self):
        event = {"id": "evt_original", "object": {"session_id": "cs_test_original"}}
        self.gateway.verify_event.return_value = event
        self.assertEqual(
            self.api.receive_webhook(b"raw", "signature"), {"received": True}
        )
        self.core.receive_event.assert_called_once_with(event)
        self.core.process_event.assert_not_called()

    def real_core(self):
        from studio_platform.payments import PaymentCore

        settings = SimpleNamespace(
            **{
                **vars(self.settings),
                "deployment_target": "staging",
                "public_origin": "https://staging.inkseq.com",
                "public_origin_aliases": (),
            }
        )
        return PaymentCore(self.repo, settings, terms_check=self.terms)

    def test_owned_history_pages_never_include_foreign_payment_or_cursor(self):
        core = self.real_core()
        pack = {**PACK, "revision": "catalog-fixture-v1"}
        own = [
            core.begin_checkout(self.user, pack, key, account_scope="acct_testaccount")[
                "payment"
            ]["id"]
            for key in ("first", "second")
        ]
        foreign = core.begin_checkout(
            Principal("other", "browser:other"),
            pack,
            "foreign",
            account_scope="acct_testaccount",
        )["payment"]["id"]
        with self.repo.transaction() as connection:
            connection.execute(
                update(payments)
                .where(payments.c.id == own[0])
                .values(
                    receipt_url="https://legacy-unsupported.example.test/private-receipt"
                )
            )
            connection.execute(
                update(payments)
                .where(payments.c.id == own[1])
                .values(receipt_url="https://pay.stripe.com/receipts/test-receipt")
            )
        with self.assertLogs(
            "studio_platform.stripe_client", level="WARNING"
        ) as logged:
            first = self.api.history(self.user, limit=1)
            second = self.api.history(self.user, before=first["next_cursor"], limit=1)
        self.assertEqual(
            logged.output,
            ["WARNING:studio_platform.stripe_client:billing_receipt_url_omitted"],
        )
        projected = {row["id"]: row for row in first["payments"] + second["payments"]}
        self.assertIsNone(projected[own[0]]["receipt_url"])
        self.assertEqual(
            projected[own[1]]["receipt_url"],
            "https://pay.stripe.com/receipts/test-receipt",
        )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(payments.c.receipt_url).where(payments.c.id == own[0])
                ).scalar_one(),
                "https://legacy-unsupported.example.test/private-receipt",
            )
        self.assertEqual(
            {first["payments"][0]["id"], second["payments"][0]["id"]}, set(own)
        )
        self.assertIsNone(second["next_cursor"])
        self.assertNotIn("account_scope", first["payments"][0])
        self.assertNotIn("stripe_payment_intent_id", first["payments"][0])
        with self.assertRaisesRegex(BillingError, "billing_cursor_invalid"):
            self.api.history(self.user, before=foreign)
        with self.assertRaises(BillingError):
            self.api.history(replace(self.user, machine=True))

    def test_original_checkout_survives_admin_removing_pack(self):
        saved = self.save()
        core = self.real_core()
        prepared = core.begin_checkout(
            self.user,
            {**PACK, "revision": saved["revision"]},
            "original-purchase",
            account_scope="acct_testaccount",
        )
        payment = prepared["payment"]
        core.bind_checkout(
            payment["id"],
            payment["request_fingerprint"],
            {
                "account_scope": "acct_testaccount",
                "livemode": False,
                "object": {
                    "payment_id": payment["id"],
                    "user_id": payment["user_id"],
                    "pack_id": payment["pack_id"],
                    "pack_revision": payment["pack_revision"],
                    "credits": payment["credits"],
                    "price_id": payment["price_id"],
                    "amount_cents": payment["amount_cents"],
                    "currency": "usd",
                    "session_id": "cs_test_original",
                    "payment_status": "unpaid",
                    "checkout_url": "https://checkout.stripe.com/c/pay/original",
                    "expires_at": self.now + 600,
                },
            },
        )
        self.api.set_packs(
            self.admin,
            {
                "expected_revision": saved["revision"],
                "packs": [],
                "reason": "Hide pack from new purchases",
            },
            key="remove-pack",
        )
        self.api.core = core
        self.assertEqual(
            self.api.checkout(
                self.user, {"pack_id": PACK["id"]}, key="original-purchase"
            ),
            {"url": "https://checkout.stripe.com/c/pay/original"},
        )
        self.gateway.checkout.assert_not_called()

    def test_unsupported_optional_receipt_does_not_block_paid_or_refund_settlement(
        self,
    ):
        import stripe
        from studio_platform.credit_schema import accounts, entries

        core = self.real_core()
        prepared = core.begin_checkout(
            self.user,
            {**PACK, "revision": "fixture-v1"},
            "receipt-original",
            account_scope="acct_testaccount",
        )
        row = prepared["payment"]
        config = {
            "version": 1,
            "account_id": "acct_testaccount",
            "secret_key": "sk_test_" + "s" * 32,
            "publishable_key": "pk_test_" + "p" * 32,
            "webhook_secret": "whsec_" + "w" * 32,
        }
        path = Path(self.temp.name) / "stripe-receipt.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        settings = SimpleNamespace(
            **{
                **vars(self.settings),
                "stripe_config_file": path,
                "deployment_target": "staging",
                "public_origin": "https://staging.inkseq.com",
                "public_origin_aliases": (),
            }
        )
        provider = Mock()
        gateway = StripeGateway(settings, client_factory=lambda _config: provider)
        self.repo.generation_transport = SimpleNamespace(payment_event=Mock())
        metadata = {
            "payment_id": row["id"],
            "user_id": row["user_id"],
            "pack_id": row["pack_id"],
            "pack_revision": row["pack_revision"],
            "credits": str(row["credits"]),
        }
        charge = {
            "object": "charge",
            "id": "ch_optional",
            "livemode": False,
            "amount": 500,
            "currency": "usd",
            "payment_intent": "pi_optional",
            "receipt_url": "https://unknown.example.test/private-receipt",
            "metadata": metadata,
        }
        session = {
            "object": "checkout.session",
            "id": "cs_test_optional",
            "livemode": False,
            "mode": "payment",
            "payment_status": "paid",
            "amount_total": 500,
            "currency": "usd",
            "payment_intent": "pi_optional",
            "metadata": metadata,
        }

        def signed(kind, ident, obj):
            raw = json.dumps(
                {
                    "id": ident,
                    "object": "event",
                    "type": kind,
                    "created": self.now,
                    "livemode": False,
                    "api_version": "2026-09-30.endive",
                    "data": {"object": obj},
                }
            ).encode()
            return gateway.verify_event(
                raw,
                stripe.WebhookSignature.generate_signature_header(
                    raw.decode(), config["webhook_secret"]
                ),
            )

        original = signed("checkout.session.completed", "evt_paid_optional", session)
        provider.v1.checkout.sessions.retrieve.return_value = {
            **session,
            "line_items": {
                "has_more": False,
                "data": [
                    {
                        "quantity": 1,
                        "price": {
                            "id": PACK["price_id"],
                            "livemode": False,
                            "currency": "usd",
                            "tax_behavior": "inclusive",
                        },
                    }
                ],
            },
            "payment_intent": {"id": "pi_optional", "latest_charge": charge},
        }
        with self.assertLogs(
            "studio_platform.stripe_client", level="WARNING"
        ) as logged:
            proof = gateway.resolve_event(original)
            self.assertNotIn("receipt_url", proof["object"])
            core.receive_event(original)
            self.assertEqual(
                core.process_event("sixnine", original["id"], proof=proof)["state"],
                "processed",
            )
            refund = signed(
                "charge.refunded",
                "evt_refund_optional",
                {**charge, "amount_refunded": 500},
            )
            self.assertNotIn("receipt_url", refund["object"])
            core.receive_event(refund)
            self.assertEqual(
                core.process_event("sixnine", refund["id"])["state"], "processed"
            )
        self.assertEqual(
            logged.output,
            ["WARNING:studio_platform.stripe_client:billing_receipt_url_omitted"] * 2,
        )
        self.assertNotIn("unknown.example", " ".join(logged.output))
        with self.repo.engine.connect() as connection:
            account = (
                connection.execute(
                    select(accounts).where(accounts.c.user_id == self.ids["supervan"])
                )
                .mappings()
                .one()
            )
            self.assertEqual((account["balance"], account["held"]), (0, 0))
            kinds = (
                connection.execute(
                    select(entries.c.kind).where(
                        entries.c.user_id == self.ids["supervan"]
                    )
                )
                .scalars()
                .all()
            )
            self.assertCountEqual(kinds, ["purchase", "refund"])
            self.assertIsNone(
                connection.execute(
                    select(payments.c.receipt_url).where(payments.c.id == row["id"])
                ).scalar_one()
            )
