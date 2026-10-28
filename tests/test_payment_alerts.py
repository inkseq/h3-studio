"""Original payment/credit observations; disposable SQLite and no provider I/O."""

import json
from types import SimpleNamespace
import unittest
import uuid

from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.dialects import postgresql

from studio_platform.credit_schema import entries, metadata as credit_metadata
from studio_platform.payment_alerts import (
    _event_statement,
    _grant_exists,
    read_payment_snapshot,
)
from studio_platform.payment_schema import events, metadata, payments


class PaymentAlertTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        credit_metadata.create_all(self.engine)
        self.repo = SimpleNamespace(engine=self.engine)
        self.now = 1000.0
        self.tenant = "test-tenant"
        self.account = "acct_original"
        self.user = str(uuid.uuid4())

    def tearDown(self):
        self.engine.dispose()

    def payment(self, **changes):
        ident = str(uuid.uuid4())
        value = {
            "id": ident,
            "tenant": self.tenant,
            "user_id": self.user,
            "pack_id": "starter",
            "pack_revision": "original-revision",
            "pack_snapshot": {"private": "SECRET content"},
            "price_id": "price_Original",
            "credits": 500,
            "amount_cents": 500,
            "currency": "usd",
            "account_scope": self.account,
            "livemode": 0,
            "idempotency_key": ident,
            "request_fingerprint": "a" * 64,
            "terms_version": "original-terms",
            "terms_accepted_at": 500,
            "status": "created",
            "created_at": 600,
            "updated_at": 700,
            "checkout_started_at": 600,
            "checkout_bound_at": 610,
            "stripe_checkout_session_id": "cs_test_" + ident,
            "stripe_payment_intent_id": "pi_" + ident,
            "stripe_charge_id": "ch_" + ident,
            "checkout_url": "https://checkout.stripe.com/SECRET",
            "expires_at": None,
            "paid_at": None,
            "receipt_url": "https://pay.stripe.com/SECRET",
            "refunded_cents": 0,
            "refund_credits": 0,
            "clawed_back_credits": 0,
            "unmet_credits": 0,
            "dispute_open": 0,
            "stripe_dispute_id": None,
            **changes,
        }
        with self.engine.begin() as c:
            c.execute(insert(payments).values(**value))
        return value

    def signed(
        self,
        payment=None,
        *,
        kind="checkout.session.completed",
        status="deferred",
        payment_id=None,
        obj=None,
        received_at=650,
        **changes,
    ):
        obj = (
            obj
            if obj is not None
            else {
                "payment_id": payment["id"],
                "session_id": payment["stripe_checkout_session_id"],
                "payment_status": "paid",
            }
        )
        ident = "evt_" + uuid.uuid4().hex
        payload = {
            "id": ident,
            "type": kind,
            "created": 640,
            "account_scope": self.account,
            "livemode": False,
            "object": obj,
        }
        value = {
            "event_id": ident,
            "tenant": self.tenant,
            "account_scope": self.account,
            "livemode": 0,
            "type": kind,
            "verified_payload": payload,
            "fingerprint": "b" * 64,
            "received_at": received_at,
            "processed_at": None,
            "status": status,
            "payment_id": payment_id,
            "reason_code": "SECRET provider exception",
            **changes,
        }
        with self.engine.begin() as c:
            c.execute(insert(events).values(**value))
        return value

    def grant(self, payment, **changes):
        value = {
            "id": str(uuid.uuid4()),
            "tenant": payment["tenant"],
            "user_id": payment["user_id"],
            "kind": "purchase",
            "amount": 500,
            "held_delta": 0,
            "balance_after": 500,
            "held_after": 0,
            "reference_type": "payment",
            "reference_id": payment["id"],
            "reason": "SECRET original reason",
            "actor": "stripe:webhook",
            "idempotency_key": "stripe-purchase:"
            + payment["stripe_checkout_session_id"],
            "fingerprint": "c" * 64,
            "created_at": 660,
            "price_revision": None,
            "origin": "stripe-test",
            "receipt_url": None,
            **changes,
        }
        with self.engine.begin() as c:
            c.execute(insert(entries).values(**value))
        return value

    def snapshot(self, **changes):
        return read_payment_snapshot(
            self.repo,
            now=self.now,
            tenant_id=self.tenant,
            livemode=False,
            account_scope=self.account,
            **changes,
        )

    def test_paid_row_without_original_grant_uses_paid_time(self):
        payment = self.payment(status="paid", paid_at=600, updated_at=999)
        result = self.snapshot()
        self.assertTrue(result["complete"])
        self.assertEqual(result["fulfillment"], [{"id": payment["id"], "since": 600}])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_signed_paid_created_row_deferred_or_review_is_observed_without_paid_row(
        self,
    ):
        for status in ("received", "deferred", "review"):
            payment = self.payment()
            self.signed(payment, status=status, received_at=550)
            result = self.snapshot()
            self.assertIn({"id": payment["id"], "since": 550}, result["fulfillment"])
            self.assertIn({"id": payment["id"], "since": 550}, result["processing"])

    def test_duplicate_signed_events_and_paid_row_collapse_to_original_purchase(self):
        payment = self.payment(status="paid", paid_at=600)
        self.signed(payment, received_at=650)
        self.signed(payment, received_at=660)
        result = self.snapshot()
        self.assertEqual(result["fulfillment"], [{"id": payment["id"], "since": 600}])
        self.assertEqual(result["processing"], [{"id": payment["id"], "since": 650}])

    def test_original_grant_means_fulfilled_even_when_spent_or_fully_refunded(self):
        payment = self.payment(
            status="refunded",
            paid_at=600,
            refunded_cents=500,
            refund_credits=500,
            clawed_back_credits=500,
        )
        self.grant(payment)
        self.signed(payment)
        # No wallet read is necessary. The original positive grant still exists
        # after spending/refund, independent of today's account balance.
        self.assertEqual(self.snapshot()["fulfillment"], [])

    def test_wrong_original_grant_identity_origin_amount_or_key_cannot_hide_failure(
        self,
    ):
        for field, wrong in (
            ("user_id", str(uuid.uuid4())),
            ("origin", "stripe-live"),
            ("amount", 499),
            ("idempotency_key", "unrelated-original"),
            ("kind", "grant"),
            ("actor", "browser:admin"),
        ):
            with self.subTest(field=field):
                payment = self.payment(status="paid", paid_at=600)
                self.grant(payment, **{field: wrong})
                self.assertIn(
                    {"id": payment["id"], "since": 600}, self.snapshot()["fulfillment"]
                )

    def test_no_signed_paid_proof_does_not_turn_created_checkout_into_failure(self):
        payment = self.payment()
        self.signed(
            payment, obj={"payment_id": payment["id"], "payment_status": "unpaid"}
        )
        result = self.snapshot()
        self.assertEqual(result["fulfillment"], [])
        self.assertEqual(len(result["processing"]), 1)

    def test_uncorrelated_signed_paid_has_stable_uuid_without_raw_event_id(self):
        signed = self.signed(
            obj={"session_id": "cs_test_uncorrelated", "payment_status": "paid"}
        )
        first, second = self.snapshot(), self.snapshot()
        self.assertEqual(first, second)
        self.assertEqual(len(first["fulfillment"]), 1)
        uuid.UUID(first["fulfillment"][0]["id"])
        self.assertNotIn(signed["event_id"], json.dumps(first))

    def test_foreign_tenant_account_mode_cannot_correlate_or_leak(self):
        self.payment(tenant="foreign", status="paid", paid_at=600)
        self.payment(account_scope="acct_foreign", status="paid", paid_at=600)
        self.signed(obj={"payment_status": "paid"}, tenant="foreign")
        self.signed(obj={"payment_status": "paid"}, account_scope="acct_foreign")
        # A mismatch between signed normalized context and stored mode is also
        # not an authoritative paid signal for this scope.
        self.signed(
            obj={"payment_status": "paid"},
            verified_payload={
                "livemode": True,
                "account_scope": self.account,
                "object": {"payment_status": "paid"},
            },
        )
        self.assertEqual(self.snapshot()["fulfillment"], [])

    def test_same_session_other_account_is_uncorrelated_not_foreign_purchase(self):
        foreign = self.payment(account_scope="acct_foreign", status="paid", paid_at=600)
        self.signed(foreign)
        result = self.snapshot()
        self.assertEqual(len(result["fulfillment"]), 1)
        self.assertNotEqual(result["fulfillment"][0]["id"], foreign["id"])

    def test_ambiguous_event_selectors_have_event_identity_not_arbitrary_purchase(self):
        first, second = self.payment(), self.payment()
        self.signed(
            obj={
                "payment_id": first["id"],
                "session_id": second["stripe_checkout_session_id"],
                "payment_status": "paid",
            }
        )
        result = self.snapshot()
        self.assertEqual(len(result["fulfillment"]), 1)
        self.assertNotIn(result["fulfillment"][0]["id"], {first["id"], second["id"]})

    def test_grant_on_one_ambiguous_candidate_does_not_assign_other_purchase(self):
        first, second = self.payment(), self.payment()
        self.grant(first)
        self.signed(
            obj={
                "payment_id": first["id"],
                "session_id": second["stripe_checkout_session_id"],
                "payment_status": "paid",
            }
        )
        result = self.snapshot()
        self.assertEqual(len(result["fulfillment"]), 1)
        self.assertNotIn(result["fulfillment"][0]["id"], {first["id"], second["id"]})

    def test_failure_refund_dispute_window_is_bounded_and_deduplicated(self):
        payment = self.payment(
            status="failed",
            updated_at=750,
            refunded_cents=10,
            refund_credits=10,
            unmet_credits=10,
            dispute_open=1,
        )
        for kind in (
            "checkout.session.async_payment_failed",
            "charge.refunded",
            "charge.dispute.created",
        ):
            self.signed(payment, kind=kind, status="processed", received_at=750)
            self.signed(payment, kind=kind, status="processed", received_at=760)
        result = self.snapshot()
        for name in ("payment_failed", "payment_refunded", "payment_disputed"):
            self.assertEqual(result["activity"][name], {"count": 1, "since": 750})
        self.now = 1101
        for value in self.snapshot()["activity"].values():
            self.assertEqual(value, {"count": 0, "since": None})
        self.assertEqual(
            self.snapshot(window_seconds=600)["activity"]["payment_failed"]["count"], 1
        )

    def test_missing_optional_family_is_unknown_not_healthy_empty(self):
        entries.drop(self.engine)
        result = self.snapshot()
        self.assertFalse(result["complete"])

    def test_read_only_queries_preserve_all_original_rows_and_never_wallet_read(self):
        payment = self.payment(status="paid", paid_at=600)
        self.signed(payment)
        with self.engine.connect() as c:
            before = [dict(r) for r in c.execute(select(payments)).mappings()]
        statements = []

        def capture(_connection, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            result = self.snapshot()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertTrue(result["complete"])
        self.assertTrue(
            all(s.lstrip().upper().startswith(("SELECT", "SET")) for s in statements)
        )
        self.assertTrue(all("platform_credit_accounts" not in s for s in statements))
        with self.engine.connect() as c:
            after = [dict(r) for r in c.execute(select(payments)).mappings()]
        self.assertEqual(before, after)

    def test_portable_postgres_json_and_correlated_grant_query_compile(self):
        query = _event_statement(self.tenant, True, self.account).where(
            ~_grant_exists()
        )
        compiled = str(query.compile(dialect=postgresql.dialect()))
        self.assertIn("platform_credit_entries", compiled)
        self.assertIn("LEFT OUTER JOIN", compiled)
        self.assertIn("CAST", compiled)

    def test_invalid_context_refuses_before_database_access(self):
        with self.assertRaisesRegex(ValueError, "payment_alert_context_invalid"):
            self.snapshot(window_seconds=0)
