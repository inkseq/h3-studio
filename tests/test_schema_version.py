"""Reader-before-writer backup compatibility; no Alembic dependency or adoption."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from sqlalchemy import Column, MetaData, PrimaryKeyConstraint, String, Table, inspect

from studio_platform.auth import Auth
from studio_platform.backup import (
    BackupError,
    _export_business_db,
    _export_postgres_business_db,
)
from studio_platform.schema_version import (
    BASELINE_REVISION,
    SchemaVersionError,
    read_schema_version,
    read_sqlite_schema_version,
)
from test_platform_repository import LedgerCase


class SchemaVersionTests(LedgerCase):
    def marker(self, *, revision=BASELINE_REVISION, extra=False):
        table = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), nullable=False),
            *([Column("extra", String(1))] if extra else []),
            PrimaryKeyConstraint("version_num", name="alembic_version_pkc"),
        )
        table.create(self.repo.engine)
        with self.repo.engine.begin() as connection:
            connection.execute(table.insert().values(version_num=revision))

    def export(self, destination):
        if self.bootstrap is not None:
            return _export_postgres_business_db(
                self.repo.engine, destination, schema=self.schema
            )
        with self.repo.engine.connect() as connection:
            return _export_business_db(
                connection.connection.driver_connection, destination
            )

    def test_absent_marker_remains_absent_and_reader_performs_no_initialization(self):
        with self.repo.engine.connect() as connection:
            before = inspect(connection).get_table_names()
            self.assertIsNone(read_schema_version(connection))
            self.assertEqual(before, inspect(connection).get_table_names())

    def test_known_marker_remains_but_business_export_excludes_marker_and_auth(self):
        self.marker()
        Auth(self.repo.engine, tenant=self.scope.tenant_id)
        with self.repo.engine.connect() as connection:
            self.assertEqual(read_schema_version(connection), BASELINE_REVISION)
            if self.bootstrap is None:
                self.assertEqual(
                    read_sqlite_schema_version(connection.connection.driver_connection),
                    BASELINE_REVISION,
                )
        with tempfile.TemporaryDirectory(prefix="schema-reader-") as directory:
            destination = Path(directory) / "business.sqlite3"
            # Simulate the rollback image: neither Alembic nor the future
            # baseline writer is importable, yet its read-only backup works.
            with patch.dict(
                "sys.modules",
                {"alembic": None, "studio_platform.schema_baseline": None},
            ):
                counts = self.export(destination)
            with closing(sqlite3.connect(destination)) as db:
                names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertIn("platform_jobs", names)
            self.assertNotIn("alembic_version", names)
            self.assertNotIn("platform_accounts", names)
            self.assertNotIn("alembic_version", counts)
        with self.repo.engine.connect() as connection:
            self.assertEqual(read_schema_version(connection), BASELINE_REVISION)

    def test_unknown_revision_refuses_export_without_rewriting_or_dropping_marker(self):
        self.marker(revision="unknown")
        with self.repo.engine.connect() as connection:
            with self.assertRaisesRegex(
                SchemaVersionError, "schema_revision_requires_review"
            ):
                read_schema_version(connection)
        with tempfile.TemporaryDirectory(prefix="schema-reader-refused-") as directory:
            with self.assertRaisesRegex(
                BackupError, "backup_schema_revision_requires_review"
            ):
                self.export(Path(directory) / "business.sqlite3")
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one(),
                "unknown",
            )

    def test_extra_marker_column_is_not_a_looser_whitelist(self):
        self.marker(extra=True)
        with self.repo.engine.connect() as connection:
            with self.assertRaisesRegex(
                SchemaVersionError, "schema_version_table_requires_review"
            ):
                read_schema_version(connection)
        with tempfile.TemporaryDirectory(prefix="schema-reader-shape-") as directory:
            with self.assertRaisesRegex(
                BackupError, "backup_schema_revision_requires_review"
            ):
                self.export(Path(directory) / "business.sqlite3")
