"""Credit adoption and portable rows; no services, real payments or GPU calls."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import (
    Column,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    create_engine,
    event,
    inspect,
    select,
)
from sqlalchemy.exc import IntegrityError

from studio_platform import credit_schema as credit
from studio_platform import identity_schema as identity
from studio_platform.auth import accounts as legacy_accounts
from studio_platform.backup import BackupError, _export_business_db
from studio_platform.migrations.versions import (
    v20261019_0004_customer_credits as frozen,
)
from studio_platform.schema_baseline import (
    BaselineError,
    adopt_credits,
    adopt_identity,
    compare_schema,
    frozen_metadata,
)
from studio_platform.schema_registry import platform_metadata
from studio_platform.schema_version import (
    CREDIT_REVISION,
    IDENTITY_REVISION,
    WORKFLOW_REVISIONS,
)
import test_workflow_state as fixtures

USER = '11111111-1111-4111-8111-111111111111'


def marker(engine, revision=CREDIT_REVISION):
    table = Table(
        'alembic_version',
        MetaData(),
        Column('version_num', String(32), nullable=False),
        PrimaryKeyConstraint('version_num', name='alembic_version_pkc'),
    )
    table.create(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(version_num=revision))


def seed(connection):
    connection.execute(
        identity.users.insert().values(
            id=USER,
            tenant='sixnine',
            owner='supervan',
            role='user',
            status='active',
            display_name='Original owner',
            created_at=1.0,
            updated_at=1.0,
        )
    )
    connection.execute(
        credit.accounts.insert().values(
            user_id=USER,
            tenant='sixnine',
            balance=100,
            held=20,
            version=2,
            created_at=2.0,
            updated_at=3.0,
        )
    )
    for number, kind, amount, delta in ((1, 'grant', 100, 0), (2, 'hold', 0, 20)):
        connection.execute(
            credit.entries.insert().values(
                id=f'22222222-2222-4222-8222-{number:012d}',
                tenant='sixnine',
                user_id=USER,
                kind=kind,
                amount=amount,
                held_delta=delta,
                balance_after=100,
                held_after=0 if number == 1 else 20,
                reference_type='job',
                reference_id='original-job',
                reason='synthetic original entry',
                actor=USER,
                idempotency_key=f'original:{number}',
                fingerprint=str(number) * 64,
                created_at=2.0 + number,
                price_revision='initial',
                origin='agent:original',
                receipt_url=None,
            )
        )
    connection.execute(
        credit.prices.insert().values(
            tenant='sixnine',
            revision='initial',
            rates={'fast': 2, 'standard': 4, 'refine': 12},
            configuration={'credit_usd': 0.01, 'welcome_credits': 0},
            published_by=USER,
            published_at=2.0,
        )
    )


class CreditReaderTests(unittest.TestCase):
    def setUp(self):
        platform_metadata()
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        frozen_metadata().create_all(self.engine)

    def family(self):
        identity.metadata.create_all(self.engine)
        credit.metadata.create_all(self.engine)

    def test_reader_is_optional_and_does_not_create_ddl_or_import_alembic(self):
        statements = []
        event.listen(
            self.engine,
            'before_cursor_execute',
            lambda _c, _s, sql, *_: statements.append(sql),
        )
        with (
            self.engine.connect() as connection,
            patch.dict('sys.modules', {'alembic': None, 'dbos': None}),
        ):
            self.assertFalse(credit.ready(connection))
            self.assertFalse(
                credit.validate_sqlite(connection.connection.driver_connection)
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
            set(platform_metadata(include_credits=True).tables)
            - set(platform_metadata().tables),
            credit.TABLE_NAMES,
        )

    def test_complete_future_family_readable_but_only_adopted_family_is_authority(self):
        self.family()
        with self.engine.connect() as connection:
            self.assertTrue(credit.validate_connection(connection))
            self.assertTrue(
                credit.validate_sqlite(connection.connection.driver_connection)
            )
            self.assertTrue(compare_schema(connection)['matches'])
            self.assertFalse(credit.ready(connection))
        marker(self.engine)
        with self.engine.connect() as connection:
            self.assertTrue(credit.ready(connection))
            self.assertTrue(compare_schema(connection)['matches'])
        self.assertIn(CREDIT_REVISION, WORKFLOW_REVISIONS)

    def test_runtime_ready_uses_bounded_queries_without_shape_reflection(self):
        self.family()
        marker(self.engine)
        statements = []
        event.listen(
            self.engine,
            'before_cursor_execute',
            lambda _c, _s, sql, *_: statements.append(sql),
        )
        with self.engine.connect() as connection:
            with patch.object(
                credit,
                'validate_connection',
                side_effect=AssertionError('hot-path reflection'),
            ):
                self.assertTrue(credit.ready(connection))
        self.assertLessEqual(len(statements), 8, statements)
        identity.metadata.drop_all(self.engine)
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(credit.CreditSchemaError, 'partial_family'):
                credit.ready(connection)

    def test_workos_admin_and_identity_readers_accept_inherited_revision(self):
        from studio_platform.auth import Principal
        from studio_platform.identity import account_identity
        from studio_platform.identity_admin import AdminError, IdentityAdmin
        from studio_platform.workos_auth import WorkOSError, WorkOSAuth

        self.family()
        marker(self.engine)
        settings = SimpleNamespace(tenant_id='sixnine')
        admin = IdentityAdmin(None, settings)
        hosted = WorkOSAuth(None, None, settings)
        principal = Principal(owner='supervan', actor_id='original-session')
        with self.engine.begin() as connection:
            seed(connection)
            connection.execute(identity.users.update().values(role='admin'))
            hosted._ready(connection)
            self.assertEqual(admin._actor(connection, principal)['id'], USER)
            self.assertEqual(
                account_identity(connection, 'sixnine', 'supervan')['id'], USER
            )
        identity.metadata.drop_all(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(
                account_identity(connection, 'sixnine', 'supervan'),
                {'status': 'unavailable'},
            )
            with self.assertRaises(WorkOSError):
                hosted._ready(connection)
            with self.assertRaises(AdminError):
                admin._actor(connection, principal)

    def test_complete_backup_restore_preserves_optional_family_and_held_balances(self):
        from studio_platform.backup import backup_local, restore_local, verify_local
        from studio_platform.storage import LocalObjectStore

        for present in (False, True):
            with (
                self.subTest(present=present),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                if present:
                    self.family()
                    marker(self.engine)
                    with self.engine.begin() as connection:
                        seed(connection)
                source = root / 'source.sqlite3'
                with (
                    self.engine.connect() as connection,
                    closing(sqlite3.connect(source)) as target,
                ):
                    connection.connection.driver_connection.backup(target)
                store = LocalObjectStore(root / 'objects')
                backup_local(source, store.root, root / 'backup')
                manifest = verify_local(root / 'backup')
                self.assertEqual(credit.TABLE_NAMES <= set(manifest['tables']), present)
                restore_local(root / 'backup', root / 'restored')
                for path in (
                    source,
                    root / 'backup/database.sqlite3',
                    root / 'restored/platform.sqlite3',
                ):
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(credit.validate_sqlite(db), present)
                        if present:
                            self.assertEqual(
                                db.execute(
                                    'SELECT balance,held,version FROM platform_credit_accounts'
                                ).fetchone(),
                                (100, 20, 2),
                            )
                            self.assertEqual(
                                db.execute(
                                    'SELECT count(*) FROM platform_credit_entries'
                                ).fetchone()[0],
                                2,
                            )

    def test_partial_and_recorded_missing_families_fail_closed(self):
        credit.accounts.create(self.engine)
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(credit.CreditSchemaError, 'partial_family'):
                credit.ready(connection)
            with self.assertRaisesRegex(BaselineError, 'partial_family'):
                compare_schema(connection)
        credit.accounts.drop(self.engine)
        identity.metadata.create_all(self.engine)
        marker(self.engine)
        with self.engine.connect() as connection:
            self.assertFalse(compare_schema(connection)['matches'])
            with self.assertRaisesRegex(credit.CreditSchemaError, 'partial_family'):
                credit.ready(connection)
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    BackupError, 'credit_schema_partial_family'
                ):
                    _export_business_db(
                        connection.connection.driver_connection,
                        Path(directory) / 'refused.sqlite3',
                    )

    def test_shape_and_check_drift_is_refused(self):
        self.family()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                'ALTER TABLE platform_credit_prices ADD COLUMN secret TEXT'
            )
        with self.engine.connect() as connection:
            self.assertFalse(compare_schema(connection)['matches'])
            with self.assertRaisesRegex(
                credit.CreditSchemaError, 'shape_requires_review'
            ):
                credit.validate_connection(connection)
            with self.assertRaisesRegex(
                credit.CreditSchemaError, 'shape_requires_review'
            ):
                credit.validate_sqlite(connection.connection.driver_connection)
        for expression in (
            'balance >= 0 AND held >= 0 AND held <= balance AND version >= 0 OR 1=1',
            "kind IN ('grant', 'purchase', 'hold', 'CAPTURE', 'release', 'refund', 'adjust', 'expire', 'waive')",
            'balance >= 0 AND held >= 0 AND held <= 100 AND version >= 0',
        ):
            with self.assertRaises(credit.CreditSchemaError):
                credit.normalize_check(expression)

    def test_database_enforces_balances_and_idempotency_scope(self):
        self.family()
        with self.engine.begin() as connection:
            seed(connection)
        for updates in ({'held': 101}, {'balance': -1}, {'held': -1}, {'version': -1}):
            with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(credit.accounts.update().values(**updates))
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            row = dict(
                connection.execute(
                    select(credit.entries).where(credit.entries.c.kind == 'hold')
                )
                .mappings()
                .one()
            )
            connection.execute(
                credit.entries.insert().values(
                    **{**row, 'id': '33333333-3333-4333-8333-333333333333'}
                )
            )

    def test_business_backup_preserves_credit_rows_and_original_usd_without_auth(self):
        self.family()
        marker(self.engine)
        with self.engine.begin() as connection:
            seed(connection)
            connection.exec_driver_sql(
                "INSERT INTO platform_budget_accounts (id,tenant_id,owner_id,limit_microusd,reserved_microusd,spent_microusd) VALUES ('original','sixnine','supervan',1000000,123,456)"
            )
        with (
            tempfile.TemporaryDirectory() as directory,
            self.engine.connect() as connection,
        ):
            destination = Path(directory) / 'business.sqlite3'
            counts = _export_business_db(
                connection.connection.driver_connection, destination
            )
            self.assertEqual(
                {name: counts[name] for name in credit.TABLE_NAMES},
                {
                    'platform_credit_accounts': 1,
                    'platform_credit_entries': 2,
                    'platform_credit_prices': 1,
                },
            )
            with closing(sqlite3.connect(destination)) as db:
                self.assertTrue(credit.validate_sqlite(db))
                self.assertEqual(
                    db.execute(
                        'SELECT balance,held,version FROM platform_credit_accounts'
                    ).fetchone(),
                    (100, 20, 2),
                )
                self.assertEqual(
                    db.execute(
                        'SELECT sum(amount),sum(held_delta) FROM platform_credit_entries'
                    ).fetchone(),
                    (100, 20),
                )
                self.assertEqual(
                    db.execute(
                        'SELECT reserved_microusd,spent_microusd FROM platform_budget_accounts'
                    ).fetchone(),
                    (123, 456),
                )
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

    def test_legacy_backup_does_not_invent_credit_or_identity_rows(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.engine.connect() as connection,
        ):
            destination = Path(directory) / 'legacy.sqlite3'
            counts = _export_business_db(
                connection.connection.driver_connection, destination
            )
            self.assertFalse(credit.TABLE_NAMES & set(counts))
            with closing(sqlite3.connect(destination)) as db:
                self.assertFalse(credit.validate_sqlite(db))

    def test_adoption_and_revision_cannot_run_outside_verified_postgres_path(self):
        with self.engine.begin() as connection:
            with self.assertRaisesRegex(
                BaselineError, 'credit_adoption_requires_verified_transaction'
            ):
                adopt_credits(connection, expected_fingerprint='0' * 64)
        with patch('alembic.context.is_offline_mode', return_value=True):
            with self.assertRaisesRegex(
                RuntimeError, 'credit_verified_adoption_required'
            ):
                frozen.upgrade()
        with (
            patch('alembic.context.is_offline_mode', return_value=False),
            patch('alembic.context.config', create=True) as config,
        ):
            config.attributes = {}
            with self.assertRaisesRegex(
                RuntimeError, 'credit_verified_adoption_required'
            ):
                frozen.upgrade()
        with self.assertRaisesRegex(RuntimeError, 'destructive_downgrade'):
            frozen.downgrade()


@unittest.skipUnless(
    os.environ.get('PLATFORM_TEST_DATABASE_URL'),
    'Explicit isolated local PG URL required',
)
class CreditAdoptionPostgresTests(unittest.TestCase):
    tearDown = fixtures.WorkflowStateTests.tearDown
    database = fixtures.WorkflowStateTests.database
    adopt = fixtures.WorkflowStateTests.adopt

    def setUp(self):
        fixtures.WorkflowStateTests.setUp(self)
        self.adopt()
        with self.engine.begin() as connection:
            connection.execute(
                legacy_accounts.insert().values(
                    tenant='sixnine',
                    username='superdan',
                    disabled=0,
                    password_hash='synthetic',
                    updated=1.0,
                )
            )
            adopt_identity(
                connection,
                expected_fingerprint=compare_schema(connection)['schema_fingerprint'],
                tenant_id='sixnine',
                admin_owners=('superdan',),
                clock=lambda: 1000.0,
            )

    def run_adoption(self, connection, fingerprint=None):
        return adopt_credits(
            connection,
            expected_fingerprint=fingerprint
            or compare_schema(connection)['schema_fingerprint'],
        )

    def test_adoption_is_atomic_repeatable_and_mints_no_money(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO platform_budget_accounts (id,tenant_id,owner_id,limit_microusd,reserved_microusd,spent_microusd) VALUES ('original','sixnine','supervan',1000000,123,456)"
            )
            result = self.run_adoption(connection)
            self.assertEqual(result['state'], 'adopted_credits')
            self.assertEqual(result['after']['recorded_revision'], CREDIT_REVISION)
            self.assertTrue(credit.ready(connection))
            for table in credit.TABLE_NAMES:
                self.assertEqual(
                    connection.exec_driver_sql(
                        'SELECT count(*) FROM ' + table
                    ).scalar_one(),
                    0,
                )
            seed(connection)
            repeated = self.run_adoption(connection)
            self.assertEqual(repeated['state'], 'already_adopted')
            self.assertEqual(
                connection.execute(
                    select(credit.accounts.c.balance, credit.accounts.c.held)
                ).one(),
                (100, 20),
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT reserved_microusd,spent_microusd FROM platform_budget_accounts WHERE id='original'"
                ).one(),
                (123, 456),
            )

    def test_caller_rollback_removes_tables_and_version_atomically(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
            with self.engine.begin() as connection:
                self.run_adoption(connection)
                self.assertTrue(credit.ready(connection))
                raise RuntimeError('synthetic rollback')
        with self.engine.connect() as connection:
            self.assertFalse(
                credit.TABLE_NAMES & set(inspect(connection).get_table_names())
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                IDENTITY_REVISION,
            )

    def test_stale_fingerprint_and_unrecorded_family_do_not_adopt(self):
        with self.engine.begin() as connection:
            fingerprint = compare_schema(connection)['schema_fingerprint']
            credit.metadata.create_all(connection)
            with self.assertRaisesRegex(BaselineError, 'schema_snapshot_changed'):
                self.run_adoption(connection, fingerprint)
            with self.assertRaisesRegex(BaselineError, 'credit_unrecorded_family'):
                self.run_adoption(connection)
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                IDENTITY_REVISION,
            )


if __name__ == '__main__':
    unittest.main()
