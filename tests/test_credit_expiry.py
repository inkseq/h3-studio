"""Disposable SQL invariants for expiry, accepted holds and original provenance."""

import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from sqlalchemy import func, insert, select, update

from studio_platform import credits, credit_expiry, credit_lots
from studio_platform.credit_schema import accounts, entries
from studio_platform.credits_pricing import DEFAULT_RATES, PricingError
from studio_platform.repository import documents
import test_credits_routes as fixtures


def epoch(year, month, day):
    return datetime(year, month, day, tzinfo=timezone.utc).timestamp()


class CreditExpiryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CreditsRouteTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.now = epoch(2028, 2, 29)
        self.revision = self.f.enable()
        self.index = 0

    def adopt(self):
        value = self.f.service.publish(
            self.f.admin,
            {
                "expected_revision": self.revision,
                "rates": dict(DEFAULT_RATES),
                "expiry_policy": dict(credit_lots.POLICY),
            },
            key="expiry-adoption",
        )
        self.revision = value["revision"]
        return value

    def grant(self, amount, *, key=None, kind="grant", reference=None, issued_at=None):
        self.index += 1
        with self.f.repo.transaction() as connection:
            return credits.grant(
                connection,
                "sixnine",
                "supervan",
                amount,
                actor="fixture",
                reason="Disposable expiry test",
                idempotency_key=key or f"grant-{self.index}",
                now=self.f.now,
                kind=kind,
                reference_type="payment" if kind == "purchase" else "signup",
                reference_id=reference or f"ref-{self.index}",
                issued_at=issued_at,
            )

    def change(
        self, kind, amount, held_delta=0, *, ref="job-1", key=None, ref_type="job"
    ):
        self.index += 1
        with self.f.repo.transaction() as connection:
            row = credits._account(
                connection, "sixnine", self.f.ids["supervan"], now=self.f.now
            )
            return credits._change(
                connection,
                row,
                kind=kind,
                amount=amount,
                held_delta=held_delta,
                reference_type=ref_type,
                reference_id=ref,
                actor="fixture",
                reason="Disposable outcome",
                now=self.f.now,
                idempotency_key=key or f"change-{self.index}",
            )

    def balance(self):
        return self.f.service.balance(self.f.user)

    def ledger(self):
        with self.f.repo.engine.connect() as connection:
            return [dict(x) for x in connection.execute(select(entries)).mappings()]

    def assert_invariants(self):
        with self.f.repo.engine.connect() as connection:
            row = dict(connection.execute(select(accounts)).mappings().one())
            sums = connection.execute(
                select(func.sum(entries.c.amount), func.sum(entries.c.held_delta))
            ).one()
            self.assertEqual(sums, (row["balance"], row["held"]))
            saved = credit_expiry.state_document(connection, row, self.f.now)
            if saved:
                credit_lots.validate(saved["payload"]["state"], row)

    def test_explicit_revision_only_and_no_retroactive_expiry(self):
        original = self.grant(100, key="legacy")
        self.f.now += 40 * 86400
        self.assertNotIn("expiry", self.balance())
        self.adopt()
        self.assertNotIn("expiry", self.balance())
        added = self.grant(50, key="new")
        self.assertEqual(self.balance()["expiry"]["next_credits"], 50)
        self.f.now += 31 * 86400
        before = self.ledger()
        self.assertEqual(
            (self.balance()["balance"], self.balance()["available"]), (100, 100)
        )
        self.assertEqual(self.balance()["expiry"]["expired_unspent"], 50)
        self.assertEqual(
            self.ledger(), before
        )  # GET is a projection, never expiry writes.
        self.grant(1)
        expired = [x for x in self.ledger() if x["kind"] == "expire"]
        self.assertEqual(
            [(x["amount"], x["reference_id"]) for x in expired],
            [(-50, added["entry"]["id"])],
        )
        self.assertEqual(
            next(x for x in self.ledger() if x["id"] == original["entry"]["id"]),
            original["entry"],
        )
        self.assert_invariants()

    def test_calendar_year_and_exact_promotion_boundary(self):
        self.adopt()
        self.grant(60, kind="purchase", reference="payment-1")
        self.grant(40)
        self.assertEqual(self.balance()["expiry"]["next_at"], self.f.now + 30 * 86400)
        self.f.now += 30 * 86400
        self.assertEqual(self.balance()["balance"], 60)
        self.assertEqual(self.balance()["expiry"]["next_at"], epoch(2029, 2, 28))
        self.assertEqual(
            credit_lots.expiry("purchase", epoch(2027, 3, 31)), epoch(2028, 3, 31)
        )

    def test_missing_account_does_not_create_unlocked_lot_state(self):
        self.adopt()
        with self.f.repo.transaction() as connection:
            row = credits._account(
                connection, "sixnine", self.f.ids["supervan"], now=self.f.now
            )
            self.assertEqual(row["balance"], 0)
            self.assertIsNone(credit_expiry.read(connection, row))
        self.grant(10)
        self.assertEqual(self.balance()["expiry"]["next_credits"], 10)

    def test_delayed_purchase_delivery_uses_original_verified_event_time(self):
        self.adopt()
        bought_at = self.f.now
        self.f.now += 20 * 86400
        self.grant(60, key="delayed", kind="purchase", issued_at=bought_at)
        self.assertEqual(self.balance()["expiry"]["next_at"], epoch(2029, 2, 28))
        original = self.ledger()
        self.f.now += 10
        self.grant(
            60, key="delayed", kind="purchase", issued_at=bought_at, reference="ref-1"
        )
        self.assertEqual(self.ledger(), original)
        with self.assertRaises(credits.CreditError):
            self.grant(60, kind="purchase", issued_at=self.f.now + 1)

    def test_racing_holds_cannot_double_allocate_the_same_lot(self):
        self.adopt()
        self.grant(100)
        from concurrent.futures import ThreadPoolExecutor

        def reserve(index):
            try:
                self.change("hold", 0, 60, ref=f"race-{index}", key=f"race-{index}")
                return True
            except credits.CreditError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(result), 1)
        self.assertEqual(self.balance()["held"], 60)
        self.assert_invariants()

    def test_earliest_expiry_allocates_before_legacy_and_preserves_held_credit(self):
        self.grant(100)
        self.adopt()
        purchase = self.grant(60, kind="purchase")
        promo = self.grant(40)
        held = self.change("hold", 0, 50, key="hold")
        with self.f.repo.engine.connect() as connection:
            allocation = credit_expiry.read(
                connection,
                {"tenant": "sixnine", "user_id": self.f.ids["supervan"]},
                credit_expiry.ALLOCATION,
                held["entry"]["id"],
            )["payload"]["uses"]
        self.assertEqual(
            allocation,
            [
                {"lot": promo["entry"]["id"], "amount": 40},
                {"lot": purchase["entry"]["id"], "amount": 10},
            ],
        )
        self.f.now += 31 * 86400
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (200, 50))
        self.change("capture", -50, -50, key="terminal")
        self.assertEqual(self.balance()["balance"], 150)
        self.assertFalse([x for x in self.ledger() if x["kind"] == "expire"])
        self.assert_invariants()

    def test_release_after_expiry_voids_only_released_portion_once(self):
        self.adopt()
        self.grant(100)
        self.change("hold", 0, 40, key="hold")
        self.f.now += 31 * 86400
        self.assertEqual((self.balance()["balance"], self.balance()["held"]), (40, 40))
        self.change("release", 0, -40, key="terminal")
        original = self.ledger()
        self.change("release", 0, -40, key="terminal")
        self.assertEqual(self.ledger(), original)
        self.assertEqual(
            sorted(x["amount"] for x in original if x["kind"] == "expire"), [-60, -40]
        )
        self.assertEqual(self.balance()["balance"], 0)
        self.assert_invariants()

    def test_accepted_legacy_hold_and_original_waiver_remain_nonexpiring(self):
        self.grant(100)
        self.change("hold", 0, 60, key="hold")
        self.adopt()
        self.grant(1)
        self.f.now += 400 * 86400
        self.change("capture", -60, -60, key="terminal")
        self.change("waive", 60, key="waive")
        self.assertEqual(self.balance()["balance"], 100)
        self.assert_invariants()

    def test_waiver_restores_original_lots_without_extending_expiry(self):
        self.adopt()
        self.grant(100)
        self.change("hold", 0, 60, key="hold")
        self.change("capture", -60, -60, key="terminal")
        self.f.now += 31 * 86400
        self.change("waive", 60, key="waive")
        original = self.ledger()
        self.change("waive", 60, key="waive")
        self.assertEqual(self.ledger(), original)
        self.assertEqual(self.balance()["balance"], 0)
        self.assertEqual(
            sum(-x["amount"] for x in original if x["kind"] == "expire"), 100
        )
        self.assert_invariants()

    def test_original_purchase_refund_cannot_consume_another_pack(self):
        self.adopt()
        self.grant(100, kind="purchase", reference="first")
        self.f.now += 370 * 86400
        self.grant(200, kind="purchase", reference="second")
        with self.f.repo.transaction() as connection:
            row = credits._account(
                connection, "sixnine", self.f.ids["supervan"], now=self.f.now
            )
            self.assertEqual(
                credit_expiry.refundable_purchase(connection, row, "first", self.f.now),
                0,
            )
            self.assertEqual(
                credit_expiry.refundable_purchase(
                    connection, row, "second", self.f.now
                ),
                200,
            )
        with self.assertRaises(credits.CreditError):
            self.change("refund", -1, ref="first", ref_type="payment")
        self.assertEqual(self.balance()["balance"], 200)
        self.assert_invariants()

    def test_allocation_error_rolls_back_original_ledger_and_account(self):
        self.adopt()
        self.grant(100)
        before = self.ledger()
        with patch.object(credit_expiry, "save", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                self.change("hold", 0, 50)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.balance()["held"], 0)
        self.assert_invariants()

    def test_corrupt_state_fails_closed_and_tenant_reader_cannot_see_it(self):
        self.adopt()
        self.grant(100)
        with self.f.repo.transaction() as connection:
            row = {"tenant": "sixnine", "user_id": self.f.ids["supervan"]}
            saved = credit_expiry.read(connection, row)
            payload = copy.deepcopy(saved["payload"])
            payload["state"]["account_version"] += 1
            connection.execute(
                update(documents)
                .where(*credit_expiry.where(row, credit_expiry.STATE, "active"))
                .values(payload=payload)
            )
            self.assertIsNone(
                credit_expiry.read(connection, {**row, "tenant": "other"})
            )
        with self.assertRaises(credits.CreditError):
            self.balance()
        with self.assertRaises(credits.CreditError):
            self.grant(1)

    def test_invalid_policy_refused_and_unrelated_revisions_preserve_policy(self):
        for invalid in (
            None,
            True,
            {**credit_lots.POLICY, "purchase_months": 24},
            {**credit_lots.POLICY, "version": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(PricingError):
                self.f.service.publish(
                    self.f.admin,
                    {
                        "expected_revision": self.revision,
                        "rates": dict(DEFAULT_RATES),
                        "expiry_policy": invalid,
                    },
                )
        self.adopt()
        self.f.service.publish(
            self.f.admin,
            {
                "expected_revision": self.revision,
                "rates": dict(DEFAULT_RATES),
                "image_credits": 5,
            },
        )
        self.assertEqual(
            self.f.service.admin_prices(self.f.admin)["expiry_policy"],
            credit_lots.POLICY,
        )

    def test_adoption_refuses_existing_generation_and_image_work(self):
        from studio_platform.repository import Scope, jobs

        self.f.service.policy(
            self.f.admin,
            {
                "expected_revision": self.revision,
                "enabled": False,
                "reason": "Disposable pre-adoption work",
            },
            key="disable-fixture",
        )
        original = self.f.job(scope=Scope("sixnine", "supervan", "fixture"), cost=0)
        with self.assertRaisesRegex(PricingError, "credit_expiry_adoption_busy"):
            self.adopt()
        with self.f.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == original["id"])
                .values(status="cancelled")
            )
        self.grant(100)
        self.change("hold", 0, 20, ref_type="story_chat_image")
        with self.assertRaisesRegex(PricingError, "credit_expiry_adoption_busy"):
            self.adopt()

    def test_real_paid_event_keeps_purchase_time_and_refund_keeps_other_grant(self):
        import test_payments as payment_fixtures
        from studio_platform.credit_schema import prices
        from studio_platform.credits_pricing import DEFAULT_CONFIGURATION
        from studio_platform.payment_schema import payments

        payment = payment_fixtures.PaymentTests()
        payment.setUp()
        self.addCleanup(payment.tearDown)
        payment.now = epoch(2028, 2, 29)
        payment.policy(False)
        with payment.repo.transaction() as connection:
            connection.execute(
                insert(prices).values(
                    tenant=payment.scope.tenant_id,
                    revision="price-1",
                    rates=dict(DEFAULT_RATES),
                    configuration={
                        **copy.deepcopy(DEFAULT_CONFIGURATION),
                        "expiry_policy": dict(credit_lots.POLICY),
                    },
                    published_by=payment.user_id,
                    published_at=payment.now,
                )
            )
        original = payment.purchase()
        event = payment.event(original, event_id="evt_paid_expiry")
        payment.now += 10 * 86400
        self.assertEqual(payment.process(event)["state"], "processed")
        with payment.repo._budget_read() as connection:
            value = credits.account(
                connection,
                payment.scope.tenant_id,
                payment.scope.owner_id,
                now=payment.now,
            )
        self.assertEqual(value["expiry"]["next_at"], epoch(2029, 2, 28))
        payment.now = epoch(2029, 3, 2)
        payment.seed(200, key="unrelated-later-grant")
        refund = payment.event(original, kind="charge.refunded", amount_refunded=500)
        self.assertEqual(payment.process(refund)["state"], "processed")
        original_entries = payment.rows(entries)
        payment.process(refund)
        self.assertEqual(payment.rows(entries), original_entries)
        self.assertEqual(payment.balance()["balance"], 200)
        row = payment.rows(payments)[0]
        self.assertEqual((row["clawed_back_credits"], row["unmet_credits"]), (0, 500))
        self.assertEqual(
            sum(-e["amount"] for e in original_entries if e["kind"] == "expire"), 500
        )
        payment.assert_invariants()


if __name__ == "__main__":
    unittest.main()
