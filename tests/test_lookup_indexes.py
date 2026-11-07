"""Exact index reader, transactional adoption, rollback and local PG failures."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from studio_platform.lookup_indexes import INDEXES, adopt, plan
from studio_platform.schema_baseline import BaselineError, compare_schema
from studio_platform.schema_version import (
    CREDIT_ADMISSION_REVISION,
    LOOKUP_INDEX_REVISION,
)
import test_credit_admission_reader as reader_fixture
from test_credit_admission_reader import original_rows, seed_original_rows


class LookupReaderTests(unittest.TestCase):
    def test_007_optional_aliases_and_008_required_exact_shapes(self):
        engine = create_engine("sqlite://")
        try:
            reader_fixture.CreditAdmissionReaderTests.family(
                self, engine, CREDIT_ADMISSION_REVISION
            )
            with engine.begin() as connection:
                self.assertTrue(compare_schema(connection)["matches"])
                for table, name, columns in INDEXES:
                    connection.exec_driver_sql(
                        f"CREATE INDEX {name} ON {table} ({','.join(columns)})"
                    )
                self.assertTrue(compare_schema(connection)["matches"])
                connection.exec_driver_sql(
                    f"UPDATE alembic_version SET version_num='{LOOKUP_INDEX_REVISION}'"
                )
                self.assertTrue(compare_schema(connection)["matches"])
                connection.exec_driver_sql("DROP INDEX ix_outbox_aggregate")
                with self.assertRaisesRegex(BaselineError, "lookup_indexes_missing"):
                    compare_schema(connection)
                connection.exec_driver_sql(
                    "CREATE INDEX alias_aggregate ON platform_outbox (aggregate_id)"
                )
                self.assertTrue(compare_schema(connection)["matches"])
                self.assertEqual(plan(connection)[1]["existing"], ["alias_aggregate"])
                connection.exec_driver_sql("DROP INDEX alias_aggregate")
                connection.exec_driver_sql(
                    "CREATE INDEX ix_outbox_aggregate ON platform_outbox (aggregate_id) WHERE delivered_at IS NULL"
                )
                with self.assertRaisesRegex(
                    BaselineError, "lookup_index_name_conflict"
                ):
                    plan(connection)
                with self.assertRaisesRegex(BaselineError, "lookup_indexes_missing"):
                    compare_schema(connection)
        finally:
            engine.dispose()

    def test_writer_requires_verified_postgres_transaction(self):
        engine = create_engine("sqlite://")
        try:
            with (
                engine.begin() as connection,
                self.assertRaisesRegex(BaselineError, "verified_transaction"),
            ):
                adopt(connection, expected_fingerprint="a" * 64, tenant_id="sixnine")
        finally:
            engine.dispose()


@unittest.skipUnless(
    os.environ.get("PLATFORM_TEST_DATABASE_URL"), "isolated local PostgreSQL only"
)
class LookupPostgresTests(unittest.TestCase):
    from test_credit_admission_reader import (
        CreditAdmissionReaderPostgresTests as fixture,
    )

    tearDown = fixture.tearDown
    database = fixture.database
    # The older fixture calls self.adopt() to create its workflow family.
    adopt = fixture.adopt
    payment_fixtures = fixture.payment_fixtures

    def setUp(self):
        self.fixture.setUp(self)
        self.fixture.preview007(self)
        with self.engine.begin() as connection:
            seed_original_rows(connection, CREDIT_ADMISSION_REVISION)

    def run_adoption(self, connection, fingerprint=None):
        return adopt(
            connection,
            expected_fingerprint=fingerprint
            or compare_schema(connection)["schema_fingerprint"],
            tenant_id="sixnine",
        )

    def seed_paid_rows(self):
        from studio_platform import credits
        from studio_platform.repository import documents

        with self.engine.begin() as connection:
            self.payment_fixtures.fixtures.seed(connection)
            self.payment_fixtures.seed_payment(connection)
            connection.execute(
                insert(documents).values(
                    tenant_id="sixnine",
                    owner_id=credits.POLICY_OWNER,
                    project_id=credits.POLICY_PROJECT,
                    kind=credits.POLICY_KIND,
                    document_id=credits.POLICY_ID,
                    version=1,
                    updated_at=1000,
                    payload={"enabled": True, "active_revision": "initial"},
                )
            )

    def financial_rows(self, connection):
        from studio_platform.assets import asset_table
        from studio_platform.guided import receipts as edit_receipts

        return {
            **self.payment_fixtures.PaymentAdoptionPostgresTests.original_rows(
                connection
            ),
            "platform_budget_reservations": original_rows(
                connection, CREDIT_ADMISSION_REVISION
            ),
            **{
                table.name: sorted(
                    json.dumps(dict(row), sort_keys=True)
                    for row in connection.execute(select(table)).mappings()
                )
                for table in (asset_table, edit_receipts)
            },
        }

    def test_adoption_reuses_alias_preserves_rows_and_reader_backup(self):
        from studio_platform.backup import _export_postgres_business_db, dump_postgres
        from test_credit_admission_reader import assert_published_rows

        with self.engine.begin() as connection:
            before = original_rows(connection, CREDIT_ADMISSION_REVISION)
            connection.exec_driver_sql(
                "CREATE INDEX existing_aggregate ON platform_outbox (aggregate_id)"
            )
            result = self.run_adoption(connection)
            self.assertEqual(result["state"], "adopted_lookup_indexes")
            self.assertEqual(
                result["after"]["recorded_revision"], LOOKUP_INDEX_REVISION
            )
            self.assertEqual(
                before, original_rows(connection, CREDIT_ADMISSION_REVISION)
            )
            self.assertEqual(plan(connection)[1]["existing"], ["existing_aggregate"])
            self.assertEqual(self.run_adoption(connection)["state"], "already_adopted")
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "business.sqlite3"
            # A qualified rollback reader works without importing the writer.
            with patch.dict(
                "sys.modules",
                {"alembic": None, "studio_platform.schema_baseline": None},
            ):
                _export_postgres_business_db(self.engine, output)
            assert_published_rows(self, output, before)
            url_file = Path(folder) / "synthetic-local-url"
            url_file.write_text(
                self.engine.url.render_as_string(hide_password=False), encoding="utf-8"
            )
            url_file.chmod(0o600)
            dump_postgres(
                url_file,
                Path(folder) / "native",
                pg_dump=os.environ.get("PLATFORM_TEST_PG_DUMP", "pg_dump"),
            )
            restored = subprocess.run(
                [
                    os.environ.get("PLATFORM_TEST_PG_RESTORE", "pg_restore"),
                    "--data-only",
                    "--table=platform_budget_reservations",
                    "--file=-",
                    str(Path(folder) / "native" / "database.dump"),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ).stdout
            self.assertIn("budget_role", restored)
            self.assertIn("original-reservation-0", restored)
            self.assertIn("original-reservation-1", restored)

    def test_stale_snapshot_and_caller_failure_roll_back_all_ddl(self):
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(BaselineError, "schema_snapshot_changed"),
        ):
            self.run_adoption(connection, "0" * 64)
        with (
            self.assertRaisesRegex(RuntimeError, "synthetic abort"),
            self.engine.begin() as connection,
        ):
            self.run_adoption(connection)
            raise RuntimeError("synthetic abort")
        with self.engine.connect() as connection:
            self.assertEqual(
                compare_schema(connection)["recorded_revision"],
                CREDIT_ADMISSION_REVISION,
            )
            self.assertTrue(all(not item["existing"] for item in plan(connection)))

    def test_paid_adoption_and_repeat_preserve_original_money_and_policy(self):
        self.seed_paid_rows()
        with self.engine.connect() as connection:
            before = self.financial_rows(connection)
        for expected in ("adopted_lookup_indexes", "already_adopted"):
            with self.engine.begin() as connection:
                result = self.run_adoption(connection)
                self.assertEqual(result["state"], expected)
                self.assertEqual(
                    result["after"]["recorded_revision"], LOOKUP_INDEX_REVISION
                )
                self.assertTrue(all(item["existing"] for item in plan(connection)))
            with self.engine.connect() as connection:
                self.assertEqual(self.financial_rows(connection), before)

    def test_unverified_migration_still_refuses(self):
        from alembic import command
        from studio_platform.schema_baseline import alembic_config

        with self.engine.begin() as connection:
            config = alembic_config()
            config.attributes.update(
                verified_baseline_connection=connection,
                verified_baseline_mode="upgrade",
            )
            with self.assertRaisesRegex(RuntimeError, "verified_adoption_required"):
                command.upgrade(config, LOOKUP_INDEX_REVISION)
            self.assertEqual(
                compare_schema(connection)["recorded_revision"],
                CREDIT_ADMISSION_REVISION,
            )
            self.assertTrue(all(not item["existing"] for item in plan(connection)))

    def test_paid_cli_allows_lookup_but_retains_credit_admission_off_guard(self):
        from studio_platform.schema_baseline import main
        from studio_platform.settings import Settings

        self.seed_paid_rows()
        with self.engine.connect() as connection:
            before = self.financial_rows(connection)
        for mode in ("test", "live"):
            with (
                self.subTest(mode=mode),
                patch.object(
                    Settings,
                    "from_environment",
                    return_value=SimpleNamespace(stripe_mode=mode, tenant_id="sixnine"),
                ),
                patch(
                    "studio_platform.settings.database_url_from_environment",
                    return_value=self.engine.url.render_as_string(hide_password=False),
                ),
            ):
                with self.engine.connect() as connection:
                    fingerprint = compare_schema(connection)["schema_fingerprint"]
                error = io.StringIO()
                with redirect_stderr(error):
                    self.assertEqual(
                        main(
                            [
                                "adopt-credit-admission",
                                "--expected-fingerprint",
                                fingerprint,
                            ]
                        ),
                        1,
                    )
                self.assertEqual(
                    json.loads(error.getvalue())["code"],
                    "credit_admission_adoption_requires_payments_off",
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        main(
                            [
                                "adopt-lookup-indexes",
                                "--expected-fingerprint",
                                fingerprint,
                            ]
                        ),
                        0,
                    )
                self.assertEqual(
                    json.loads(output.getvalue())["state"],
                    "adopted_lookup_indexes" if mode == "test" else "already_adopted",
                )
                with self.engine.connect() as connection:
                    self.assertEqual(self.financial_rows(connection), before)

    def test_shared_account_gate_timeout_leaves_paid_007_unchanged(self):
        from studio_platform.identity import lock_account_gate

        self.seed_paid_rows()
        with self.engine.connect() as connection:
            before = self.financial_rows(connection)
            fingerprint = compare_schema(connection)["schema_fingerprint"]
        with self.engine.begin() as blocker:
            lock_account_gate(blocker)
            with self.assertRaises(DBAPIError) as raised:
                with self.engine.begin() as connection:
                    self.run_adoption(connection, fingerprint)
            self.assertEqual(raised.exception.orig.sqlstate, "55P03")
            with self.engine.connect() as connection:
                self.assertEqual(
                    compare_schema(connection)["recorded_revision"],
                    CREDIT_ADMISSION_REVISION,
                )
                self.assertTrue(all(not item["existing"] for item in plan(connection)))
                self.assertEqual(self.financial_rows(connection), before)
        with self.engine.begin() as connection:
            self.assertEqual(
                self.run_adoption(connection)["state"], "adopted_lookup_indexes"
            )
            self.assertEqual(self.financial_rows(connection), before)

    def test_service_capture_and_replay_wait_for_adoption_commit(self):
        from studio_platform import credits_pricing, service_credits as service
        from studio_platform.credit_schema import accounts, prices
        from studio_platform.repository import Scope, documents
        from test_service_credit_core import ServiceCreditTests

        self.seed_paid_rows()
        # Reuse attachment/receipt helpers on the already exact 007 database.
        fixture = ServiceCreditTests()
        fixture.now = 1000
        fixture.repo = SimpleNamespace(
            engine=self.engine, transaction=self.engine.begin, clock=lambda: fixture.now
        )
        fixture.scope = Scope(
            "sixnine", "supervan", "lookup-project", "browser:fixture"
        )
        fixture.reference = "story_chat_image:lookup_request_1234"
        fixture.binding = dict(
            tenant=fixture.scope.tenant_id,
            owner=fixture.scope.owner_id,
            project_id=fixture.scope.project_id,
            actor_id=fixture.scope.actor_id,
            card_id="lookup-card",
            request_id="lookup_request_1234",
            input_hash="a" * 64,
        )
        fixture.project = {
            "entities": [{"id": "lookup-card", "type": "character", "data": {}}],
            "journey": {"storyChat": {"images": {}}},
        }
        with self.engine.begin() as connection:
            connection.execute(
                update(prices).values(
                    configuration={
                        **credits_pricing.DEFAULT_CONFIGURATION,
                        "image_credits": 5,
                    }
                )
            )
            connection.execute(
                insert(documents).values(
                    tenant_id=fixture.scope.tenant_id,
                    owner_id=fixture.scope.owner_id,
                    project_id="__projects",
                    kind="project",
                    document_id=fixture.scope.project_id,
                    version=1,
                    updated_at=fixture.now,
                    payload=fixture.project,
                )
            )
        held = fixture.call(service.service_hold)
        attached = fixture.attach()
        captured = None
        for expected_created in (True, False):
            with self.engine.connect() as connection:
                before = self.financial_rows(connection)
            worker_pid = Queue()

            def capture():
                with self.engine.begin() as connection:
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '4s'")
                    worker_pid.put(
                        connection.exec_driver_sql(
                            "SELECT pg_backend_pid()"
                        ).scalar_one()
                    )
                    return service.service_capture(
                        connection,
                        fixture.repo,
                        fixture.scope,
                        fixture.reference,
                        binding=fixture.binding,
                    )

            with ThreadPoolExecutor(max_workers=1) as executor:
                with self.engine.begin() as adopter:
                    adopter_pid = adopter.exec_driver_sql(
                        "SELECT pg_backend_pid()"
                    ).scalar_one()
                    self.run_adoption(adopter)
                    pending = executor.submit(capture)
                    pid = worker_pid.get(timeout=2)
                    deadline = time.monotonic() + 2
                    waiting = False
                    with self.engine.connect() as observer:
                        while time.monotonic() < deadline and not pending.done():
                            waiting = observer.execute(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                    "WHERE pid=:worker AND locktype='advisory' "
                                    "AND NOT granted) "
                                    "AND :adopter = ANY(pg_blocking_pids(:worker))"
                                ),
                                {"worker": pid, "adopter": adopter_pid},
                            ).scalar_one()
                            if waiting:
                                break
                            time.sleep(0.01)
                        self.assertTrue(
                            waiting, "capture must wait on adopter's account gate"
                        )
                        self.assertFalse(pending.done())
                        self.assertEqual(self.financial_rows(observer), before)
                result = pending.result(timeout=5)
            self.assertEqual(result["created"], expected_created)
            self.assertEqual(result["hold"], held["hold"])
            if expected_created:
                captured = result["entry"]
                self.assertEqual(result["attachment"], attached)
                self.assertEqual(captured["amount"], -5)
                self.assertEqual(captured["held_delta"], -5)
            else:
                self.assertEqual(result["entry"], captured)
                with self.engine.connect() as connection:
                    self.assertEqual(self.financial_rows(connection), before)
            with self.engine.connect() as connection:
                after = self.financial_rows(connection)
                for table, rows in before.items():
                    if table not in {
                        "platform_credit_accounts",
                        "platform_credit_entries",
                    }:
                        self.assertEqual(after[table], rows)
                self.assertEqual(
                    connection.execute(
                        select(accounts.c.balance, accounts.c.held)
                    ).one(),
                    (95, 20),
                )

    def test_unknown_index_and_nonterminal_work_refuse(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE INDEX unexpected_lookup ON platform_outbox (created_at)"
            )
            with self.assertRaisesRegex(BaselineError, "require_adopted007"):
                self.run_adoption(connection)
        from studio_platform.repository import jobs, plans

        with self.engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX unexpected_lookup")
            scope = dict(
                tenant_id="sixnine", owner_id="synthetic", project_id="fixture"
            )
            connection.execute(
                insert(plans).values(
                    **scope,
                    id="lookup-plan",
                    request={},
                    request_hash="a" * 64,
                    execution_plan={},
                    plan_hash="b" * 64,
                    estimated_cost_microusd=0,
                    created_at=1000,
                    expires_at=2000,
                )
            )
            connection.execute(
                insert(jobs).values(
                    **scope,
                    id="lookup-job",
                    actor_id="fixture",
                    idempotency_key="fixture",
                    request_hash="a" * 64,
                    plan_id="lookup-plan",
                    request={},
                    execution_plan={},
                    status="queued",
                    pool="fixture",
                    expected_runtime_s=30,
                    estimated_cost_microusd=0,
                    created_at=1000,
                    updated_at=1000,
                    not_before=1000,
                )
            )
            for values in (
                dict(status="queued", lease_worker_id=None, lease_expires_at=None),
                dict(
                    status="succeeded", lease_worker_id="worker", lease_expires_at=None
                ),
                dict(status="succeeded", lease_worker_id=None, lease_expires_at=2000),
            ):
                connection.execute(update(jobs).values(**values))
                with self.assertRaisesRegex(BaselineError, "require_quiescence"):
                    self.run_adoption(connection)
            self.assertTrue(all(not item["existing"] for item in plan(connection)))

    def test_lock_timeout_and_statement_timeout_leave_retryable_007(self):
        with self.engine.connect() as blocker, blocker.begin():
            blocker.exec_driver_sql("LOCK TABLE platform_outbox IN ROW EXCLUSIVE MODE")
            with self.assertRaises(DBAPIError), self.engine.begin() as connection:
                self.run_adoption(connection)
        from alembic import op

        original = op.create_index
        calls = []

        def slow(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                op.get_bind().exec_driver_sql("SELECT pg_sleep(4)")
            return original(*args, **kwargs)

        with patch.object(op, "create_index", side_effect=slow):
            with self.assertRaises(DBAPIError), self.engine.begin() as connection:
                self.run_adoption(connection)
        with self.engine.begin() as connection:
            self.assertEqual(
                compare_schema(connection)["recorded_revision"],
                CREDIT_ADMISSION_REVISION,
            )
            self.assertTrue(all(not item["existing"] for item in plan(connection)))
            self.assertEqual(
                self.run_adoption(connection)["state"], "adopted_lookup_indexes"
            )
