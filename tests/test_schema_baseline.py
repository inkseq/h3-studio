"""Frozen baseline: SQLite locally, isolated localhost/sixnine_test in PG CI."""

import json
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import threading
import unittest
import uuid
from unittest.mock import patch

from alembic import command
from sqlalchemy import (
    MetaData,
    String,
    Table,
    Column,
    Float,
    Integer,
    Text,
    create_engine,
    event,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.engine import make_url

from studio_platform.repository import Repository, Scope
from studio_platform.schema_baseline import (
    BaselineError,
    REVISION,
    adopt_baseline,
    alembic_config,
    compare_schema,
    frozen_metadata,
    metadata_signature,
    offline_sql,
)
from studio_platform.schema_registry import metadata_families, platform_metadata
from studio_platform.schema_version import BASELINE_REVISION


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = None
        self.schema = None
        self.url = "sqlite://"
        postgres = os.environ.get("PLATFORM_TEST_DATABASE_URL")
        if postgres:
            parsed = make_url(postgres)
            if (
                parsed.host not in {"localhost", "127.0.0.1"}
                or parsed.database != "sixnine_test"
            ):
                raise RuntimeError("test_database_must_be_explicit_local_sixnine_test")
            self.schema = "baseline_test_" + uuid.uuid4().hex
            self.bootstrap = create_engine(postgres, hide_parameters=True)
            with self.bootstrap.begin() as connection:
                connection.exec_driver_sql('CREATE SCHEMA "' + self.schema + '"')
            self.url = parsed.update_query_dict(
                {"options": "-csearch_path=" + self.schema}
            ).render_as_string(hide_password=False)
        self.engine = create_engine(self.url, hide_parameters=True)

    def tearDown(self):
        self.engine.dispose()
        if self.bootstrap is not None:
            with self.bootstrap.begin() as connection:
                connection.exec_driver_sql('DROP SCHEMA "' + self.schema + '" CASCADE')
            self.bootstrap.dispose()

    def report(self, connection):
        return compare_schema(connection, schema=self.schema)

    def adopt(self, connection, fingerprint):
        return adopt_baseline(
            connection, expected_fingerprint=fingerprint, schema=self.schema
        )

    def test_inventory_frozen_ddl_matches_all_runtime_metadata(self):
        self.assertEqual(REVISION, BASELINE_REVISION)
        self.assertEqual(len(metadata_families()), 11)
        metadata = frozen_metadata()
        self.assertEqual(len(metadata.tables), 59)
        self.assertEqual(
            sum(len(table.columns) for table in metadata.tables.values()), 444
        )
        for dialect in (self.engine.dialect, postgresql_dialect()):
            self.assertEqual(
                metadata_signature(metadata, dialect),
                metadata_signature(platform_metadata(), dialect),
            )

    def test_empty_database_applies_frozen_revision_then_idempotent_adoption(self):
        with self.engine.begin() as connection:
            before = self.report(connection)
            result = self.adopt(connection, before["schema_fingerprint"])
            self.assertEqual(result["state"], "initialized_empty")
            self.assertTrue(result["after"]["matches"])
            self.assertEqual(result["after"]["recorded_revision"], REVISION)
            again = self.adopt(connection, result["after"]["schema_fingerprint"])
            self.assertEqual(again["state"], "already_adopted")

    def test_populated_database_stamp_preserves_every_business_row_and_financial_counter(
        self,
    ):
        frozen_metadata().create_all(self.engine)
        # Use the original repository operations, not invented financial rows.
        repository = Repository(self.url)
        if self.url == "sqlite://":
            repository.engine.dispose()
            repository.engine = self.engine
        scope = Scope("synthetic-baseline", "superdan", "project-one")
        repository.configure_budget(
            "baseline-budget",
            tenant_id=scope.tenant_id,
            owner_id=scope.owner_id,
            limit_microusd=1_000_000,
        )
        plan = repository.create_plan(
            scope,
            {"prompt": "synthetic local test"},
            {"pool": "test"},
            expires_at=4_000_000_000,
            estimated_cost_microusd=12345,
        )
        repository.create_job(
            scope,
            plan["id"],
            "baseline-original-job",
            initial_status="planned",
            budget_account_ids=("baseline-budget",),
        )

        def rows(connection):
            return {
                name: sorted(
                    [
                        json.dumps(dict(row), sort_keys=True)
                        for row in connection.execute(table.select()).mappings()
                    ]
                )
                for name, table in frozen_metadata().tables.items()
            }

        statements = []
        listener = lambda conn, cursor, statement, parameters, context, many: (
            statements.append(statement)
        )
        event.listen(self.engine, "before_cursor_execute", listener)
        try:
            with self.engine.begin() as connection:
                before_rows = rows(connection)
                before = self.report(connection)
                self.assertTrue(before["matches"])
                result = self.adopt(connection, before["schema_fingerprint"])
                self.assertEqual(result["state"], "adopted_existing")
                self.assertEqual(before_rows, rows(connection))
                self.assertTrue(result["after"]["matches"])
                # Only version-table writes are allowed; no historical table
                # DDL, ledger insert, UPDATE, DELETE or settlement occurs.
                writes = [
                    s
                    for s in statements
                    if s.lstrip()
                    .upper()
                    .startswith(
                        ("CREATE ", "INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP ")
                    )
                ]
                self.assertEqual(len(writes), 2)
                self.assertTrue(
                    all("alembic_version" in statement for statement in writes)
                )
        finally:
            event.remove(self.engine, "before_cursor_execute", listener)
            if self.url != "sqlite://":
                repository.close()

    def test_schema_drift_refuses_stamp_without_modifying_data(self):
        metadata = frozen_metadata()
        metadata.tables["platform_accounts"].c.username.type = String(79)
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                insert(metadata.tables["platform_accounts"]).values(
                    tenant="test",
                    username="test",
                    password_hash="synthetic",
                    disabled=0,
                    updated=1,
                )
            )
            before = self.report(connection)
            self.assertFalse(before["matches"])
            with self.assertRaisesRegex(BaselineError, "schema_drift_requires_review"):
                self.adopt(connection, before["schema_fingerprint"])
            self.assertNotIn(
                "alembic_version",
                __import__("sqlalchemy")
                .inspect(connection)
                .get_table_names(schema=self.schema),
            )
            self.assertEqual(
                connection.execute(
                    select(metadata.tables["platform_accounts"].c.username)
                ).scalar_one(),
                "test",
            )

    def test_nullable_default_and_primary_key_drift_are_refused(self):
        metadata = frozen_metadata()
        metadata.remove(metadata.tables["platform_accounts"])
        Table(
            "platform_accounts",
            metadata,
            Column("tenant", String(200), primary_key=True),
            Column("username", String(80), nullable=True),
            Column("password_hash", Text(), nullable=False),
            Column("disabled", Integer(), nullable=False, server_default="1"),
            Column("updated", Float(), nullable=False),
        )
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            report = self.report(connection)
            self.assertIn(
                dict(table="platform_accounts", field="columns"), report["differences"]
            )
            self.assertIn(
                dict(table="platform_accounts", field="primary"), report["differences"]
            )
            with self.assertRaisesRegex(BaselineError, "schema_drift_requires_review"):
                self.adopt(connection, report["schema_fingerprint"])

    def test_unknown_version_is_not_assumed_adopted(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version VALUES ('unknown_revision')"
            )
            with self.assertRaisesRegex(
                BaselineError, "schema_revision_requires_review"
            ):
                self.report(connection)

    def test_postgres_concurrent_adoption_uses_original_lock_and_snapshot(self):
        if self.engine.dialect.name != "postgresql":
            self.skipTest("Actual PostgreSQL advisory lock verification")
        frozen_metadata().create_all(self.engine)
        with self.engine.connect() as connection:
            fingerprint = self.report(connection)["schema_fingerprint"]
        gate = threading.Barrier(2)

        def attempt(_):
            gate.wait(timeout=10)
            try:
                with self.engine.begin() as connection:
                    return self.adopt(connection, fingerprint)["state"]
            except BaselineError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(
                executor.map(attempt, range(2)),
                ["adopted_existing", "schema_snapshot_changed"],
            )

    def test_postgres_post_verification_failure_rolls_back_version_only_write(self):
        if self.engine.dialect.name != "postgresql":
            self.skipTest("Actual PostgreSQL transactional DDL verification")
        frozen_metadata().create_all(self.engine)
        with self.engine.connect() as connection:
            before = self.report(connection)
        bad_after = dict(before, recorded_revision=REVISION, matches=False)
        with self.assertRaisesRegex(
            BaselineError, "schema_adoption_verification_failed"
        ):
            with (
                self.engine.begin() as connection,
                patch(
                    "studio_platform.schema_baseline.compare_schema",
                    side_effect=[before, bad_after],
                ),
            ):
                self.adopt(connection, before["schema_fingerprint"])
        with self.engine.connect() as connection:
            self.assertEqual(self.report(connection), before)

    def test_primary_unique_foreign_check_and_index_drift_are_not_ignored(self):
        metadata = frozen_metadata()
        # One missing constraint of each kind plus an index. No destructive
        # SQL: this deliberately incomplete synthetic schema is created once.
        tables = metadata.tables
        for name, kind in (
            ("platform_documents", "UniqueConstraint"),
            ("platform_jobs", "ForeignKeyConstraint"),
            ("platform_budget_accounts", "CheckConstraint"),
        ):
            table = tables[name]
            constraint = next(c for c in table.constraints if type(c).__name__ == kind)
            table.constraints.remove(constraint)
        index = next(iter(tables["platform_outbox"].indexes))
        tables["platform_outbox"].indexes.remove(index)
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            report = self.report(connection)
            self.assertTrue(
                {
                    ("platform_documents", "unique"),
                    ("platform_jobs", "foreign"),
                    ("platform_budget_accounts", "checks"),
                    ("platform_outbox", "indexes"),
                }.issubset({(d["table"], d["field"]) for d in report["differences"]})
            )
            with self.assertRaisesRegex(BaselineError, "schema_drift_requires_review"):
                self.adopt(connection, report["schema_fingerprint"])

    def test_stale_fingerprint_and_runtime_drift_never_create_version_table(self):
        with self.engine.begin() as connection:
            old = self.report(connection)
            Table("unexpected", MetaData(), Column("id", String(10))).create(connection)
            with self.assertRaisesRegex(BaselineError, "schema_snapshot_changed"):
                self.adopt(connection, old["schema_fingerprint"])
        runtime = platform_metadata()
        runtime.tables["platform_accounts"].c.username.type = String(1)
        with (
            self.engine.begin() as connection,
            patch(
                "studio_platform.schema_registry.platform_metadata",
                return_value=runtime,
            ),
        ):
            with self.assertRaisesRegex(
                BaselineError, "schema_runtime_diff_requires_migration"
            ):
                self.adopt(connection, self.report(connection)["schema_fingerprint"])
            self.assertNotIn(
                "alembic_version",
                __import__("sqlalchemy")
                .inspect(connection)
                .get_table_names(schema=self.schema),
            )

    def test_generic_online_upgrade_and_stamp_refused(self):
        for action in (command.upgrade, command.stamp):
            with self.assertRaisesRegex(
                RuntimeError, "baseline_verified_adoption_required"
            ):
                action(alembic_config(), REVISION)
        with self.engine.connect() as connection:
            self.assertEqual(self.report(connection)["table_count"], 0)

    def test_offline_sql_is_complete_and_non_destructive(self):
        for dialect in ("sqlite", "postgresql"):
            sql = offline_sql(dialect)
            self.assertEqual(
                sql.count("CREATE TABLE "), 60
            )  # 59 business/auth + original Alembic version table.
            self.assertIn("INSERT INTO alembic_version", sql)
            self.assertNotRegex(sql, r"(?i)\b(?:DROP|DELETE|UPDATE|ALTER)\b")
        from studio_platform.migrations.versions import (
            v20261019_0001_platform_baseline as revision,
        )

        with self.assertRaisesRegex(
            RuntimeError, "baseline_destructive_downgrade_not_supported"
        ):
            revision.downgrade()

    def test_version_and_views_require_review(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE VIEW unexpected_view AS SELECT 1 AS x")
            with self.assertRaisesRegex(
                BaselineError, "schema_unexpected_database_objects"
            ):
                self.report(connection)

    def test_portable_backup_accepts_known_baseline_but_never_exports_version_or_auth(
        self,
    ):
        from studio_platform.backup import (
            _export_business_db,
            _export_postgres_business_db,
            BackupError,
        )
        import sqlite3

        frozen_metadata().create_all(self.engine)
        with self.engine.begin() as connection:
            self.adopt(connection, self.report(connection)["schema_fingerprint"])
        with tempfile.TemporaryDirectory(prefix="baseline-backup-") as directory:
            destination = Path(directory) / "business.sqlite3"
            if self.engine.dialect.name == "postgresql":
                counts = _export_postgres_business_db(
                    self.engine, destination, schema=self.schema
                )
            else:
                with self.engine.connect() as connection:
                    counts = _export_business_db(
                        connection.connection.driver_connection, destination
                    )
            with closing(sqlite3.connect(destination)) as db:
                names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertNotIn("alembic_version", names)
            self.assertNotIn("platform_accounts", names)
            self.assertIn("platform_jobs", names)
            self.assertNotIn("alembic_version", counts)
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE alembic_version SET version_num='unknown_revision'"
                )
            refused = Path(directory) / "refused.sqlite3"
            with self.assertRaisesRegex(
                BackupError, "backup_schema_revision_requires_review"
            ):
                if self.engine.dialect.name == "postgresql":
                    _export_postgres_business_db(
                        self.engine, refused, schema=self.schema
                    )
                else:
                    with self.engine.connect() as connection:
                        _export_business_db(
                            connection.connection.driver_connection, refused
                        )


if __name__ == "__main__":
    unittest.main()
