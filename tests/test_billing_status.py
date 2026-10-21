"""Admin-only billing readiness; no keys, provider calls or money changes."""

from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from sqlalchemy import Column, MetaData, String, Table, insert

from studio_platform.auth import Principal
from studio_platform.credit_schema import metadata as credit_metadata
from studio_platform.identity_schema import metadata, users
from studio_platform.payment_schema import metadata as payment_metadata, events
from studio_platform.payments_routes import BillingError, PaymentAPI
from studio_platform.schema_version import PAYMENT_REVISION
from studio_platform.stripe_client import StripeGatewayError
from test_platform_repository import LedgerCase


class BillingStatusTests(LedgerCase):
    def setUp(self):
        super().setUp()
        with self.repo.transaction() as conn:
            metadata.create_all(conn)
            credit_metadata.create_all(conn)
            payment_metadata.create_all(conn)
            marker = Table(
                'alembic_version',
                MetaData(),
                Column('version_num', String(32), primary_key=True),
            )
            marker.create(conn)
            conn.execute(marker.insert().values(version_num=PAYMENT_REVISION))
            for owner, role in (('admin', 'admin'), ('user', 'user')):
                conn.execute(
                    insert(users).values(
                        id=str(uuid.uuid4()),
                        tenant='sixnine',
                        owner=owner,
                        role=role,
                        status='active',
                        display_name=owner,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.gateway = Mock()
        self.config = SimpleNamespace(
            mode='live',
            account_id='acct_original',
            webhook_secret=None,
            webhook_endpoint_id=None,
        )
        self.gateway.configuration.return_value = self.config
        self.api = PaymentAPI(
            self.repo,
            SimpleNamespace(tenant_id='sixnine', stripe_mode='off'),
            gateway=self.gateway,
            core=Mock(),
        )
        self.admin = Principal('admin', 'browser:admin')

    def test_inert_live_readback_is_off_and_never_calls_stripe(self):
        value = self.api.admin_status(self.admin)
        self.assertEqual(value['configured_mode'], 'live')
        self.assertFalse(value['checkout_enabled'])
        self.assertFalse(value['credit_charging_enabled'])
        self.assertFalse(value['schema_ready'])  # 005 cannot accept LIVE obligations.
        self.assertFalse(value['webhook_configured'])
        self.assertIsNone(value['webhook_endpoint_id'])
        self.assertEqual(
            self.gateway.method_calls,
            [('configuration', (), {'require_webhook': False})],
        )
        self.assertNotIn('secret', str(value))

    def test_only_current_canonical_human_admin_can_read(self):
        for principal in (
            Principal('user', 'browser:user'),
            Principal('admin', 'key:test', machine=True),
        ):
            with self.assertRaises(BillingError) as caught:
                self.api.admin_status(principal)
            self.assertEqual(caught.exception.status, 403)
        self.gateway.configuration.assert_not_called()

    def test_last_event_is_filtered_by_exact_account_and_mode(self):
        self.config.mode = 'test'
        self.config.webhook_secret = 'synthetic-never-returned'
        with self.repo.transaction() as conn:
            for ident, account, received in (
                ('evt_own', 'acct_original', 3.0),
                ('evt_other', 'acct_other', 99.0),
            ):
                conn.execute(
                    events.insert().values(
                        event_id=ident,
                        tenant='sixnine',
                        account_scope=account,
                        livemode=0,
                        type='checkout.session.completed',
                        verified_payload={},
                        fingerprint='a' * 64,
                        received_at=received,
                        status='received',
                    )
                )
        value = self.api.admin_status(self.admin)
        self.assertEqual(value['last_signed_event_at'], 3.0)
        self.assertFalse(value['checkout_enabled'])
        self.assertNotIn('synthetic-never-returned', str(value))

    def test_unconfigured_readback_stays_off_with_safe_reason(self):
        self.gateway.configuration.side_effect = StripeGatewayError(
            'billing_unavailable'
        )
        value = self.api.admin_status(self.admin)
        self.assertIsNone(value['configured_mode'])
        self.assertFalse(value['checkout_enabled'])
        self.assertEqual(value['reason_code'], 'billing_unavailable')
