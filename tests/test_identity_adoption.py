"""Explicit account adoption: real isolated PG chain, no services/providers."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, event, inspect, select, update

from studio_platform.auth import accounts
from studio_platform.identity import lock_account_gate
from studio_platform.identity_schema import metadata, users
from studio_platform.migrations.versions import v20261019_0003_account_roles as frozen
from studio_platform.schema_baseline import (
    BaselineError,
    adopt_identity,
    compare_schema,
)
from studio_platform.schema_version import IDENTITY_REVISION, WORKFLOW_REVISION
import test_workflow_state as fixtures


class IdentityAdoptionBoundaryTests(unittest.TestCase):
    def test_sqlite_adoption_refuses_before_any_ddl_or_query(self):
        engine = create_engine('sqlite://')
        statements = []
        event.listen(
            engine,
            'before_cursor_execute',
            lambda _c, _s, sql, *_: statements.append(sql),
        )
        try:
            with engine.begin() as connection:
                with self.assertRaisesRegex(
                    BaselineError, 'identity_adoption_requires_verified_transaction'
                ):
                    adopt_identity(
                        connection,
                        expected_fingerprint='0' * 64,
                        tenant_id='sixnine',
                        admin_owners=('superdan',),
                    )
            self.assertEqual(statements, [])
        finally:
            engine.dispose()

    def test_frozen_upgrade_cannot_run_in_offline_or_unverified_context(self):
        with patch('alembic.context.is_offline_mode', return_value=True):
            with self.assertRaisesRegex(
                RuntimeError, 'identity_verified_adoption_required'
            ):
                frozen.upgrade()
        with (
            patch('alembic.context.is_offline_mode', return_value=False),
            patch('alembic.context.config', create=True) as config,
        ):
            config.attributes = {}
            with self.assertRaisesRegex(
                RuntimeError, 'identity_verified_adoption_required'
            ):
                frozen.upgrade()


@unittest.skipUnless(
    os.environ.get('PLATFORM_TEST_DATABASE_URL'),
    'Explicit isolated local PG URL required',
)
class IdentityAdoptionPostgresTests(unittest.TestCase):
    tearDown = fixtures.WorkflowStateTests.tearDown
    database = fixtures.WorkflowStateTests.database
    adopt = fixtures.WorkflowStateTests.adopt

    def setUp(self):
        fixtures.WorkflowStateTests.setUp(self)
        self.adopt()
        with self.engine.begin() as connection:
            connection.execute(
                accounts.insert(),
                [
                    dict(
                        tenant='sixnine',
                        username='superdan',
                        disabled=0,
                        password_hash='synthetic-one',
                        updated=1.0,
                    ),
                    dict(
                        tenant='sixnine',
                        username='supervan',
                        disabled=0,
                        password_hash='synthetic-two',
                        updated=2.0,
                    ),
                    dict(
                        tenant='sixnine',
                        username='legacy-disabled',
                        disabled=1,
                        password_hash='synthetic-disabled',
                        updated=3.0,
                    ),
                    dict(
                        tenant='foreign',
                        username='superdan',
                        disabled=0,
                        password_hash='synthetic-foreign',
                        updated=4.0,
                    ),
                ],
            )

    def run_adoption(
        self, connection, *, fingerprint=None, admin_owners=('superdan', 'supervan')
    ):
        return adopt_identity(
            connection,
            expected_fingerprint=fingerprint
            or compare_schema(connection)['schema_fingerprint'],
            tenant_id='sixnine',
            admin_owners=admin_owners,
            clock=lambda: 1000.0,
        )

    def test_bootstrap_preserves_password_rows_owners_and_financial_counters(self):
        with self.engine.begin() as connection:
            legacy = list(
                connection.execute(
                    select(accounts).order_by(accounts.c.tenant, accounts.c.username)
                ).mappings()
            )
            connection.exec_driver_sql(
                "INSERT INTO platform_budget_accounts (id,tenant_id,owner_id,limit_microusd,reserved_microusd,spent_microusd) VALUES ('original','sixnine','supervan',1000000,123,456)"
            )
            result = self.run_adoption(connection)
            self.assertEqual(result['state'], 'adopted_identity')
            self.assertEqual(result['backfilled_users'], 4)
            self.assertEqual(result['after']['recorded_revision'], IDENTITY_REVISION)
            current = {
                (row['tenant'], row['owner']): row
                for row in connection.execute(select(users)).mappings()
            }
            self.assertEqual(
                set(current), {(row['tenant'], row['username']) for row in legacy}
            )
            self.assertEqual(current['sixnine', 'superdan']['role'], 'admin')
            self.assertEqual(current['sixnine', 'supervan']['role'], 'admin')
            self.assertEqual(current['foreign', 'superdan']['role'], 'user')
            self.assertEqual(
                current['sixnine', 'legacy-disabled']['status'], 'suspended'
            )
            for row in current.values():
                self.assertEqual(str(uuid.UUID(row['id'])), row['id'])
                self.assertIsNone(row['email'])
                self.assertIsNone(row['workos_user_id'])
                self.assertEqual(row['display_name'], row['owner'])
            self.assertEqual(
                list(
                    connection.execute(
                        select(accounts).order_by(
                            accounts.c.tenant, accounts.c.username
                        )
                    ).mappings()
                ),
                legacy,
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT reserved_microusd,spent_microusd FROM platform_budget_accounts WHERE id='original'"
                ).one(),
                (123, 456),
            )

    def test_repeat_does_not_override_persisted_roles_status_profile_or_ids(self):
        with self.engine.begin() as connection:
            self.run_adoption(connection)
            connection.execute(
                update(users)
                .where(users.c.tenant == 'sixnine', users.c.owner == 'superdan')
                .values(role='user', status='suspended', display_name='Edited profile')
            )
            original = list(
                connection.execute(select(users).order_by(users.c.id)).mappings()
            )
            result = self.run_adoption(connection)
            self.assertEqual(
                (result['state'], result['backfilled_users']), ('already_adopted', 0)
            )
            self.assertEqual(
                list(connection.execute(select(users).order_by(users.c.id)).mappings()),
                original,
            )

    def test_outer_rollback_removes_ddl_backfill_and_version_update_together(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
            with self.engine.begin() as connection:
                self.run_adoption(connection)
                self.assertTrue(inspect(connection).has_table(users.name))
                raise RuntimeError('synthetic rollback')
        with self.engine.connect() as connection:
            self.assertFalse(inspect(connection).has_table(users.name))
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                WORKFLOW_REVISION,
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT count(*) FROM platform_accounts'
                ).scalar_one(),
                4,
            )

    def test_stale_fingerprint_and_unrecorded_family_never_bootstrap(self):
        with self.engine.begin() as connection:
            fingerprint = compare_schema(connection)['schema_fingerprint']
            metadata.create_all(connection)
            connection.execute(
                users.insert().values(
                    id='11111111-1111-1111-1111-111111111111',
                    tenant='sixnine',
                    owner='superdan',
                    role='user',
                    status='suspended',
                    display_name='Preserved',
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            with self.assertRaisesRegex(BaselineError, 'schema_snapshot_changed'):
                self.run_adoption(connection, fingerprint=fingerprint)
            with self.assertRaisesRegex(
                BaselineError, 'identity_unrecorded_family_requires_review'
            ):
                self.run_adoption(connection)
            self.assertEqual(
                connection.execute(select(users.c.role, users.c.status)).one(),
                ('user', 'suspended'),
            )
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                WORKFLOW_REVISION,
            )

    def test_bootstrap_without_active_configured_admin_cannot_lock_everyone_out(self):
        with self.engine.begin() as connection:
            with self.assertRaisesRegex(
                BaselineError, 'identity_bootstrap_active_admin_required'
            ):
                self.run_adoption(connection, admin_owners=('legacy-disabled',))
            self.assertFalse(inspect(connection).has_table(users.name))
            self.assertEqual(
                connection.exec_driver_sql(
                    'SELECT version_num FROM alembic_version'
                ).scalar_one(),
                WORKFLOW_REVISION,
            )

    def test_adoption_waits_for_existing_reader_account_gate_before_ddl(self):
        started, finished = threading.Event(), threading.Event()

        def adopt():
            started.set()
            try:
                with self.engine.begin() as connection:
                    return self.run_adoption(connection)
            finally:
                finished.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.engine.begin() as connection:
                lock_account_gate(connection)
                future = executor.submit(adopt)
                self.assertTrue(started.wait(3))
                self.assertFalse(finished.wait(0.1))
                self.assertFalse(inspect(connection).has_table(users.name))
            self.assertEqual(future.result(timeout=15)['state'], 'adopted_identity')


if __name__ == '__main__':
    unittest.main()
