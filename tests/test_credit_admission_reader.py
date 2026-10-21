"""Pure 006/007 readers and exact business rows; no live adoption or policy."""

from contextlib import closing, redirect_stderr
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import (
    Column,
    DefaultClause,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    text,
)
from studio_platform import credit_admission, credits
from studio_platform.credit_schema import metadata as credit_metadata, entries
from studio_platform.identity_schema import metadata as identity_metadata, users
from studio_platform.repository import (
    BudgetExceeded,
    budget_accounts,
    budget_reservations,
)
from studio_platform.schema_version import CREDIT_ADMISSION_REVISION
from test_platform_repository import LedgerCase


def original_rows(connection, revision):
    fields = "id,account_id,reference_type,reference_id,amount_microusd,state,actual_cost_microusd,created_at,settled_at"
    if revision == CREDIT_ADMISSION_REVISION:
        fields += ",budget_role"
    return list(
        connection.execute(
            text("SELECT " + fields + " FROM platform_budget_reservations ORDER BY id")
        )
    )


def seed_original_rows(connection, revision):
    for index, role in enumerate(("accounting", "enforced")):
        connection.execute(
            insert(budget_accounts).values(
                id="original-account-" + str(index),
                tenant_id="sixnine",
                owner_id="superdan" if index == 0 else None,
                limit_microusd=500_000,
                reserved_microusd=123,
                spent_microusd=456,
            )
        )
        connection.execute(
            insert(budget_reservations).values(
                id="original-reservation-" + str(index),
                account_id="original-account-" + str(index),
                reference_type="job",
                reference_id="original-job-" + str(index),
                amount_microusd=100_000 + index,
                state="settled" if index else "reserved",
                actual_cost_microusd=987 if index else None,
                created_at=1000.0,
                settled_at=1010.0 if index else None,
            )
        )
        if revision == CREDIT_ADMISSION_REVISION:
            connection.execute(
                text(
                    "UPDATE platform_budget_reservations SET budget_role=:role WHERE id=:id"
                ),
                {"role": role, "id": "original-reservation-" + str(index)},
            )


def assert_published_rows(case, path, expected):
    from studio_platform.backup import _check_db, _quarantine_restored_database

    with closing(sqlite3.connect(path)) as db:
        _check_db(db, published=True)
        fields = "id,account_id,reference_type,reference_id,amount_microusd,state,actual_cost_microusd,created_at,settled_at"
        if any(
            row[1] == "budget_role"
            for row in db.execute('PRAGMA table_info("platform_budget_reservations")')
        ):
            fields += ",budget_role"
        before = db.execute(
            "SELECT " + fields + " FROM platform_budget_reservations ORDER BY id"
        ).fetchall()
        accounts = db.execute(
            "SELECT * FROM platform_budget_accounts ORDER BY id"
        ).fetchall()
        case.assertEqual(before, [tuple(row) for row in expected])
    _quarantine_restored_database(path)
    with closing(sqlite3.connect(path)) as db:
        case.assertEqual(
            db.execute(
                "SELECT " + fields + " FROM platform_budget_reservations ORDER BY id"
            ).fetchall(),
            before,
        )
        case.assertEqual(
            db.execute("SELECT * FROM platform_budget_accounts ORDER BY id").fetchall(),
            accounts,
        )


