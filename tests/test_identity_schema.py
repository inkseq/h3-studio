"""Read-only identity bridge: exact optional schema and portable business data."""

from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import (
    Column,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    create_engine,
    event,
    inspect,
)

from studio_platform import identity_schema as identity
from studio_platform.backup import BackupError, _export_business_db
from studio_platform.schema_baseline import (
    BaselineError,
    compare_schema,
    frozen_metadata,
    metadata_signature,
)
from studio_platform.schema_registry import platform_metadata
from studio_platform.schema_version import (
    IDENTITY_REVISION,
    WORKFLOW_REVISIONS,
    read_schema_version,
)
import test_workflow_state as workflow_fixture


class IdentityReaderTests(unittest.TestCase):
    def setUp(self):
        # Register the existing shared metadata before dependency-isolation
        # patches; unloading a lazy imported table module would leave its SQL
        # tables registered and make a later app import attempt to define twice.
        platform_metadata()
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        frozen_metadata().create_all(self.engine)

    def marker(self, revision=IDENTITY_REVISION):
        marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), nullable=False),
            PrimaryKeyConstraint("version_num", name="alembic_version_pkc"),
        )
        marker.create(self.engine)
        with self.engine.begin() as connection:
            connection.execute(marker.insert().values(version_num=revision))

    def test_inventory_registers_future_business_tables_without_startup_creation(self):
        writes = []
        event.listen(
            self.engine,
            "before_cursor_execute",
            lambda _c, _s, sql, *_: writes.append(sql),
        )
        with (
            self.engine.connect() as connection,
            patch.dict("sys.modules", {"alembic": None, "dbos": None}),
        ):
            self.assertFalse(identity.validate_connection(connection))
            self.assertFalse(
                identity.validate_sqlite(connection.connection.driver_connection)
            )
            self.assertTrue(compare_schema(connection)["matches"])
        self.assertFalse(
            any(
                sql.lstrip()
                .upper()
                .startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP"))
                for sql in writes
            )
        )
        self.assertEqual(
            set(platform_metadata(include_identity=True).tables)
            - set(platform_metadata().tables),
            identity.TABLE_NAMES,
        )
        self.assertNotIn("platform_users", inspect(self.engine).get_table_names())

    def test_exact_complete_family_is_readable_before_and_after_revision_marker(self):
        identity.metadata.create_all(self.engine)
        with self.engine.connect() as connection:
            self.assertTrue(identity.validate_connection(connection))
            self.assertTrue(
                identity.validate_sqlite(connection.connection.driver_connection)
            )
            self.assertTrue(compare_schema(connection)["matches"])
        self.marker()
        with self.engine.connect() as connection:
            self.assertEqual(read_schema_version(connection), IDENTITY_REVISION)
            self.assertTrue(compare_schema(connection)["matches"])
        self.assertIn(IDENTITY_REVISION, WORKFLOW_REVISIONS)

    def test_partial_family_or_adopted_missing_family_is_refused(self):
        identity.users.create(self.engine)
        with self.engine.connect() as connection:
            with self.assertRaisesRegex(identity.IdentitySchemaError, "partial_family"):
                identity.validate_connection(connection)
            with self.assertRaisesRegex(BaselineError, "partial_family"):
                compare_schema(connection)
        identity.users.drop(self.engine)
        self.marker()
        with self.engine.connect() as connection:
            report = compare_schema(connection)
            self.assertFalse(report["matches"])
            self.assertEqual(
                {row["table"] for row in report["differences"]}, identity.TABLE_NAMES
            )
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(BackupError, "partial_family"):
                    _export_business_db(
                        connection.connection.driver_connection,
                        Path(directory) / "refused.sqlite3",
                    )

    def test_unknown_column_index_and_check_are_not_permitted_optional_shapes(self):
        identity.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE platform_users ADD COLUMN secret TEXT"
            )
        with self.engine.connect() as connection:
            self.assertFalse(compare_schema(connection)["matches"])
            with self.assertRaisesRegex(
                identity.IdentitySchemaError, "shape_requires_review"
            ):
                identity.validate_connection(connection)
            with self.assertRaisesRegex(
                identity.IdentitySchemaError, "shape_requires_review"
            ):
                identity.validate_sqlite(connection.connection.driver_connection)
        identity.metadata.drop_all(self.engine)
        identity.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE INDEX unexpected_identity_index ON platform_users (email)"
            )
        with self.engine.connect() as connection:
            self.assertFalse(compare_schema(connection)["matches"])
            with self.assertRaises(identity.IdentitySchemaError):
                identity.validate_connection(connection)
            with self.assertRaises(identity.IdentitySchemaError):
                identity.validate_sqlite(connection.connection.driver_connection)
        for value in (
            "role IN ('user', 'admin', 'root')",
            "role IN ('USER', 'ADMIN')",
            "role IN ('user()', 'admin')",
            "role IN ('user::text', 'admin')",
            "role IN ('user ', 'admin')",
            "role='admin' OR 1=1",
            "status IN ('active','suspended'); DELETE FROM platform_users",
        ):
            with self.assertRaises(identity.IdentitySchemaError):
                identity.normalize_check(value)

    def test_business_backup_preserves_user_owner_roles_terms_and_audit_excludes_passwords(
        self,
    ):
        identity.metadata.create_all(self.engine)
        self.marker()
        user = dict(
            id="11111111-1111-1111-1111-111111111111",
            tenant="test",
            owner="supervan",
            role="admin",
            status="active",
            display_name="Original owner",
            email="test@example.invalid",
            email_verified=1,
            workos_user_id=None,
            created_at=1.0,
            updated_at=2.0,
            terms_version="test-terms",
            terms_accepted_at=2.0,
        )
        audit = dict(
            id="22222222-2222-2222-2222-222222222222",
            tenant="test",
            actor_user_id=user["id"],
            target_user_id=user["id"],
            action="user_role_changed",
            reason="Synthetic test",
            before={"role": "user"},
            after={"role": "admin"},
            created_at=2.0,
        )
        with self.engine.begin() as connection:
            connection.execute(identity.users.insert().values(**user))
            connection.execute(identity.admin_audit.insert().values(**audit))
            connection.exec_driver_sql(
                "INSERT INTO platform_accounts VALUES ('test','supervan','never-export-password',0,1.0)"
            )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "business.sqlite3"
            with (
                self.engine.connect() as connection,
                patch.dict(
                    "sys.modules",
                    {
                        "alembic": None,
                        "dbos": None,
                        "studio_platform.schema_baseline": None,
                    },
                ),
            ):
                counts = _export_business_db(
                    connection.connection.driver_connection, destination
                )
            self.assertEqual(counts["platform_users"], 1)
            self.assertEqual(counts["platform_admin_audit"], 1)
            with closing(sqlite3.connect(destination)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT owner,role,terms_version FROM platform_users"
                    ).fetchone(),
                    ("supervan", "admin", "test-terms"),
                )
                self.assertEqual(
                    db.execute(
                        "SELECT action,reason FROM platform_admin_audit"
                    ).fetchone(),
                    ("user_role_changed", "Synthetic test"),
                )
                self.assertIsNone(
                    db.execute(
                        "SELECT name FROM sqlite_master WHERE name='platform_accounts'"
                    ).fetchone()
                )
                self.assertTrue(identity.validate_sqlite(db))
            self.assertNotIn(b"never-export-password", destination.read_bytes())

    def test_frozen_definition_has_no_adoption_entry_point_and_metadata_is_exact(self):
        self.assertEqual(
            metadata_signature(identity.frozen_metadata(), self.engine.dialect),
            metadata_signature(identity.metadata, self.engine.dialect),
        )
        with self.assertRaisesRegex(RuntimeError, "reader_bridge_cannot_adopt"):
            identity.frozen.upgrade()
        for value in (
            "((role)::text = ANY ((ARRAY['user'::character varying, 'admin'::character varying])::text[]))",
            "status IN ('active', 'suspended')",
        ):
            self.assertTrue(identity.normalize_check(value))

    def test_complete_backup_and_restore_preserve_optional_identity_absence_or_rows(
        self,
    ):
        from studio_platform.backup import backup_local, restore_local, verify_local
        from studio_platform.storage import LocalObjectStore

        for present in (False, True):
            with (
                self.subTest(present=present),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                if present:
                    identity.metadata.create_all(self.engine)
                    with self.engine.begin() as connection:
                        connection.execute(
                            identity.users.insert().values(
                                id="11111111-1111-1111-1111-111111111111",
                                tenant="test",
                                owner="supervan",
                                role="admin",
                                display_name="Original owner",
                                created_at=1.0,
                                updated_at=1.0,
                            )
                        )
                source = root / "source.sqlite3"
                with (
                    self.engine.connect() as connection,
                    closing(sqlite3.connect(source)) as target,
                ):
                    connection.connection.driver_connection.backup(target)
                store = LocalObjectStore(root / "objects")
                backup_local(source, store.root, root / "backup")
                manifest = verify_local(root / "backup")
                self.assertEqual(
                    identity.TABLE_NAMES <= set(manifest["tables"]), present
                )
                restore_local(root / "backup", root / "restored")
                for path in (
                    source,
                    root / "backup/database.sqlite3",
                    root / "restored/platform.sqlite3",
                ):
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(identity.validate_sqlite(db), present)
                        if present:
                            self.assertEqual(
                                db.execute(
                                    "SELECT owner,role FROM platform_users"
                                ).fetchone(),
                                ("supervan", "admin"),
                            )

    def test_native_dump_selects_optional_tables_and_inherits_workflows_in_one_snapshot(
        self,
    ):
        from studio_platform.backup import dump_postgres

        for present, revision in ((False, None), (True, IDENTITY_REVISION)):
            with (
                self.subTest(present=present),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = Path(directory) / "native"
                output.mkdir()
                connection, engine = MagicMock(), MagicMock()
                connection.execution_options.return_value = connection
                connection.__enter__.return_value = connection
                connection.exec_driver_sql.return_value.scalar_one.return_value = (
                    "000001-1"
                )
                engine.connect.return_value = connection
                commands = []

                def run(command, **_kwargs):
                    commands.append(command)
                    (output / "database.dump").write_bytes(b"synthetic-native-archive")

                environment = dict(
                    PGUSER="test",
                    PGPASSWORD="synthetic",
                    PGHOST="127.0.0.1",
                    PGPORT="5432",
                    PGDATABASE="sixnine_test",
                    PGSSLMODE="disable",
                )
                with (
                    patch(
                        "studio_platform.backup._postgres_environment",
                        return_value=environment,
                    ),
                    patch(
                        "studio_platform.backup._private_new_directory",
                        return_value=output,
                    ),
                    patch("studio_platform.backup.create_engine", return_value=engine),
                    patch(
                        "studio_platform.schema_version.read_schema_version",
                        return_value=revision,
                    ),
                    patch(
                        "studio_platform.identity_schema.validate_connection",
                        return_value=present,
                    ),
                    patch(
                        "studio_platform.workflow_schema.schema_exists",
                        return_value=present,
                    ),
                    patch(
                        "studio_platform.workflow_backup.export",
                        return_value={"synthetic": True},
                    ) as export,
                    patch("studio_platform.backup.subprocess.run", side_effect=run),
                ):
                    dump_postgres(
                        Path(directory) / "unused-url",
                        output,
                        include_workflows=present,
                    )
                self.assertEqual(len(commands), 1)
                self.assertIn("--snapshot=000001-1", commands[0])
                for name in identity.TABLE_NAMES:
                    self.assertEqual("--table=public." + name in commands[0], present)
                if present:
                    export.assert_called_once_with(connection, output)
                else:
                    export.assert_not_called()


@unittest.skipUnless(
    os.environ.get("PLATFORM_TEST_DATABASE_URL"),
    "Explicit isolated local PG URL required",
)
class IdentityPostgresReaderTests(unittest.TestCase):
    setUp = workflow_fixture.WorkflowStateTests.setUp
    tearDown = workflow_fixture.WorkflowStateTests.tearDown
    database = workflow_fixture.WorkflowStateTests.database
    adopt = workflow_fixture.WorkflowStateTests.adopt

    def test_new_identity_revision_preserves_exact_dbos_catalog_and_business_backup(
        self,
    ):
        from studio_platform.backup import backup_postgres_engine, verify_local
        from studio_platform.storage import LocalObjectStore
        from studio_platform.workflow_backup import read

        self.adopt()
        # Synthetic fixture only: the reader bridge exposes no adoption writer.
        identity.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num='20261019_0003'"
            )
            connection.execute(
                identity.users.insert().values(
                    id="11111111-1111-1111-1111-111111111111",
                    tenant="fixture",
                    owner="supervan",
                    display_name="Preserved owner",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            self.assertTrue(identity.validate_connection(connection))
            report = compare_schema(connection)
            self.assertTrue(report["matches"])
            self.assertIn("workflow_catalog_sha256", report)
            self.assertEqual(report["recorded_revision"], IDENTITY_REVISION)
        store = LocalObjectStore(self.root / "objects")
        with patch.dict(
            "sys.modules",
            {"dbos": None, "alembic": None, "studio_platform.schema_baseline": None},
        ):
            backup_postgres_engine(self.engine, store.root, self.root / "backup")
        manifest = verify_local(self.root / "backup")
        self.assertEqual(manifest["tables"]["platform_users"], 1)
        self.assertEqual(manifest["tables"]["platform_admin_audit"], 0)
        self.assertIn("workflow_state", manifest)
        self.assertIn(
            "workflow_status",
            read(self.root / "backup", manifest["workflow_state"])["tables"],
        )


if __name__ == "__main__":
    unittest.main()
