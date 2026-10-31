"""Tax-exclusive money contracts; fake Stripe, disposable SQL only."""

import copy
import unittest

from studio_platform import payment_tax as tax
from studio_platform import payments as core
from studio_platform import stripe_client as gateway
from studio_platform import payments_routes as routes
import test_payments as payment_fixtures
import test_stripe_client as stripe_fixtures
from studio_platform.payment_schema import payments, events
from studio_platform.credit_schema import entries


def session(*, behavior='exclusive', gross=550, base=500, tax_amount=50, **extra):
    charge = {
        'object': 'charge',
        'id': 'ch_original',
        'livemode': False,
        'currency': 'usd',
        'amount': gross,
        'payment_intent': 'pi_original',
    }
    return stripe_fixtures.session(
        amount_total=gross,
        amount_subtotal=base,
        total_details={
            'amount_tax': tax_amount,
            'amount_discount': 0,
            'amount_shipping': 0,
        },
        payment_status='paid',
        payment_intent={'id': 'pi_original', 'latest_charge': charge},
        line_items={
            'has_more': False,
            'data': [
                {
                    'quantity': 1,
                    'price': {
                        'id': 'price_starter',
                        'livemode': False,
                        'currency': 'usd',
                        'unit_amount': base,
                        'type': 'one_time',
                        'tax_behavior': behavior,
                        'active': False,
                    },
                }
            ],
        },
        **extra,
    )


class TaxProposalTests(unittest.TestCase):
    def test_exclusive_basis_validates_price_and_total(self):
        value = session()
        self.assertEqual(
            tax.session_basis(value),
            {
                'base_amount_cents': 500,
                'tax_amount_cents': 50,
                'tax_behavior': 'exclusive',
            },
        )
        for key, bad in [('amount_total', 549), ('amount_subtotal', 499)]:
            invalid = copy.deepcopy(value)
            invalid[key] = bad
            with self.assertRaises(ValueError):
                tax.session_basis(invalid)
        invalid = copy.deepcopy(value)
        invalid['total_details']['amount_discount'] = 1
        with self.assertRaises(ValueError):
            tax.session_basis(invalid)

    def test_inclusive_archived_settlement_is_retained(self):
        self.assertEqual(
            tax.session_basis(session(behavior='inclusive', gross=500, tax_amount=50))[
                'base_amount_cents'
            ],
            500,
        )

    def test_exclusive_partial_refund_is_not_invented_tax_allocation(self):
        value = {
            'amount_cents': 550,
            'base_amount_cents': 500,
            'tax_amount_cents': 50,
            'tax_behavior': 'exclusive',
        }
        self.assertEqual(tax.refund_base_amount({**value, 'amount_refunded': 550}), 500)
        with self.assertRaisesRegex(
            ValueError, 'payment_refund_tax_allocation_required'
        ):
            tax.refund_base_amount({**value, 'amount_refunded': 50})


class GatewayProposalTests(unittest.TestCase):
    def setUp(self):
        self.h = stripe_fixtures.StripeClientTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.g = gateway.StripeGateway(self.h.settings, client_factory=self.h.factory)

    def test_signed_event_normalization_and_fingerprint_stay_identical(self):
        raw, signature = self.h.signed(session())
        self.assertEqual(
            self.g.verify_event(raw, signature),
            self.h.gateway.verify_event(raw, signature),
        )
        self.h.factory.assert_not_called()

    def test_new_checkout_exclusive_and_base_gross_split(self):
        self.h.client.v1.prices.retrieve.return_value['tax_behavior'] = 'exclusive'
        self.h.client.v1.checkout.sessions.create.return_value = session()
        result = self.g.checkout(stripe_fixtures.payment(), idempotency_key='original')
        self.assertEqual(result['object']['amount_cents'], 550)
        self.assertEqual(result['object']['base_amount_cents'], 500)
        self.assertEqual(result['object']['tax_amount_cents'], 50)

    def test_paid_resolution_preserves_original_gross_and_price_basis(self):
        original = session()
        verified = self.g.verify_event(*self.h.signed(original))
        self.h.client.v1.checkout.sessions.retrieve.return_value = original
        proof = self.g.resolve_event(verified)
        self.assertEqual(proof['object']['amount_cents'], 550)
        self.assertEqual(proof['object']['base_amount_cents'], 500)
        self.assertTrue(
            all(proof['object'][k] == v for k, v in verified['object'].items())
        )

    def test_full_refund_resolves_original_session_and_same_charge(self):
        original = session()
        charge = copy.deepcopy(original['payment_intent']['latest_charge'])
        charge['amount_refunded'] = 550
        verified = self.g.verify_event(*self.h.signed(charge, kind='charge.refunded'))
        self.h.client.v1.checkout.sessions.list.return_value = {
            'has_more': False,
            'data': [{'id': original['id']}],
        }
        self.h.client.v1.checkout.sessions.retrieve.return_value = original
        proof = self.g.resolve_event(verified)
        self.assertEqual(proof['object']['base_amount_cents'], 500)
        self.assertEqual(proof['object']['amount_refunded'], 550)
        original['payment_intent']['latest_charge']['id'] = 'ch_foreign'
        with self.assertRaises(gateway.StripeGatewayError):
            self.g.resolve_event(verified)