class CreditAdmissionReaderTests(unittest.TestCase):
    def family(self, engine, revision, *, role_check=None, role_shape=None):
        from studio_platform.schema_baseline import frozen_metadata
        from studio_platform.identity_schema import metadata as identity
        from studio_platform.payment_schema import frozen_metadata as payment_metadata
        from studio_platform.migrations.versions.v20261022_0007_credit_admission import (
            extend,
        )

        baseline = frozen_metadata()
        if revision == CREDIT_ADMISSION_REVISION:
            extend(baseline)
            for name, value in (role_shape or {}).items():
                setattr(
                    baseline.tables["platform_budget_reservations"].c.budget_role,
                    name,
                    value,
                )
            if role_check is not None:
                for constraint in baseline.tables[
                    "platform_budget_reservations"
                ].constraints:
                    if constraint.name == "ck_budget_reservation_role":
                        constraint.sqltext = text(role_check)
        baseline.create_all(engine)
        identity.create_all(engine)
        credit_metadata.create_all(engine)
        payment_metadata(revision="20261021_0006").create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
            connection.execute(
                text("INSERT INTO alembic_version VALUES (:revision)"),
                {"revision": revision},
            )

    def test_frozen006_and007_exact_schema_and_business_backup_preserve_role(self):
        from studio_platform.schema_baseline import compare_schema
        from studio_platform.backup import _export_business_db, _check_db

        for revision in ("20261021_0006", CREDIT_ADMISSION_REVISION):
            with self.subTest(revision=revision), tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / "source.sqlite3"
                engine = create_engine("sqlite:///" + source.as_posix())
                try:
                    self.family(engine, revision)
                    with engine.begin() as connection:
                        seed_original_rows(connection, revision)
                        expected = original_rows(connection, revision)
                    with engine.connect() as connection:
                        self.assertTrue(compare_schema(connection)["matches"])
                        self.assertEqual(
                            credit_admission.ready(connection),
                            revision == CREDIT_ADMISSION_REVISION,
                        )
                    target = Path(temp) / "business.sqlite3"
                    with closing(sqlite3.connect(source)) as db:
                        _export_business_db(db, target)
                    with closing(sqlite3.connect(target)) as db:
                        _check_db(db, published=True)
                        columns = {
                            row[1]
                            for row in db.execute(
                                'PRAGMA table_info("platform_budget_reservations")'
                            )
                        }
                        self.assertEqual(
                            "budget_role" in columns,
                            revision == CREDIT_ADMISSION_REVISION,
                        )
                    assert_published_rows(self, target, expected)
                    self.assertNotIn("budget_role", budget_reservations.c)
                finally:
                    engine.dispose()

    def test_reader_release_has_no_adopter_and_upgrade_always_refuses(self):
        from studio_platform import schema_baseline
        from studio_platform.migrations.versions import (
            v20261022_0007_credit_admission as preview,
        )

        self.assertFalse(hasattr(schema_baseline, "adopt_credit_admission"))
        with self.assertRaisesRegex(RuntimeError, "reader_only_release"):
            preview.upgrade()
        with self.assertRaisesRegex(RuntimeError, "destructive_downgrade"):
            preview.downgrade()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            schema_baseline.main(
                ["adopt-credit-admission", "--expected-fingerprint", "a" * 64]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_inconsistent_marker_type_default_and_nullability_are_refused(self):
        from studio_platform.backup import BackupError, _check_db
        from studio_platform.schema_version import SchemaVersionError

        cases = (
            (CREDIT_ADMISSION_REVISION, None, {"type": String(21)}),
            (CREDIT_ADMISSION_REVISION, None, {"nullable": True}),
            (
                CREDIT_ADMISSION_REVISION,
                None,
                {"server_default": DefaultClause("accounting")},
            ),
            (CREDIT_ADMISSION_REVISION, "20261021_0006", None),
            ("20261021_0006", CREDIT_ADMISSION_REVISION, None),
            ("20261021_0006", "20261022_9999", None),
        )
        for original, changed, shape in cases:
            with self.subTest(original=original, changed=changed, shape=shape):
                engine = create_engine("sqlite://")
                try:
                    self.family(engine, original, role_shape=shape)
                    if changed:
                        with engine.begin() as connection:
                            connection.execute(
                                text("UPDATE alembic_version SET version_num=:value"),
                                {"value": changed},
                            )
                    with (
                        engine.connect() as connection,
                        self.assertRaises((credits.CreditError, SchemaVersionError)),
                    ):
                        credit_admission.ready(connection)
                    raw = engine.raw_connection()
                    try:
                        with self.assertRaises(BackupError):
                            _check_db(raw.driver_connection)
                    finally:
                        raw.close()
                finally:
                    engine.dispose()

    def test_role_check_accepts_only_finite_sqlite_and_postgres_grammars(self):
        from studio_platform.migrations.versions.v20261022_0007_credit_admission import (
            normalize_check,
        )

        positive = (
            "budget_role IN ('accounting', 'enforced')",
            "(budget_role IN ('accounting','enforced'))",
            "((budget_role)::text = ANY ((ARRAY['accounting'::character varying, 'enforced'::character varying])::text[]))",
            "(budget_role = ANY (ARRAY['accounting'::text, 'enforced'::text]))",
            "budget_role::text = ANY (ARRAY['accounting'::character varying, 'enforced'::character varying]::text[])",
        )
        for sql in positive:
            with self.subTest(sql=sql):
                self.assertIsNotNone(normalize_check(sql))
        for literal in (
            "acc::textounting",
            "acc::character varyingounting",
            "acc(ounting",
            "acc ounting",
            "ACCOUNTING",
            "account''ing",
        ):
            for sql in (
                f"budget_role IN ('{literal}', 'enforced')",
                f"((budget_role)::text = ANY ((ARRAY['{literal}'::character varying, 'enforced'::character varying])::text[]))",
                f"budget_role::text = ANY (ARRAY['{literal}'::character varying, 'enforced'::character varying]::text[])",
            ):
                with self.subTest(sql=sql):
                    self.assertIsNone(normalize_check(sql))
        for sql in (
            "budget_role IN (('accounting','enforced'))",
            "budget_role IN ('accounting','enforced') OR 1=1",
            "budget_role IN ('accounting','enforced'))",
            "budget_role = ANY (ARRAY['accounting'::text::text,'enforced'::text])",
            "other_role::text = ANY (ARRAY['accounting'::character varying, 'enforced'::character varying]::text[])",
            "budget_role::text::text = ANY (ARRAY['accounting'::character varying, 'enforced'::character varying]::text[])",
        ):
            with self.subTest(sql=sql):
                self.assertIsNone(normalize_check(sql))

    def test_sqlite_constraint_text_in_literals_or_comments_is_not_authority(self):
        from studio_platform.migrations.versions.v20261022_0007_credit_admission import (
            sqlite_check,
        )

        quoted = "CREATE TABLE t (note TEXT DEFAULT 'CONSTRAINT ck_budget_reservation_role CHECK (budget_role IN (''accounting'', ''enforced''))', budget_role VARCHAR(20))"
        comment = "CREATE TABLE t (budget_role VARCHAR(20)) /* CONSTRAINT ck_budget_reservation_role CHECK (budget_role IN ('accounting', 'enforced')) */"
        line_comment = "CREATE TABLE t (budget_role VARCHAR(20)) -- CONSTRAINT ck_budget_reservation_role CHECK (budget_role IN ('accounting', 'enforced'))"
        for sql in (quoted, comment, line_comment):
            with self.subTest(sql=sql):
                self.assertFalse(sqlite_check(sql))

    def test_sqlite_backup_literal_drift_refused_with_and_without_marker(self):
        from studio_platform.backup import BackupError, _check_db

        for literal in ("acc ounting", "ACCOUNTING", "acc::textounting"):
            engine = create_engine("sqlite://")
            try:
                self.family(
                    engine,
                    CREDIT_ADMISSION_REVISION,
                    role_check=f"budget_role IN ('{literal}', 'enforced')",
                )
                with (
                    engine.connect() as connection,
                    self.assertRaisesRegex(
                        credits.CreditError, "schema_requires_review"
                    ),
                ):
                    credit_admission.ready(connection)
                raw = engine.raw_connection()
                try:
                    with self.assertRaisesRegex(
                        BackupError, "credit_admission_schema_requires_review"
                    ):
                        _check_db(raw.driver_connection)
                    raw.driver_connection.execute("DROP TABLE alembic_version")
                    with self.assertRaisesRegex(
                        BackupError, "credit_admission_schema_requires_review"
                    ):
                        _check_db(raw.driver_connection, published=True)
                finally:
                    raw.close()
            finally:
                engine.dispose()


class LegacyUsdAdmissionTests(LedgerCase):
    def test006_off_keeps_owner_and_shared_caps_and_no_credit_hold(self):
        marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        with self.repo.transaction() as connection:
            identity_metadata.create_all(connection)
            credit_metadata.create_all(connection)
            marker.create(connection)
            connection.execute(insert(marker).values(version_num="20261021_0006"))
            connection.execute(
                insert(users).values(
                    id="00000000-0000-4000-8000-000000000409",
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    role="user",
                    status="active",
                    display_name="Synthetic reader fixture",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        self.repo.configure_budget(
            "owner-budget",
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=0,
        )
        with self.assertRaises(BudgetExceeded):
            self.job()
        self.repo.configure_budget(
            "owner-budget",
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=1_000_000,
        )
        job = self.job()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )
        self.repo.configure_budget(
            "shared-budget", tenant_id=self.scope.tenant_id, limit_microusd=0
        )
        with self.assertRaises(BudgetExceeded):
            self.repo.create_job(
                self.scope,
                self.plan()["id"],
                "shared-cap",
                budget_account_ids=("owner-budget", "shared-budget"),
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(entries.c.id))), [])
            self.assertFalse(credit_admission.ready(connection))
            self.assertEqual(
                len(list(connection.execute(select(budget_reservations.c.id)))), 1
            )


