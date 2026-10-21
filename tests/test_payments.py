"""Original purchase/event money invariants; disposable DB and no Stripe I/O."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from sqlalchemy import insert, select, update

from studio_platform import credits
from studio_platform.auth import Principal
from studio_platform.credit_schema import entries
from studio_platform.identity_schema import admin_audit, users
from studio_platform.payment_schema import (
    LIVE_PAYMENT_REVISION,
    events,
    frozen_metadata,
    metadata,
    payments,
)
from studio_platform.payments import PaymentCore, PaymentError, STAGING
from studio_platform.payments_workflows import register_payment_workflows
from studio_platform.repository import outbox
import test_credits as fixtures
import test_dbos_postgres as dbos_fixtures


class OriginalTransport:
    def __init__(self):
        self.fail = False

    def job_event(self, *_args, **_kwargs):
        pass

    def payment_event(self, connection, tenant, event_id):
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, "payment-test:" + tenant + event_id))
        if (
            connection.execute(select(outbox.c.id).where(outbox.c.id == ident)).first()
            is None
        ):
            connection.execute(
                insert(outbox).values(
                    id=ident,
                    event_type="payment-test-wake",
                    aggregate_id=event_id,
                    payload={"tenant": tenant},
                    created_at=1000,
                )
            )
        if self.fail:
            raise RuntimeError("Synthetic SDK enqueue failure")


class PaymentTests(fixtures.CreditTests):
    def setUp(self):
        super().setUp()
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id,
            deployment_target="staging",
            public_origin=STAGING,
            public_origin_aliases=(),
        )
        metadata.create_all(self.repo.engine)
        with self.repo.transaction() as connection:
            connection.execute(update(self.version).values(version_num="20261020_0005"))
            connection.execute(
                update(users).values(
                    terms_version="reviewed-terms-v1", terms_accepted_at=self.now - 1
                )
            )
        self.transport = OriginalTransport()
        self.repo.generation_transport = self.transport
        self.principal = Principal(
            "superdan", "browser:superdan", role="user", identity_source="canonical"
        )
        self.core = PaymentCore(
            self.repo,
            self.settings,
            terms_check=lambda _conn, _principal, user: {
                "version": "reviewed-terms-v1",
                "accepted_at": user["terms_accepted_at"],
            },
        )
        self.pack = dict(
            id="starter",
            revision="packs-v1",
            price_id="price_test_original",
            credits=500,
            amount_cents=500,
            currency="usd",
            bonus_percent=0,
        )

    def rows(self, table):
        with self.repo.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def purchase(self, *, key="purchase-original", credits_amount=500):
        pack = dict(self.pack, credits=credits_amount)
        return self.core.begin_checkout(
            self.principal, pack, key, account_scope="acct_test_original"
        )

    def object(self, payment, **changes):
        row = payment["payment"]
        return {
            "payment_id": row["id"],
            "user_id": row["user_id"],
            "pack_id": row["pack_id"],
            "pack_revision": row["pack_revision"],
            "credits": row["credits"],
            "price_id": row["price_id"],
            "amount_cents": row["amount_cents"],
            "currency": "usd",
            "session_id": "cs_test_" + row["id"].replace("-", ""),
            "payment_intent_id": "pi_" + row["id"].replace("-", ""),
            "charge_id": "ch_" + row["id"].replace("-", ""),
            "payment_status": "paid",
            **changes,
        }

    def event(
        self, payment, *, event_id=None, kind="checkout.session.completed", **changes
    ):
        return {
            "id": event_id or "evt_" + uuid.uuid4().hex,
            "type": kind,
            "created": self.now,
            "account_scope": "acct_test_original",
            "livemode": False,
            "object": self.object(payment, **changes),
        }

    def process(self, event):
        self.core.receive_event(event)
        return self.core.process_event(self.scope.tenant_id, event["id"])

    def test_original_intent_is_frozen_before_sdk_and_unknown_retries_keep_same_key(
        self,
    ):
        first = self.purchase()
        changed = dict(
            self.pack,
            revision="packs-v2",
            credits=999,
            amount_cents=1000,
            price_id="price_later",
        )
        replay = self.core.begin_checkout(
            self.principal,
            changed,
            "purchase-original",
            account_scope="acct_test_original",
        )
        self.assertTrue(first["dispatch"])
        self.assertEqual(first, replay)
        self.assertEqual(len(self.rows(payments)), 1)
        self.assertEqual(self.rows(entries), [])
        self.assertEqual(self.rows(payments)[0]["checkout_started_at"], self.now)
        self.now += 23 * 3600
        with self.assertRaisesRegex(PaymentError, "checkout_reconciliation_required"):
            self.purchase()
        self.assertEqual(len(self.rows(payments)), 1)

    def test_human_active_account_reviewed_terms_and_test_environment_are_required(
        self,
    ):
        with self.assertRaisesRegex(PaymentError, "human_required"):
            self.core.begin_checkout(
                Principal("superdan", "key:agent", machine=True),
                self.pack,
                "machine",
                account_scope="acct_test_original",
            )
        with self.assertRaisesRegex(PaymentError, "terms_unavailable"):
            PaymentCore(self.repo, self.settings).begin_checkout(
                self.principal, self.pack, "terms", account_scope="acct_test_original"
            )
        self.settings.deployment_target = "production"
        with self.assertRaisesRegex(PaymentError, "test_environment_required"):
            self.purchase()
        self.settings.deployment_target = "staging"
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == self.user_id)
                .values(status="suspended")
            )
        with self.assertRaisesRegex(PaymentError, "account_unavailable"):
            self.purchase()
        self.assertEqual(self.rows(payments), [])

    def test_exact_sdk_session_bind_is_owned_and_cannot_change_original_price(self):
        payment = self.purchase()
        original = payment["payment"]
        session = {
            "account_scope": "acct_test_original",
            "livemode": False,
            "object": self.object(
                payment,
                payment_status="unpaid",
                checkout_url="https://checkout.stripe.com/c/pay/cs_test_fixture",
                expires_at=self.now + 1800,
            ),
        }
        changed = copy.deepcopy(session)
        changed["object"]["amount_cents"] += 1
        with self.assertRaisesRegex(PaymentError, "original_mismatch"):
            self.core.bind_checkout(
                original["id"], original["request_fingerprint"], changed
            )
        bound = self.core.bind_checkout(
            original["id"], original["request_fingerprint"], session
        )
        self.assertEqual(bound["checkout_url"], session["object"]["checkout_url"])
        self.assertFalse(self.purchase()["dispatch"])
        self.assertEqual(self.rows(entries), [])

    def test_verified_receipt_and_durable_wake_commit_atomically_and_event_conflicts_refuse(
        self,
    ):
        event = self.event(self.purchase(), event_id="evt_original")
        self.transport.fail = True
        with self.assertRaisesRegex(RuntimeError, "enqueue"):
            self.core.receive_event(event)
        self.assertEqual(self.rows(events), [])
        self.assertFalse(
            [
                row
                for row in self.rows(outbox)
                if row["event_type"] == "payment-test-wake"
            ]
        )
        self.transport.fail = False
        self.core.receive_event(event)
        self.core.receive_event(event)
        self.assertEqual(len(self.rows(events)), 1)
        changed = copy.deepcopy(event)
        changed["object"]["credits"] += 1
        with self.assertRaisesRegex(PaymentError, "event_conflict"):
            self.core.receive_event(changed)
        self.assertEqual(self.rows(events)[0]["verified_payload"], event)

    def test_same_event_and_distinct_paid_notifications_only_credit_original_once(self):
        payment = self.purchase()
        before = self.repo.get_budget("owner-budget")
        event = self.event(payment)
        self.process(event)
        self.process(event)
        self.process(
            self.event(payment, kind="checkout.session.async_payment_succeeded")
        )
        self.process(
            self.event(
                payment,
                kind="checkout.session.async_payment_failed",
                payment_status="unpaid",
            )
        )
        self.assertEqual(
            [(row["kind"], row["amount"]) for row in self.rows(entries)],
            [("purchase", 500)],
        )
        self.assertEqual(self.balance()["balance"], 500)
        self.assertEqual(self.rows(payments)[0]["status"], "paid")
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(len(self.rows(admin_audit)), 1)
        self.assert_invariants()

    def test_missing_actual_price_defers_then_additive_provider_proof_fulfills(self):
        payment = self.purchase()
        event = self.event(payment)
        del event["object"]["price_id"]
        self.assertEqual(self.process(event)["state"], "deferred")
        self.assertEqual(self.rows(entries), [])
        proof = copy.deepcopy(event)
        proof["object"]["price_id"] = self.pack["price_id"]
        changed = copy.deepcopy(proof)
        changed["object"]["credits"] += 1
        with self.assertRaisesRegex(PaymentError, "resolved_proof_conflict"):
            self.core.process_event(self.scope.tenant_id, event["id"], proof=changed)
        self.assertEqual(
            self.core.process_event(self.scope.tenant_id, event["id"], proof=proof)[
                "state"
            ],
            "processed",
        )
        self.assertEqual(self.rows(events)[0]["verified_payload"], event)
        self.assertEqual(self.balance()["balance"], 500)

    def test_missing_payment_status_defers_until_provider_proof(self):
        payment = self.purchase()
        event = self.event(payment)
        del event["object"]["payment_status"]
        self.assertEqual(self.process(event)["state"], "deferred")
        self.assertEqual(self.rows(entries), [])
        proof = copy.deepcopy(event)
        proof["object"]["payment_status"] = "paid"
        self.assertEqual(
            self.core.process_event(self.scope.tenant_id, event["id"], proof=proof)[
                "state"
            ],
            "processed",
        )
        self.assertEqual(self.balance()["balance"], 500)

    def test_abandoned_async_failed_and_unpaid_completed_never_add_credits(self):
        for kind in (
            "checkout.session.completed",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        ):
            payment = self.purchase(key=kind)
            self.process(self.event(payment, kind=kind, payment_status="unpaid"))
        self.assertEqual(self.rows(entries), [])
        self.assertEqual(self.balance()["balance"], 0)

    def test_refund_before_paid_retains_original_purchase_and_ceil_fraction(self):
        payment = self.purchase(credits_amount=101)
        self.process(self.event(payment, kind="charge.refunded", amount_refunded=2))
        self.assertEqual(self.rows(entries), [])
        self.process(self.event(payment))
        self.assertEqual(
            [(row["kind"], row["amount"]) for row in self.rows(entries)],
            [("purchase", 101), ("refund", -1)],
        )
        row = self.rows(payments)[0]
        self.assertEqual(
            (
                row["refunded_cents"],
                row["refund_credits"],
                row["clawed_back_credits"],
                row["unmet_credits"],
            ),
            (2, 1, 1, 0),
        )
        self.assertEqual(self.balance()["balance"], 100)
        self.assert_invariants()

    def test_cumulative_refund_keeps_exact_cents_when_credit_ceiling_does_not_change(
        self,
    ):
        payment = self.purchase(credits_amount=1)
        self.process(self.event(payment))
        for cents in (1, 2, 2, 1):
            self.process(
                self.event(payment, kind="charge.refunded", amount_refunded=cents)
            )
        row = self.rows(payments)[0]
        self.assertEqual(
            (row["refunded_cents"], row["refund_credits"], row["clawed_back_credits"]),
            (2, 1, 1),
        )
        self.assertEqual(len(self.rows(entries)), 2)
        self.assertEqual(self.balance()["balance"], 0)
        self.assert_invariants()

    def test_refund_cannot_spend_holds_or_silently_recover_old_unmet_debt(self):
        payment = self.purchase(credits_amount=100)
        self.process(self.event(payment))
        job = self.credit_job(amount=80)
        self.hold(job)
        self.process(self.event(payment, kind="charge.refunded", amount_refunded=250))
        row = self.rows(payments)[0]
        self.assertEqual(
            (row["refund_credits"], row["clawed_back_credits"], row["unmet_credits"]),
            (50, 20, 30),
        )
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (80, 80))
        self.seed(50, key="later-independent-top-up")
        self.process(self.event(payment, kind="charge.refunded", amount_refunded=250))
        self.process(self.event(payment))
        self.assertEqual(self.balance()["balance"], 130)
        self.process(self.event(payment, kind="charge.refunded", amount_refunded=500))
        row = self.rows(payments)[0]
        self.assertEqual(
            (row["refund_credits"], row["clawed_back_credits"], row["unmet_credits"]),
            (100, 70, 30),
        )
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (80, 80))
        self.assert_invariants()

    def test_original_amount_user_and_live_mode_cannot_be_replaced_by_event_metadata(
        self,
    ):
        payment = self.purchase()
        for changes in (
            {"amount_cents": 501},
            {"credits": 999},
            {"user_id": self.other_id},
            {"price_id": "price_other"},
        ):
            result = self.process(self.event(payment, **changes))
            self.assertEqual(result["state"], "review")
        live = self.event(payment)
        live["livemode"] = True
        live["object"]["session_id"] = "cs_live_original"
        with self.assertRaisesRegex(PaymentError, "test_environment_required"):
            self.core.receive_event(live)
        self.assertEqual(self.rows(entries), [])
        self.assertIsNone(self.rows(payments)[0]["paid_at"])

    def enable_live_fixture(self):
        # An adopted, exact 006 schema is separate from the frozen 005 family.
        metadata.drop_all(self.repo.engine)
        frozen_metadata(revision=LIVE_PAYMENT_REVISION).create_all(self.repo.engine)
        with self.repo.transaction() as connection:
            connection.execute(
                update(self.version).values(version_num=LIVE_PAYMENT_REVISION)
            )
        self.settings.deployment_target = "production"
        self.settings.public_origin = "https://inkseq.com"
        self.settings.stripe_mode = "live"

    def live_purchase(self, key="live-original"):
        return self.core.begin_checkout(
            self.principal,
            self.pack,
            key,
            account_scope="acct_live_original",
            livemode=True,
        )

    def live_event(self, payment, **changes):
        event = self.event(payment, **changes)
        event.update(livemode=True, account_scope="acct_live_original")
        event["object"]["session_id"] = "cs_live_" + payment["payment"]["id"].replace(
            "-", ""
        )
        return event

    def test_live_writer_requires_adopted_006_not_existing_test_schema(self):
        self.settings.deployment_target = "production"
        self.settings.public_origin = "https://inkseq.com"
        self.settings.stripe_mode = "live"
        with self.assertRaisesRegex(PaymentError, "schema_not_ready"):
            self.live_purchase()
        self.assertEqual(self.rows(payments), [])
        self.enable_live_fixture()
        original = self.live_purchase()
        self.assertIs(original["payment"]["livemode"], True)
        self.assertEqual(self.rows(payments)[0]["livemode"], 1)
        self.assertEqual(original, self.live_purchase())
        with self.assertRaisesRegex(PaymentError, "purchases_disabled"):
            self.core.begin_checkout(
                self.principal,
                self.pack,
                "cross-mode",
                account_scope="acct_live_original",
            )

    def test_live_off_retains_signed_obligation_and_refund_without_duplicate_credit(
        self,
    ):
        self.enable_live_fixture()
        original = self.live_purchase()
        event = self.live_event(original)
        self.settings.stripe_mode = "off"
        with self.assertRaisesRegex(PaymentError, "purchases_disabled"):
            self.live_purchase("new-while-off")
        self.process(event)
        self.process(event)
        self.assertEqual(self.balance()["balance"], 500)
        self.assertEqual(self.rows(entries)[0]["origin"], "stripe-live")
        self.process(
            self.live_event(original, kind="charge.refunded", amount_refunded=250)
        )
        self.assertEqual(self.balance()["balance"], 250)
        self.assertEqual(len(self.rows(payments)), 1)
        self.assertTrue(all(row["livemode"] == 1 for row in self.rows(events)))
        self.assertTrue(
            all(row["origin"] == "stripe-live" for row in self.rows(entries))
        )

    def test_live_context_rejects_test_binding_and_foreign_account_without_ledger_effects(
        self,
    ):
        self.enable_live_fixture()
        original = self.live_purchase()
        session = {
            key: value
            for key, value in self.live_event(original).items()
            if key in {"account_scope", "livemode", "object"}
        }
        session["object"].update(
            checkout_url="https://checkout.stripe.com/c/pay/original",
            expires_at=self.now + 3600,
        )
        wrong_account = copy.deepcopy(session)
        wrong_account["account_scope"] = "acct_other"
        with self.assertRaisesRegex(PaymentError, "original_mismatch"):
            self.core.bind_checkout(
                original["payment"]["id"],
                original["payment"]["request_fingerprint"],
                wrong_account,
            )
        wrong_mode = copy.deepcopy(session)
        wrong_mode["livemode"] = False
        wrong_mode["object"]["session_id"] = "cs_test_original"
        with self.assertRaisesRegex(PaymentError, "test_environment_required"):
            self.core.bind_checkout(
                original["payment"]["id"],
                original["payment"]["request_fingerprint"],
                wrong_mode,
            )
        foreign = self.live_event(original)
        foreign["account_scope"] = "acct_other"
        self.assertEqual(self.process(foreign)["state"], "deferred")
        self.assertEqual(self.rows(entries), [])
        self.assertIsNone(self.rows(payments)[0]["stripe_checkout_session_id"])
        bound = self.core.bind_checkout(
            original["payment"]["id"],
            original["payment"]["request_fingerprint"],
            session,
        )
        self.assertIs(bound["livemode"], True)
        self.assertEqual(
            bound["stripe_checkout_session_id"], session["object"]["session_id"]
        )
        replaced = copy.deepcopy(session)
        replaced["object"]["session_id"] = "cs_live_different"
        with self.assertRaisesRegex(PaymentError, "original_mismatch"):
            self.core.bind_checkout(
                original["payment"]["id"],
                original["payment"]["request_fingerprint"],
                replaced,
            )

    def test_dispute_blocks_new_purchases_and_late_creation_cannot_reopen_won_dispute(
        self,
    ):
        payment = self.purchase()
        self.process(self.event(payment))
        self.process(
            self.event(payment, kind="charge.dispute.created", dispute_id="dp_original")
        )
        with self.assertRaisesRegex(PaymentError, "purchases_suspended"):
            self.purchase(key="another-pack")
        self.process(
            self.event(
                payment,
                kind="charge.dispute.closed",
                dispute_id="dp_original",
                dispute_won=True,
            )
        )
        self.process(
            self.event(payment, kind="charge.dispute.created", dispute_id="dp_original")
        )
        self.assertEqual(self.rows(payments)[0]["dispute_open"], 0)
        self.assertTrue(self.purchase(key="another-pack")["dispatch"])
        self.assertEqual(self.balance()["balance"], 500)

    def test_money_and_paid_proof_rollback_together_when_audit_fails(self):
        payment = self.purchase()
        event = self.event(payment)
        self.core.receive_event(event)
        with patch.object(
            self.core, "_audit", side_effect=RuntimeError("Synthetic audit failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit"):
                self.core.process_event(self.scope.tenant_id, event["id"])
        self.assertEqual(self.rows(entries), [])
        self.assertIsNone(self.rows(payments)[0]["paid_at"])
        self.assertEqual(self.rows(events)[0]["status"], "received")

    def test_concurrent_distinct_events_for_original_session_only_add_one_purchase(
        self,
    ):
        payment = self.purchase()
        original = [self.event(payment) for _ in range(4)]
        for event in original:
            self.core.receive_event(event)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda event: self.core.process_event(
                        self.scope.tenant_id, event["id"]
                    ),
                    original,
                )
            )
        self.assertTrue(all(result["done"] for result in results))
        self.assertEqual(
            [(row["kind"], row["amount"]) for row in self.rows(entries)],
            [("purchase", 500)],
        )
        self.assert_invariants()

    def test_dbos_adapter_resolves_outside_transaction_and_keeps_failure_static(self):
        class Runtime:
            @staticmethod
            def step(**_kwargs):
                return lambda fn: fn

            @staticmethod
            def workflow(**_kwargs):
                return lambda fn: fn

            @staticmethod
            async def recv_async(*_args, **_kwargs):
                raise InterruptedError("Synthetic durable wait")

        original_transaction = self.repo.transaction
        active = [0]

        @contextmanager
        def counted_transaction():
            with original_transaction() as connection:
                active[0] += 1
                try:
                    yield connection
                finally:
                    active[0] -= 1

        owner = self

        class Gateway:
            def resolve_event(self, event):
                owner.assertEqual(active[0], 0)
                return event

        first = self.event(self.purchase())
        self.core.receive_event(first)
        workflow = register_payment_workflows(
            Runtime,
            self.repo,
            self.settings,
            gateway_factory=lambda _settings: Gateway(),
        )
        with patch.object(self.repo, "transaction", counted_transaction):
            result = asyncio.run(workflow(self.scope.tenant_id, first["id"]))
        self.assertEqual(result["state"], "processed")
        self.assertEqual(self.balance()["balance"], 500)
        second = self.event(self.purchase(key="second"))
        self.core.receive_event(second)

        class UnavailableGateway:
            def resolve_event(self, _event):
                raise RuntimeError("Synthetic confidential provider diagnostic")

        workflow = register_payment_workflows(
            Runtime,
            self.repo,
            self.settings,
            gateway_factory=lambda _settings: UnavailableGateway(),
        )
        with self.assertRaises(InterruptedError):
            asyncio.run(workflow(self.scope.tenant_id, second["id"]))
        retained = next(
            row for row in self.rows(events) if row["event_id"] == second["id"]
        )
        self.assertEqual(
            (retained["status"], retained["reason_code"]),
            ("deferred", "payment_processing_unavailable"),
        )
        self.assertEqual(self.balance()["balance"], 500)


class PaymentClientTransactionTests(dbos_fixtures.ClientTransactionTests):
    """Uses the real pinned SDK only against its explicit disposable test DB."""

    def test_verified_event_sdk_receipt_replay_and_unknown_ack_are_atomic(self):
        from sqlalchemy import Column, MetaData, String, Table
        from studio_platform.credit_schema import metadata as credit_metadata
        from studio_platform.identity_schema import metadata as identity_metadata
        from studio_platform.payments_workflows import (
            PAYMENT_WORKFLOW,
            payment_workflow_id,
        )

        identity_metadata.create_all(self.repo.engine)
        credit_metadata.create_all(self.repo.engine)
        metadata.create_all(self.repo.engine)
        marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        marker.create(self.repo.engine)
        ident = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(insert(marker).values(version_num="20261020_0005"))
            connection.execute(
                insert(users).values(
                    id=ident,
                    tenant="test",
                    owner="superdan",
                    role="user",
                    status="active",
                    display_name="Synthetic payment test",
                    created_at=1000,
                    updated_at=1000,
                    terms_version="reviewed-terms-v1",
                    terms_accepted_at=999,
                )
            )
        settings = SimpleNamespace(
            tenant_id="test",
            deployment_target="staging",
            public_origin=STAGING,
            public_origin_aliases=(),
        )
        core = PaymentCore(
            self.repo,
            settings,
            terms_check=lambda _c, _p, _u: {
                "version": "reviewed-terms-v1",
                "accepted_at": 999,
            },
        )
        pack = dict(
            id="starter",
            revision="packs-v1",
            price_id="price_test_original",
            credits=500,
            amount_cents=500,
            currency="usd",
            bonus_percent=0,
        )
        purchase = core.begin_checkout(
            Principal("superdan", "browser:superdan"),
            pack,
            "original-pg-payment",
            account_scope="acct_test_original",
        )
        row = purchase["payment"]
        event = {
            "id": "evt_pg_original",
            "type": "checkout.session.completed",
            "created": 1000,
            "account_scope": "acct_test_original",
            "livemode": False,
            "object": {
                "payment_id": row["id"],
                "user_id": ident,
                "pack_id": "starter",
                "pack_revision": "packs-v1",
                "price_id": "price_test_original",
                "credits": 500,
                "amount_cents": 500,
                "currency": "usd",
                "session_id": "cs_test_original_pg",
                "payment_status": "paid",
            },
        }
        client = self.transport.client
        proxy = Mock(wraps=client)

        def lost(*args, **kwargs):
            client.send_in_transaction(*args, **kwargs)
            raise RuntimeError("Synthetic lost payment enqueue acknowledgement")

        proxy.send_in_transaction.side_effect = lost
        self.transport.client = proxy
        try:
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                core.receive_event(event)
        finally:
            self.transport.client = client
        self.assertEqual(self.workflows(), [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(events)).all(), [])
            self.assertEqual(connection.execute(select(entries)).all(), [])
        core.receive_event(event)
        core.receive_event(event)
        workflow = self.workflows()
        self.assertEqual(len(workflow), 1)
        self.assertEqual(
            workflow[0]["workflow_uuid"], payment_workflow_id("test", event["id"])
        )
        self.assertEqual(workflow[0]["name"], PAYMENT_WORKFLOW)
        self.assertEqual(workflow[0]["queue_name"], "inkseq-generation-v1")
        self.assertEqual(core.process_event("test", event["id"])["state"], "processed")
        core.receive_event(event)
        self.assertEqual(len(self.workflows()), 1)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(entries.c.kind, entries.c.amount)).all(),
                [("purchase", 500)],
            )


def load_tests(_loader, _tests, _pattern):
    # Reuse the existing fixture; do not rediscover/rerun its completed tests.
    return unittest.TestSuite(
        cls(name)
        for cls in (PaymentTests, PaymentClientTransactionTests)
        for name in cls.__dict__
        if name.startswith("test_")
    )
