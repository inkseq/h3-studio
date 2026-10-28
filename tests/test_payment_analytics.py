"""Optional anonymous attribution, original-money proof and fake SDK privacy."""

from copy import deepcopy
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import Mock, patch
import uuid

from sqlalchemy import select, update

from studio_platform import payment_analytics as analytics
from studio_platform.credit_schema import accounts, entries
from studio_platform.payment_schema import events, payments
from studio_platform.repository import documents
import test_payments as payment_fixtures
import test_payments_routes as route_fixtures

ANONYMOUS = str(uuid.uuid4())
CONTEXT = {'consent': True, 'distinct_id': ANONYMOUS}
PRIVATE = 'private-prompt-token@example.invalid'


class AnalyticsCase(unittest.TestCase):
    def setUp(self):
        self.fixture = payment_fixtures.PaymentTests()
        self.fixture.setUp()
        self.f = self.fixture

    def tearDown(self):
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def remember(self, payment, context=CONTEXT, principal=None):
        return analytics.remember(
            self.f.repo,
            self.f.settings,
            principal or self.f.principal,
            payment['payment'],
            context,
        )

    def observe(self, event, *, client=None):
        client = client or Mock(capture=Mock(return_value=str(uuid.uuid4())))
        result = analytics.observe(
            self.f.repo,
            self.f.settings,
            self.f.scope.tenant_id,
            event['id'],
            client_factory=lambda: client,
        )
        return result, client

    def document_rows(self):
        with self.f.repo.engine.connect() as c:
            return list(
                c.execute(
                    select(documents).where(documents.c.kind == analytics.KIND)
                ).mappings()
            )

    def test_only_positive_consent_and_anonymous_uuid4_or7_are_accepted(self):
        self.assertEqual(analytics.anonymous_context(CONTEXT), CONTEXT)
        uuid7 = str(uuid.UUID('018b2c34-1234-7000-8000-123456789abc'))
        self.assertIsNotNone(
            analytics.anonymous_context({'consent': True, 'distinct_id': uuid7})
        )
        for value in (
            None,
            {},
            False,
            {'consent': 1, 'distinct_id': ANONYMOUS},
            {'consent': False, 'distinct_id': ANONYMOUS},
            {'consent': True, 'distinct_id': PRIVATE},
            {
                'consent': True,
                'distinct_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'name')),
            },
            {**CONTEXT, 'amount': 500},
            {**CONTEXT, 'distinct_id': ANONYMOUS.upper()},
        ):
            with self.subTest(value=value):
                self.assertIsNone(analytics.anonymous_context(value))

    def test_attribution_is_once_scoped_and_never_changes_frozen_money(self):
        payment = self.f.purchase()
        original = deepcopy(self.f.rows(payments))
        self.assertTrue(self.remember(payment))
        self.assertTrue(
            self.remember(payment, {**CONTEXT, 'distinct_id': str(uuid.uuid4())})
        )
        rows = self.document_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['owner_id'], self.f.principal.owner)
        self.assertEqual(rows[0]['document_id'], payment['payment']['id'])
        self.assertEqual(
            rows[0]['payload'],
            {**CONTEXT, 'environment': 'staging', 'pack_id': 'starter'},
        )
        self.assertEqual(self.f.rows(payments), original)

    def test_invalid_context_wrong_owner_and_local_environment_store_nothing(self):
        payment = self.f.purchase()
        self.assertFalse(self.remember(payment, None))
        self.assertFalse(
            self.remember(payment, {'consent': False, 'distinct_id': ANONYMOUS})
        )
        self.assertFalse(
            self.remember(payment, principal=replace(self.f.principal, owner='other'))
        )
        self.f.settings.public_origin = 'http://localhost:8000'
        self.assertFalse(self.remember(payment))
        self.assertEqual(self.document_rows(), [])

    def test_absent_attribution_never_constructs_sdk(self):
        payment = self.f.purchase()
        event = self.f.event(payment)
        self.f.process(event)
        factory = Mock(side_effect=AssertionError('SDK must stay disabled'))
        self.assertFalse(
            analytics.observe(
                self.f.repo,
                self.f.settings,
                self.f.scope.tenant_id,
                event['id'],
                client_factory=factory,
            )
        )
        factory.assert_not_called()

    def test_original_purchase_entry_is_success_even_when_wallet_is_zero(self):
        payment = self.f.purchase()
        self.remember(payment)
        event = self.f.event(payment)
        self.f.process(event)
        with self.f.repo.transaction() as c:
            c.execute(update(accounts).values(balance=0, held=0))
        before = [self.f.rows(table) for table in (payments, events, entries)]
        result, client = self.observe(event)
        self.assertTrue(result)
        args, kwargs = client.capture.call_args
        self.assertEqual(args, ('payment_succeeded',))
        self.assertEqual(kwargs['distinct_id'], ANONYMOUS)
        self.assertEqual(
            kwargs['properties'],
            {
                'environment': 'staging',
                'pack_id': 'starter',
                '$process_person_profile': False,
                '$geoip_disable': True,
            },
        )
        self.assertNotIn(payment['payment']['user_id'], json.dumps(kwargs))
        self.assertNotIn(payment['payment']['id'], json.dumps(kwargs))
        self.assertNotIn('amount', json.dumps(kwargs))
        self.assertEqual(
            [self.f.rows(table) for table in (payments, events, entries)], before
        )
        _, again = self.observe(event)
        self.assertEqual(again.capture.call_args.kwargs['uuid'], kwargs['uuid'])

    def test_paid_row_without_original_grant_does_not_emit_success(self):
        payment = self.f.purchase()
        self.remember(payment)
        event = self.f.event(payment)
        self.f.process(event)
        with self.f.repo.transaction() as c:
            c.execute(
                update(entries)
                .where(entries.c.kind == 'purchase')
                .values(origin='other')
            )
        result, client = self.observe(event)
        self.assertFalse(result)
        client.capture.assert_not_called()

    def test_signed_paid_event_still_created_or_review_is_not_success(self):
        payment = self.f.purchase()
        self.remember(payment)
        event = self.f.event(payment)
        self.f.core.receive_event(event)
        for state in ('received', 'deferred', 'review'):
            with self.f.repo.transaction() as c:
                c.execute(
                    update(events).values(
                        status=state, payment_id=payment['payment']['id']
                    )
                )
            result, client = self.observe(event)
            self.assertFalse(result)
            client.capture.assert_not_called()

    def test_purchase_shaped_admin_or_held_entry_is_not_original_grant(self):
        payment = self.f.purchase()
        self.remember(payment)
        event = self.f.event(payment)
        self.f.process(event)
        for changes in (
            {'actor': 'admin:manual', 'held_delta': 0},
            {'actor': 'stripe:webhook', 'held_delta': 1},
        ):
            with self.subTest(changes=changes):
                with self.f.repo.transaction() as c:
                    c.execute(
                        update(entries)
                        .where(entries.c.kind == 'purchase')
                        .values(**changes)
                    )
                result, client = self.observe(event)
                self.assertFalse(result)
                client.capture.assert_not_called()

    def test_confirmed_async_failure_emits_but_expiry_does_not(self):
        payment = self.f.purchase()
        self.remember(payment)
        event = self.f.event(payment, kind='checkout.session.async_payment_failed')
        self.f.process(event)
        result, client = self.observe(event)
        self.assertTrue(result)
        self.assertEqual(client.capture.call_args.args, ('payment_failed',))
        second = self.f.purchase(key='expired-original')
        self.remember(second)
        expired = self.f.event(second, kind='checkout.session.expired')
        self.f.process(expired)
        result, client = self.observe(expired)
        self.assertFalse(result)
        client.capture.assert_not_called()

    def test_capture_failure_never_rewrites_or_defers_committed_money(self):
        payment = self.f.purchase()
        self.remember(payment)
        event = self.f.event(payment)
        self.f.process(event)
        before = [self.f.rows(table) for table in (payments, events, entries)]
        client = Mock(capture=Mock(side_effect=RuntimeError(PRIVATE)))
        result, _ = self.observe(event, client=client)
        self.assertFalse(result)
        self.assertEqual(
            [self.f.rows(table) for table in (payments, events, entries)], before
        )
        client.shutdown.assert_called_once()

    def test_foreign_tenant_mode_or_ambient_identity_cannot_capture(self):
        payment = self.f.purchase()
        self.remember(payment)
        event = self.f.event(payment)
        self.f.process(event)
        factory = Mock(side_effect=AssertionError('no SDK'))
        self.assertFalse(
            analytics.observe(
                self.f.repo,
                self.f.settings,
                'foreign',
                event['id'],
                client_factory=factory,
            )
        )
        with self.f.repo.transaction() as c:
            c.execute(update(events).values(account_scope='acct_foreign'))
        result, client = self.observe(event)
        self.assertFalse(result)
        client.capture.assert_not_called()


class RouteAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.f = route_fixtures.PaymentRouteTests()
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()
        self.f.doCleanups()

    def test_optional_analytics_is_not_passed_to_original_money_fingerprint(self):
        saved = self.f.save()
        self.f.core.begin_checkout.return_value = {
            'payment': {'id': str(uuid.uuid4()), 'request_fingerprint': 'f' * 64},
            'dispatch': True,
            'stripe_idempotency_key': 'original',
        }
        self.f.core.bind_checkout.return_value = {
            'checkout_url': 'https://checkout.stripe.com/c/pay/original'
        }
        with patch('studio_platform.payments_routes.remember_analytics') as remember:
            self.f.api.checkout(
                self.f.user, {'pack_id': 'starter', 'analytics': CONTEXT}, key='buy'
            )
        self.f.core.begin_checkout.assert_called_once_with(
            self.f.user,
            {**route_fixtures.PACK, 'revision': saved['revision']},
            'buy',
            account_scope='acct_testaccount',
            livemode=False,
        )
        self.assertEqual(remember.call_args.args[-1], CONTEXT)

    def test_malformed_optional_context_does_not_break_checkout(self):
        self.f.save()
        self.f.core.begin_checkout.return_value = {
            'payment': {'checkout_url': 'https://checkout.stripe.com/c/pay/original'},
            'dispatch': False,
        }
        self.assertIn(
            'url',
            self.f.api.checkout(
                self.f.user, {'pack_id': 'starter', 'analytics': PRIVATE}, key='same'
            ),
        )