@unittest.skipUnless(
    os.environ.get("PLATFORM_TEST_DATABASE_URL"),
    "explicit isolated local PostgreSQL only",
)
class CreditAdmissionReaderPostgresTests(unittest.TestCase):
    import test_payment_schema as payment_fixtures

    tearDown = payment_fixtures.PaymentAdoptionPostgresTests.tearDown
    database = payment_fixtures.PaymentAdoptionPostgresTests.database
    adopt = payment_fixtures.PaymentAdoptionPostgresTests.adopt

    def setUp(self):
        self.payment_fixtures.PaymentAdoptionPostgresTests.setUp(self)
        with self.engine.begin() as connection:
            self.payment_fixtures.PaymentAdoptionPostgresTests.run_adoption(
                self, connection
            )
            self.payment_fixtures.PaymentAdoptionPostgresTests.live_adoption(
                self, connection
            )

    def preview007(self):
        # A synthetic test database only: the release has NO adoption entry.
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE platform_budget_reservations ADD COLUMN budget_role VARCHAR(20) NOT NULL DEFAULT 'enforced' CONSTRAINT ck_budget_reservation_role CHECK (budget_role IN ('accounting', 'enforced'))"
            )
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num='20261022_0007'"
            )

    def test_native006_and007_exports_preserve_original_rows_and_reader_metadata(self):
        from studio_platform.backup import _export_postgres_business_db
        from studio_platform.schema_baseline import compare_schema

        with self.engine.begin() as connection:
            seed_original_rows(connection, "20261021_0006")
        for revision in ("20261021_0006", CREDIT_ADMISSION_REVISION):
            if revision == CREDIT_ADMISSION_REVISION:
                self.preview007()
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "UPDATE platform_budget_reservations SET budget_role='accounting' WHERE id='original-reservation-0'"
                    )
            with self.engine.connect() as connection:
                before = compare_schema(connection)
                self.assertTrue(before["matches"])
                self.assertEqual(
                    credit_admission.ready(connection),
                    revision == CREDIT_ADMISSION_REVISION,
                )
                expected = original_rows(connection, revision)
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "business.sqlite3"
                counts = _export_postgres_business_db(self.engine, target)
                self.assertEqual(counts["platform_budget_reservations"], 2)
                assert_published_rows(self, target, expected)
            with self.engine.connect() as connection:
                self.assertEqual(
                    compare_schema(connection)["schema_fingerprint"],
                    before["schema_fingerprint"],
                )
                self.assertEqual(original_rows(connection, revision), expected)
            self.assertNotIn("budget_role", budget_reservations.c)

    def test_native007_bad_role_check_refused_before_business_export(self):
        from studio_platform.backup import (
            BackupError,
            _export_postgres_business_db,
            dump_postgres,
        )

        self.preview007()
        for check in (
            "CHECK (budget_role IN ('ACCOUNTING', 'enforced'))",
            "CHECK (budget_role IN ('accounting', 'enforced')) NOT VALID",
        ):
            with self.subTest(check=check):
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE platform_budget_reservations DROP CONSTRAINT ck_budget_reservation_role"
                    )
                    connection.exec_driver_sql(
                        "ALTER TABLE platform_budget_reservations ADD CONSTRAINT ck_budget_reservation_role "
                        + check
                    )
                with (
                    self.engine.connect() as connection,
                    self.assertRaisesRegex(
                        credits.CreditError, "schema_requires_review"
                    ),
                ):
                    credit_admission.ready(connection)
                with (
                    tempfile.TemporaryDirectory() as temp,
                    self.assertRaisesRegex(BackupError, "schema_requires_review"),
                ):
                    _export_postgres_business_db(
                        self.engine, Path(temp) / "refused.sqlite3"
                    )
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    url = root / "synthetic-local-url"
                    url.write_text(
                        self.engine.url.set(password="synthetic").render_as_string(
                            hide_password=False
                        ),
                        encoding="utf-8",
                    )
                    url.chmod(0o600)
                    real_run, dump_calls = subprocess.run, []

                    def observe(command, **kwargs):
                        if command[0] == "pg_dump":
                            dump_calls.append(command[0])
                        return real_run(command, **kwargs)

                    with (
                        patch(
                            "studio_platform.backup.subprocess.run", side_effect=observe
                        ),
                        self.assertRaisesRegex(
                            BackupError, "backup_postgres_dump_failed_no_secret_output"
                        ),
                    ):
                        dump_postgres(url, root / "refused-dump")
                    self.assertEqual(dump_calls, [])
                    self.assertFalse((root / "refused-dump" / "database.dump").exists())

    def test_native007_dump_contains_original_role_column_and_values(self):
        from studio_platform.backup import dump_postgres

        self.preview007()
        with self.engine.begin() as connection:
            seed_original_rows(connection, CREDIT_ADMISSION_REVISION)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            url = root / "synthetic-local-url"
            url.write_text(
                self.engine.url.set(password="synthetic").render_as_string(
                    hide_password=False
                ),
                encoding="utf-8",
            )
            url.chmod(0o600)
            target = root / "dump"
            dump_postgres(
                url, target, pg_dump=os.environ.get("PLATFORM_TEST_PG_DUMP", "pg_dump")
            )
            result = subprocess.run(
                [
                    os.environ.get("PLATFORM_TEST_PG_RESTORE", "pg_restore"),
                    "--data-only",
                    "--table=platform_budget_reservations",
                    "--file=-",
                    str(target / "database.dump"),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.assertIn("budget_role", result.stdout)
            rows = [
                line.split("\t")
                for line in result.stdout.splitlines()
                if line.startswith("original-reservation-")
            ]
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {row[0]: (row[4], row[5], row[6], row[-1]) for row in rows},
                {
                    "original-reservation-0": (
                        "100000",
                        "reserved",
                        "\\N",
                        "accounting",
                    ),
                    "original-reservation-1": ("100001", "settled", "987", "enforced"),
                },
            )
