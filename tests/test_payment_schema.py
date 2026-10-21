"""Exact payment reader/adoption tests; no Stripe, credentials or cloud calls."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import CheckConstraint, create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError

from studio_platform import credit_schema as credit
from studio_platform import identity_schema as identity
from studio_platform import payment_schema as payment
from studio_platform.backup import BackupError, _export_business_db
from studio_platform.migrations.versions import v20261020_0005_stripe_payments as frozen
from studio_platform.migrations.versions import (
    v20261021_0006_stripe_live_mode as live_frozen,
)
from studio_platform.schema_baseline import (
    BaselineError,
    adopt_credits,
    adopt_payments,
    compare_schema,
    frozen_metadata,
    metadata_signature,
)
from studio_platform.schema_registry import platform_metadata
from studio_platform.schema_version import (
    CREDIT_REVISION,
    CREDIT_REVISIONS,
    IDENTITY_REVISIONS,
    PAYMENT_REVISION,
    LIVE_PAYMENT_REVISION,
    PAYMENT_REVISIONS,
    WORKFLOW_REVISIONS,
)
import test_credit_schema as fixtures

PAYMENT_ID = '33333333-3333-4333-8333-333333333333'


def seed_payment(connection, *, livemode=0):
    values = dict(
        id=PAYMENT_ID,
        tenant='sixnine',
        user_id=fixtures.USER,
        pack_id='starter',
        pack_revision='original',
        pack_snapshot={'id': 'starter', 'credits': 500, 'amount_cents': 500},
        price_id='price_test_original',
        credits=500,
        amount_cents=500,
        currency='usd',
        account_scope='test-account-original',
        livemode=livemode,
        idempotency_key='original-checkout',
        request_fingerprint='a' * 64,
        terms_version='terms-original',
        terms_accepted_at=1.0,
        status='refunded',
        created_at=2.0,
        updated_at=5.0,
        checkout_started_at=2.0,
        checkout_bound_at=3.0,
        stripe_checkout_session_id='cs_test_original',
        stripe_payment_intent_id='pi_original',
        stripe_charge_id='ch_original',
        checkout_url='https://checkout.stripe.com/c/pay/test_original',
        expires_at=100.0,
        paid_at=4.0,
        receipt_url='https://pay.stripe.com/receipts/test_original',
        refunded_cents=200,
        refund_credits=200,
        clawed_back_credits=150,
        unmet_credits=50,
        dispute_open=0,
        stripe_dispute_id=None,
    )
    connection.execute(payment.payments.insert().values(**values))
    connection.execute(
        payment.events.insert().values(
            event_id='evt_original',
            tenant='sixnine',
            account_scope=values['account_scope'],
            livemode=livemode,
            type='charge.refunded',
            verified_payload={'id': 'ch_original', 'amount_refunded': 200},
            fingerprint='b' * 64,
            received_at=5.0,
            processed_at=5.0,
            status='review',
            payment_id=PAYMENT_ID,
            reason_code='refund_unmet_credits',
        )
    )
    return values


class PaymentReaderTests(unittest.TestCase):
    def setUp(self):
        platform_metadata()
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        frozen_metadata().create_all(self.engine)

    def family(self, *, payments=True, revision=PAYMENT_REVISION):
        identity.metadata.create_all(self.engine)
        credit.metadata.create_all(self.engine)
        if payments:
            payment.frozen_metadata(revision=revision).create_all(self.engine)

    def test006_exact_reader_does_not_change005_or_import_payment_sdks(self):
        self.assertEqual(
            hashlib.sha256(
                Path(frozen.__file__).read_bytes().replace(b'\r\n', b'\n')
            ).hexdigest(),
            'e95c8a4bbbcaaecad0c93875c2e2e29714a320e602df9605b7429eb9e9be1fda',
        )
        before = metadata_signature(payment.frozen_metadata(), self.engine.dialect)
        after = metadata_signature(
            payment.frozen_metadata(revision=LIVE_PAYMENT_REVISION),
            self.engine.dialect,
        )
        for name in payment.TABLE_NAMES:
            before_checks = before[name].pop('checks')
            after_checks = after[name].pop('checks')
            self.assertEqual(
                [
                    check
                    for check in before_checks
                    if check != payment.normalize_check('livemode = 0')
                ],
                [
                    check
                    for check in after_checks
                    if check != payment.normalize_check(live_frozen.MODE_CHECK)
                ],
            )
        self.assertEqual(before, after)
        self.family(revision=LIVE_PAYMENT_REVISION)
        fixtures.marker(self.engine, LIVE_PAYMENT_REVISION)
        statements = []
        event.listen(
            self.engine,
            'before_cursor_execute',
            lambda _c, _s, sql, *_: statements.append(sql),
        )
        with (
            self.engine.connect() as connection,
            patch.dict('sys.modules', {'alembic': None, 'dbos': None, 'stripe': None}),
        ):
            self.assertTrue(payment.ready(connection))
            self.assertEqual(
                payment.recognized_revision(connection), LIVE_PAYMENT_REVISION
            )
            self.assertEqual(
                payment.sqlite_revision(connection.connection.driver_connection),
                LIVE_PAYMENT_REVISION,
            )
            self.assertTrue(compare_schema(connection)['matches'])
            self.assertTrue(credit.ready(connection))
        self.assertFalse(
            any(
                sql.lstrip()
                .upper()
                .startswith(('CREATE', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'DROP'))
                for sql in statements
            )
        )
        self.assertIn(LIVE_PAYMENT_REVISION, PAYMENT_REVISIONS)
        for revisions in (CREDIT_REVISIONS, IDENTITY_REVISIONS, WORKFLOW_REVISIONS):
            self.assertIn(LIVE_PAYMENT_REVISION, revisions)
        self.assertEqual(
            payment.metadata.tables['platform_payments'].c.livemode.type.python_type,
            int,
        )
        self.assertIn(
            'livemode = 0',
            [
                str(c.sqltext)
                for c in payment.payments.constraints
                if isinstance(c, CheckConstraint)
            ],
        )

    def test006_requires_matching_marker_and_both_exact_tables(self):
        for marker, change in (
            (None, None),
            (PAYMENT_REVISION, None),
            (LIVE_PAYMENT_REVISION, 'mixed'),
            (LIVE_PAYMENT_REVISION, 'missing_bound'),
            (LIVE_PAYMENT_REVISION, 'renamed_bound'),
            (LIVE_PAYMENT_REVISION, 'swapped_names'),
            ('20261021_0007', None),
        ):
            with self.subTest(marker=marker, change=change):
                engine = create_engine('sqlite://')
                self.addCleanup(engine.dispose)
                live = payment.frozen_metadata(revision=LIVE_PAYMENT_REVISION)
                if change == 'mixed':
                    live.tables['platform_payments'].create(engine)
                    payment.events.create(engine)
                else:
                    table = live.tables['platform_payments']
                    constraint = next(
                        c
                        for c in table.constraints
                        if c.name == 'ck_platform_payments_mode'
                    )
                    if change == 'missing_bound':
                        table.constraints.remove(constraint)
                    elif change == 'renamed_bound':
                        constraint.name = 'unknown_mode_constraint'
                    elif change == 'swapped_names':
                        status = next(
                            c
                            for c in table.constraints
                            if c.name == 'ck_platform_payments_status'
                        )
                        constraint.name, status.name = status.name, constraint.name
                    live.create_all(engine)
                if marker:
                    fixtures.marker(engine, marker)
                with engine.connect() as connection:
                    for validator, target in (
                        (payment.validate_connection, connection),
                        (
                            payment.validate_sqlite,
                            connection.connection.driver_connection,
                        ),
                    ):
                        with self.assertRaises(payment.PaymentSchemaError):
                            validator(target)

    def test006_rejects005_shape_and_missing_family_but_does_not_rewrite_marker(self):
        self.family()
        fixtures.marker(self.engine, LIVE_PAYMENT_REVISION)
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(
                payment.PaymentSchemaError, 'shape_requires_review'
            ):
                payment.ready(connection)
            with self.assertRaisesRegex(BaselineError, 'shape_requires_review'):
                compare_schema(connection)
        payment.events.drop(self.engine)
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(payment.PaymentSchemaError, 'partial_family'):
                payment.validate_connection(connection)
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                LIVE_PAYMENT_REVISION,
            )

    def test006_keeps_original_refund_bounds_and_accepts_only_integer_modes_zero_one(
        self,
    ):
        self.family(revision=LIVE_PAYMENT_REVISION)
        fixtures.marker(self.engine, LIVE_PAYMENT_REVISION)
        with self.engine.begin() as connection:
            seed_payment(connection, livemode=1)
        for updates in (
            {'livemode': -1},
            {'livemode': 0.5},
            {'livemode': 2},
            {'refunded_cents': 501},
            {'unmet_credits': 49},
            {'clawed_back_credits': 201},
            {'dispute_open': 2},
        ):
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(payment.payments.update().values(**updates))
        for mode in (-1, 0.5, 2):
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(payment.events.update().values(livemode=mode))
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(payment.payments.c.livemode)).scalar_one(), 1
            )
            self.assertEqual(
                connection.execute(select(payment.events.c.livemode)).scalar_one(), 1
            )

    def test006_definition_cannot_adopt_without_verified_postgres_transaction(self):
        for bind, verified in (
            (
                SimpleNamespace(
                    dialect=SimpleNamespace(name='postgresql'),
                    in_transaction=lambda: True,
                ),
                False,
            ),
            (
                SimpleNamespace(
                    dialect=SimpleNamespace(name='sqlite'), in_transaction=lambda: True
                ),
                True,
            ),
            (
                SimpleNamespace(
                    dialect=SimpleNamespace(name='postgresql'),
                    in_transaction=lambda: False,
                ),
                True,
            ),
        ):
            with (
                patch('alembic.context.is_offline_mode', return_value=False),
                patch('alembic.context.config', create=True) as config,
                patch('alembic.op.get_bind', return_value=bind),
                patch('alembic.op.drop_constraint') as drop,
                patch('alembic.op.create_check_constraint') as create,
            ):
                config.attributes = {'verified_payment_live_mode': verified}
                with self.assertRaisesRegex(
                    RuntimeError, 'live_verified_adoption_required'
                ):
                    live_frozen.upgrade()
                drop.assert_not_called()
                create.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'destructive_downgrade'):
            live_frozen.downgrade()

    def test_readers_are_optional_inert_and_do_not_import_sdk_or_alembic(self):
        statements = []
        event.listen(
            self.engine,
            'before_cursor_execute',
            lambda _c, _s, sql, *_: statements.append(sql),
        )
        with (
            self.engine.connect() as connection,
            patch.dict('sys.modules', {'alembic': None, 'dbos': None, 'stripe': None}),
        ):
            self.assertFalse(payment.ready(connection))
            self.assertFalse(payment.validate_connection(connection))
            self.assertFalse(
                payment.validate_sqlite(connection.connection.driver_connection)
            )
            self.assertTrue(compare_schema(connection)['matches'])
        self.assertFalse(
            any(
                sql.lstrip()
                .upper()
                .startswith(('CREATE', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'DROP'))
                for sql in statements
            )
        )
        self.assertEqual(
            set(platform_metadata(include_payments=True).tables)
            - set(platform_metadata().tables),
            payment.TABLE_NAMES,
        )

    def test_exact_future_family_is_readable_but_only005_enables_authority(self):
        self.family()
        with self.engine.connect() as connection:
            self.assertTrue(payment.validate_connection(connection))
            self.assertTrue(
                payment.validate_sqlite(connection.connection.driver_connection)
            )
            self.assertTrue(compare_schema(connection)['matches'])
            self.assertFalse(payment.ready(connection))
        fixtures.marker(self.engine, PAYMENT_REVISION)
        statements = []
        event.listen(
            self.engine,
            'before_cursor_execute',
            lambda _c, _s, sql, *_: statements.append(sql),
        )
        with (
            self.engine.connect() as connection,
            patch.object(
                payment,
                'validate_connection',
                side_effect=AssertionError('hot-path reflection'),
            ),
        ):
            self.assertTrue(payment.ready(connection))
            self.assertTrue(credit.ready(connection))
        self.assertLessEqual(len(statements), 8, statements)
        with self.engine.connect() as connection:
            self.assertTrue(compare_schema(connection)['matches'])
        for revisions in (CREDIT_REVISIONS, IDENTITY_REVISIONS, WORKFLOW_REVISIONS):
            self.assertIn(PAYMENT_REVISION, revisions)

    def test005_preserves_workos_admin_and_original_identity_authority(self):
        from studio_platform.auth import Principal
        from studio_platform.identity import account_identity
        from studio_platform.identity_admin import IdentityAdmin
        from studio_platform.workos_auth import WorkOSAuth

        self.family()
        fixtures.marker(self.engine, PAYMENT_REVISION)
        settings = SimpleNamespace(tenant_id='sixnine')
        with self.engine.begin() as connection:
            fixtures.seed(connection)
            connection.execute(identity.users.update().values(role='admin'))
            WorkOSAuth(None, None, settings)._ready(connection)
            principal = Principal(owner='supervan', actor_id='original-session')
            self.assertEqual(
                IdentityAdmin(None, settings)._actor(connection, principal)['id'],
                fixtures.USER,
            )
            self.assertEqual(
                account_identity(connection, 'sixnine', 'supervan')['id'], fixtures.USER
            )

    def test_backup_restore_preserves_absence_or_payment_refund_and_event_history(self):
        from studio_platform.backup import backup_local, restore_local, verify_local
        from studio_platform.storage import LocalObjectStore

        for revision in (CREDIT_REVISION, PAYMENT_REVISION, LIVE_PAYMENT_REVISION):
            present = revision in PAYMENT_REVISIONS
            with (
                self.subTest(revision=revision),
                tempfile.TemporaryDirectory() as directory,
            ):
                engine = create_engine('sqlite://')
                self.addCleanup(engine.dispose)
                frozen_metadata().create_all(engine)
                identity.metadata.create_all(engine)
                credit.metadata.create_all(engine)
                if present:
                    payment.frozen_metadata(revision=revision).create_all(engine)
                fixtures.marker(engine, revision)
                with engine.begin() as connection:
                    fixtures.seed(connection)
                    if present:
                        seed_payment(
                            connection, livemode=int(revision == LIVE_PAYMENT_REVISION)
                        )
                root = Path(directory)
                source = root / 'source.sqlite3'
                with (
                    engine.connect() as connection,
                    closing(sqlite3.connect(source)) as target,
                ):
                    connection.connection.driver_connection.backup(target)
                store = LocalObjectStore(root / 'objects')
                backup_local(source, store.root, root / 'backup')
                manifest = verify_local(root / 'backup')
                self.assertEqual(
                    payment.TABLE_NAMES <= set(manifest['tables']), present
                )
                restore_local(root / 'backup', root / 'restored')
                for path in (
                    source,
                    root / 'backup/database.sqlite3',
                    root / 'restored/platform.sqlite3',
                ):
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(
                            payment.validate_sqlite(db, published=path != source),
                            present,
                        )
                        self.assertTrue(credit.validate_sqlite(db))
                        self.assertEqual(
                            db.execute(
                                'SELECT balance,held FROM platform_credit_accounts'
                            ).fetchone(),
                            (100, 20),
                        )
                        if present:
                            self.assertEqual(
                                db.execute(
                                    'SELECT livemode FROM platform_payments'
                                ).fetchone(),
                                (int(revision == LIVE_PAYMENT_REVISION),),
                            )
                            self.assertEqual(
                                db.execute(
                                    'SELECT paid_at,refunded_cents,refund_credits,clawed_back_credits,unmet_credits FROM platform_payments'
                                ).fetchone(),
                                (4.0, 200, 200, 150, 50),
                            )
                            self.assertEqual(
                                db.execute(
                                    'SELECT event_id,status,reason_code FROM platform_stripe_events'
                                ).fetchone(),
                                ('evt_original', 'review', 'refund_unmet_credits'),
                            )
                with closing(sqlite3.connect(root / 'backup/database.sqlite3')) as db:
                    names = {
                        row[0]
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    self.assertFalse(
                        {'platform_accounts', 'platform_sessions', 'alembic_version'}
                        & names
                    )

    def test_partial_or_recorded_missing_family_cannot_be_backed_up(self):
        payment.payments.create(self.engine)
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(payment.PaymentSchemaError, 'partial_family'):
                payment.ready(connection)
            with self.assertRaisesRegex(BaselineError, 'partial_family'):
                compare_schema(connection)
        payment.payments.drop(self.engine)
        self.family(payments=False)
        fixtures.marker(self.engine, PAYMENT_REVISION)
        with self.engine.connect() as connection:
            self.assertFalse(compare_schema(connection)['matches'])
            with self.assertRaisesRegex(payment.PaymentSchemaError, 'partial_family'):
                payment.ready(connection)
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    BackupError, 'payment_schema_partial_family'
                ):
                    _export_business_db(
                        connection.connection.driver_connection,
                        Path(directory) / 'refused.sqlite3',
                    )

    def test_extra_columns_checks_and_unknown_revisions_fail_closed(self):
        self.family()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                'ALTER TABLE platform_payments ADD COLUMN secret TEXT'
            )
        with self.engine.connect() as connection:
            self.assertFalse(compare_schema(connection)['matches'])
            for validator, target in (
                (payment.validate_connection, connection),
                (payment.validate_sqlite, connection.connection.driver_connection),
            ):
                with self.assertRaisesRegex(
                    payment.PaymentSchemaError, 'shape_requires_review'
                ):
                    validator(target)
        for expression in (
            'livemode = 1',
            "status IN ('received', 'processed', 'deferred', 'REVIEW')",
            'dispute_open >= 0 AND dispute_open <= 1 OR 1=1',
        ):
            with self.assertRaises(payment.PaymentSchemaError):
                payment.normalize_check(expression)
        fixtures.marker(self.engine, '20261020_9999')
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(
                payment.PaymentSchemaError, 'revision_requires_review'
            ):
                payment.ready(connection)
            with self.assertRaisesRegex(BaselineError, 'revision_requires_review'):
                compare_schema(connection)

    def test_database_enforces_testmode_correlation_dedup_and_refund_bounds(self):
        self.family()
        with self.engine.begin() as connection:
            original = seed_payment(connection)
        for updates in (
            {'livemode': 1},
            {'refunded_cents': 501},
            {'unmet_credits': 49},
            {'clawed_back_credits': 201},
            {'dispute_open': 2},
        ):
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(payment.payments.update().values(**updates))
        for field in (
            'idempotency_key',
            'stripe_checkout_session_id',
            'stripe_payment_intent_id',
            'stripe_charge_id',
        ):
            values = {**original, 'id': '44444444-4444-4444-8444-444444444444'}
            for key in (
                'idempotency_key',
                'stripe_checkout_session_id',
                'stripe_payment_intent_id',
                'stripe_charge_id',
            ):
                if key != field:
                    values[key] += '_another'
            with (
                self.subTest(field=field),
                self.assertRaises(IntegrityError),
                self.engine.begin() as connection,
            ):
                connection.execute(payment.payments.insert().values(**values))
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            row = dict(connection.execute(select(payment.events)).mappings().one())
            connection.execute(payment.events.insert().values(**row))

    def test_adoption_requires_verified_postgres_and_frozen_revision_stays_guarded(
        self,
    ):
        with self.engine.begin() as connection:
            with self.assertRaisesRegex(
                BaselineError, 'payment_adoption_requires_verified_transaction'
            ):
                adopt_payments(connection, expected_fingerprint='0' * 64)
        with patch('alembic.context.is_offline_mode', return_value=True):
            with self.assertRaisesRegex(
                RuntimeError, 'payment_verified_adoption_required'
            ):
                frozen.upgrade()
        with (
            patch('alembic.context.is_offline_mode', return_value=False),
            patch('alembic.context.config', create=True) as config,
        ):
            config.attributes = {}
            with self.assertRaisesRegex(
                RuntimeError, 'payment_verified_adoption_required'
            ):
                frozen.upgrade()
        with self.assertRaisesRegex(RuntimeError, 'destructive_downgrade'):
            frozen.downgrade()

    def test_cli_refuses_enabled_payments_before_connecting(self):
        from studio_platform.schema_baseline import main

        with (
            patch(
                'studio_platform.settings.Settings.from_environment',
                return_value=SimpleNamespace(stripe_mode='test'),
            ),
            patch(
                'studio_platform.schema_baseline.create_engine',
                side_effect=AssertionError('must not connect'),
            ),
            patch('sys.stderr'),
        ):
            self.assertEqual(
                main(['adopt-payments', '--expected-fingerprint', '0' * 64]), 1
            )


@unittest.skipUnless(
    os.environ.get('PLATFORM_TEST_DATABASE_URL'),
    'Explicit isolated local PG URL required',
)
class PaymentAdoptionPostgresTests(unittest.TestCase):
    tearDown = fixtures.CreditAdoptionPostgresTests.tearDown
    database = fixtures.CreditAdoptionPostgresTests.database
    adopt = fixtures.CreditAdoptionPostgresTests.adopt

    def setUp(self):
        fixtures.CreditAdoptionPostgresTests.setUp(self)
        with self.engine.begin() as connection:
            adopt_credits(
                connection,
                expected_fingerprint=compare_schema(connection)['schema_fingerprint'],
            )

    def run_adoption(self, connection, fingerprint=None):
        return adopt_payments(
            connection,
            expected_fingerprint=fingerprint
            or compare_schema(connection)['schema_fingerprint'],
        )

    def test_atomic_chain_and_repeat_mints_no_money_or_config(self):
        with self.engine.begin() as connection:
            before = {
                name: connection.exec_driver_sql(
                    'SELECT count(*) FROM ' + name
                ).scalar_one()
                for name in credit.TABLE_NAMES
            }
            result = self.run_adoption(connection)
            self.assertEqual(result['state'], 'adopted_payments')
            self.assertEqual(result['after']['recorded_revision'], PAYMENT_REVISION)
            self.assertTrue(payment.ready(connection))
            self.assertTrue(credit.ready(connection))
            self.assertTrue(payment.validate_connection(connection))
            for table in payment.TABLE_NAMES:
                self.assertEqual(
                    connection.exec_driver_sql(
                        'SELECT count(*) FROM ' + table
                    ).scalar_one(),
                    0,
                )
            self.assertEqual(
                before,
                {
                    name: connection.exec_driver_sql(
                        'SELECT count(*) FROM ' + name
                    ).scalar_one()
                    for name in credit.TABLE_NAMES
                },
            )
            self.assertEqual(self.run_adoption(connection)['state'], 'already_adopted')

    def test_original_transaction_rollback_removes005_without_touching004(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
            with self.engine.begin() as connection:
                self.run_adoption(connection)
                raise RuntimeError('synthetic rollback')
        with self.engine.connect() as connection:
            self.assertFalse(
                payment.TABLE_NAMES & set(inspect(connection).get_table_names())
            )
            self.assertTrue(credit.ready(connection))
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                CREDIT_REVISION,
            )

    def test_stale_fingerprint_unrecorded_shape_and_unsafe_indexes_are_refused(self):
        with self.engine.begin() as connection:
            original = compare_schema(connection)['schema_fingerprint']
            payment.metadata.create_all(connection)
            with self.assertRaisesRegex(BaselineError, 'schema_snapshot_changed'):
                self.run_adoption(connection, original)
            with self.assertRaisesRegex(BaselineError, 'unrecorded_family'):
                self.run_adoption(connection)
            connection.exec_driver_sql(
                'CREATE INDEX unsafe_payment_index ON platform_payments (status) WHERE paid_at IS NOT NULL'
            )
            with self.assertRaisesRegex(
                payment.PaymentSchemaError, 'shape_requires_review'
            ):
                payment.validate_connection(connection)


if __name__ == '__main__':
    unittest.main()