class SDKPrivacyTests(unittest.TestCase):
    def event(self):
        return {
            'event': 'payment_succeeded',
            'distinct_id': ANONYMOUS,
            'uuid': str(uuid.uuid4()),
            'timestamp': '2026-10-28T03:00:00+00:00',
            'properties': {
                'environment': 'staging',
                'pack_id': 'starter',
                'user_id': PRIVATE,
                'amount': 500,
                '$set': {'email': PRIVATE},
            },
            'request': {'body': PRIVATE},
            'groups': {'account': PRIVATE},
        }

    def test_closed_event_rebuilds_shape_and_drops_all_sdk_enrichment(self):
        result = analytics.closed_event(self.event())
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(
            set(result), {'event', 'distinct_id', 'uuid', 'timestamp', 'properties'}
        )
        self.assertEqual(
            set(result['properties']),
            {'environment', 'pack_id', '$process_person_profile', '$geoip_disable'},
        )

    def test_sdk_pin_is_checked_before_import_or_capture(self):
        with patch.object(analytics, 'version', return_value='0.0.0'):
            with self.assertRaisesRegex(ValueError, 'sdk_mismatch'):
                analytics.sdk_client()

    def test_actual_pinned_sdk_has_no_consumers_and_fake_transport_gets_closed_bytes(
        self,
    ):
        import posthog.request
        from posthog import new_context, tag

        sent = []

        def post(_session, url, **kwargs):
            sent.append((url, json.loads(kwargs['data']), kwargs['timeout']))
            return Mock(status_code=200, json=lambda: {'status': 1})

        with (
            patch('requests.sessions.Session.post', post),
            patch(
                'socket.socket.connect', side_effect=AssertionError('network forbidden')
            ),
            patch(
                'threading.Thread.start', side_effect=AssertionError('thread forbidden')
            ),
            patch.dict(os.environ, {'POSTHOG_RELEASE_ID': PRIVATE}),
        ):
            client = analytics.sdk_client()
            self.assertTrue(client.sync_mode)
            self.assertIsNone(client.consumers)
            self.assertIsNone(client.poller)
            with new_context():
                tag('email', PRIVATE)
                result = client.capture(
                    'payment_succeeded',
                    distinct_id=ANONYMOUS,
                    properties=self.event()['properties'],
                    uuid=self.event()['uuid'],
                )
            self.assertIsNotNone(result)
            self.assertEqual(client.queue.qsize(), 0)
            client.shutdown()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], analytics.HOST + '/batch/')
        self.assertEqual(sent[0][2], 2)
        self.assertNotIn(PRIVATE, json.dumps(sent))
        self.assertEqual(
            set(sent[0][1]['batch'][0]['properties']),
            {'environment', 'pack_id', '$process_person_profile', '$geoip_disable'},
        )
        # Actual SDK transport has independently bounded connection retries;
        # max_retries=0 must not be misrepresented as a transport-level zero.
        self.assertEqual(
            posthog.request._get_session().get_adapter('https://').max_retries.connect,
            2,
        )