class CoreProposalTests(unittest.TestCase):
    def setUp(self):
        self.h = payment_fixtures.PaymentTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.addCleanup(self.h.doCleanups)
        self.h.core = core.PaymentCore(
            self.h.repo,
            self.h.settings,
            terms_check=lambda _c, _p, user: {
                'version': 'reviewed-terms-v1',
                'accepted_at': user['terms_accepted_at'],
            },
        )
        self.h.pack['amount_cents'] = 5000

    def proved(self, value):
        proof = copy.deepcopy(value)
        proof['object'].update(
            base_amount_cents=5000, tax_amount_cents=500, tax_behavior='exclusive'
        )
        self.h.core.receive_event(value)
        return self.h.core.process_event(
            self.h.scope.tenant_id, value['id'], proof=proof
        )

    def test_paid_gross_credits_exact_original_once_and_gross_receipt(self):
        purchase = self.h.purchase(credits_amount=5500)
        value = self.h.event(purchase, amount_cents=5500)
        self.assertEqual(self.proved(value)['state'], 'processed')
        rows = self.h.rows(payments)
        self.assertEqual(rows[0]['amount_cents'], 5000)
        with self.h.repo.engine.connect() as c:
            money = tax.receipt_money(c, rows[0])
        receipt = routes.customer_receipt(rows[0], money=money)
        self.assertEqual(receipt['amount_cents'], 5500)
        self.assertEqual(receipt['base_amount_cents'], 5000)
        self.assertEqual(receipt['tax_amount_cents'], 500)
        before = len(self.h.rows(entries))
        self.proved(value)
        self.assertEqual(len(self.h.rows(entries)), before)

    def test_full_gross_refund_reverses_pack_and_bonus(self):
        purchase = self.h.purchase(credits_amount=5500)
        self.proved(self.h.event(purchase, amount_cents=5500))
        value = self.h.event(
            purchase, kind='charge.refunded', amount_cents=5500, amount_refunded=5500
        )
        self.assertEqual(self.proved(value)['state'], 'processed')
        row = self.h.rows(payments)[0]
        self.assertEqual((row['refunded_cents'], row['refund_credits']), (5000, 5500))
        with self.h.repo.engine.connect() as c:
            money = tax.receipt_money(c, row)
        self.assertEqual(money['refunded_cents'], 5500)

    def test_partial_tax_only_refund_enters_review_without_wallet_write(self):
        purchase = self.h.purchase(credits_amount=5500)
        self.proved(self.h.event(purchase, amount_cents=5500))
        before = self.h.rows(entries)
        value = self.h.event(
            purchase, kind='charge.refunded', amount_cents=5500, amount_refunded=500
        )
        result = self.proved(value)
        self.assertEqual(result['state'], 'review')
        self.assertEqual(
            self.h.rows(events)[-1]['reason_code'],
            'payment_refund_tax_allocation_required',
        )
        self.assertEqual(self.h.rows(entries), before)
        self.assertEqual(self.h.rows(payments)[0]['refund_credits'], 0)

    def test_refund_before_paid_keeps_original_events_and_no_free_credit(self):
        purchase = self.h.purchase(credits_amount=5500)
        self.proved(
            self.h.event(
                purchase,
                kind='charge.refunded',
                amount_cents=5500,
                amount_refunded=5500,
            )
        )
        self.proved(self.h.event(purchase, amount_cents=5500))
        row = self.h.rows(payments)[0]
        self.assertEqual(row['refund_credits'], 5500)
        self.assertEqual(row['clawed_back_credits'], 5500)

    def test_legacy_inclusive_partial_refund_behavior_unchanged(self):
        purchase = self.h.purchase(credits_amount=5500)
        self.assertEqual(self.h.process(self.h.event(purchase))['state'], 'processed')
        self.assertEqual(
            self.h.process(
                self.h.event(purchase, kind='charge.refunded', amount_refunded=2500)
            )['state'],
            'processed',
        )
        self.assertEqual(self.h.rows(payments)[0]['refund_credits'], 2750)


if __name__ == '__main__':
    unittest.main()
