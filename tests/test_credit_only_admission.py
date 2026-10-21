"""Disposable original-ledger admission; no runtime fixture or provider calls."""

import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    select,
    text,
    update,
)

from studio_platform import credits, credit_admission, credits_pricing
from studio_platform.credit_schema import metadata as credit_metadata, prices, entries
from studio_platform.identity_schema import users, admin_audit
from studio_platform.repository import (
    BudgetExceeded,
    budget_accounts,
    budget_reservations,
    documents,
    jobs,
)
from studio_platform.schema_version import CREDIT_ADMISSION_REVISION
from studio_platform.credit_margin import report
import test_credits as fixtures
from test_platform_repository import LedgerCase


class CreditOnlyTests(LedgerCase):
    policy = fixtures.CreditTests.policy
    seed = fixtures.CreditTests.seed
    balance = fixtures.CreditTests.balance

    def setUp(self):
        super().setUp()
        self.user_id = str(uuid.uuid4())
        marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        with self.repo.transaction() as connection:
            users.create(connection)
            admin_audit.create(connection)
            credit_metadata.create_all(connection)
            marker.create(connection)
            connection.execute(
                insert(marker).values(version_num=CREDIT_ADMISSION_REVISION)
            )
            connection.exec_driver_sql(
                "ALTER TABLE platform_budget_reservations ADD COLUMN budget_role VARCHAR(20) NOT NULL DEFAULT 'enforced' CONSTRAINT ck_budget_reservation_role CHECK (budget_role IN ('accounting', 'enforced'))"
            )
            connection.execute(
                insert(users).values(
                    id=self.user_id,
                    tenant=self.scope.tenant_id,
                    owner=self.scope.owner_id,
                    role="user",
                    status="active",
                    display_name="Disposable fixture",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            connection.execute(
                insert(prices).values(
                    tenant=self.scope.tenant_id,
                    revision="price-1",
                    rates=credits_pricing.DEFAULT_RATES,
                    configuration=credits_pricing.DEFAULT_CONFIGURATION,
                    published_by=self.user_id,
                    published_at=self.now,
                )
            )
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == "owner-budget")
                .values(limit_microusd=0)
            )
        self.seed(100)
        self.policy()

    def plan(self, *, account_ids=("owner-budget",), alter=None):
        compiled = {
            "recipe_id": "h3-base-fl2va-v1",
            "request": {"model": "MiniMax-H3-Pruned-Rank8-INT8"},
            "output_spec": {"frames": 120, "fps": 24},
        }
        execution = {
            "pool": "test-pool",
            "backend": "mock",
            "expected_runtime_s": 120,
            "budget_account_ids": list(account_ids),
        }
        with self.repo.engine.connect() as connection:
            execution["credit_quote"] = credits_pricing.quote(
                connection,
                self.scope.tenant_id,
                self.scope.owner_id,
                compiled,
                execution=execution,
            )
        if alter:
            alter(execution)
        return self.repo.create_plan(
            self.scope,
            compiled,
            execution,
            expires_at=self.now + 1000,
            estimated_cost_microusd=100_000,
        )

    def admit(
        self, plan=None, *, key=None, account_ids=("owner-budget",), status="queued"
    ):
        return self.repo.create_job(
            self.scope,
            (plan or self.plan())["id"],
            key or uuid.uuid4().hex,
            budget_account_ids=account_ids,
            initial_status=status,
        )

    def assert_empty(self):
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(jobs)).all(), [])
            self.assertEqual(connection.execute(select(budget_reservations)).all(), [])
            self.assertEqual(
                list(connection.execute(select(entries.c.kind)).scalars()), ["grant"]
            )
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        self.assertEqual(self.balance()["held"], 0)

    def test_positive_credit_zero_usd_original_row_and_idempotency(self):
        plan = self.plan()
        job = self.admit(plan, key="original")
        self.assertEqual(self.balance()["held"], 20)
        account = self.repo.get_budget("owner-budget")
        self.assertEqual(
            (account["limit_microusd"], account["reserved_microusd"]), (0, 100_000)
        )
        with self.repo.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT reference_id,amount_microusd,budget_role FROM platform_budget_reservations"
                )
            ).all()
        self.assertEqual(rows, [(job["id"], 100_000, "accounting")])
        self.policy(False)
        self.assertFalse(self.admit(plan, key="original")["created"])
        self.assertEqual(self.balance()["held"], 20)

    def test_insufficient_credit_402_rolls_back_original_rows(self):
        with self.repo.transaction() as connection:
            credits.adjust(
                connection,
                self.scope.tenant_id,
                self.scope.owner_id,
                -90,
                actor="browser:admin",
                reason="Fixture insufficient balance",
                idempotency_key="reduce",
                now=self.now,
            )
        with self.assertRaises(credits.CreditError) as error:
            self.admit()
        self.assertEqual(error.exception.status, 402)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(jobs)).all(), [])
            self.assertEqual(connection.execute(select(budget_reservations)).all(), [])
        self.assertEqual(self.balance()["held"], 0)

    def test_policy_off_between_reserve_and_hold_rolls_back(self):
        original = credits.hold

        def changed(connection, *args):
            connection.execute(
                update(documents)
                .where(documents.c.kind == credits.POLICY_KIND)
                .values(payload={"enabled": False, "active_revision": "price-1"})
            )
            return original(connection, *args)

        with (
            patch.object(credits, "hold", side_effect=changed),
            self.assertRaisesRegex(credits.CreditError, "repreflight"),
        ):
            self.admit()
        self.assert_empty()

    def test_price_revision_change_between_reserve_and_hold_rolls_back(self):
        original = credits.hold

        def changed(connection, *args):
            connection.execute(
                update(documents)
                .where(documents.c.kind == credits.POLICY_KIND)
                .values(payload={"enabled": True, "active_revision": "price-2"})
            )
            return original(connection, *args)

        with (
            patch.object(credits, "hold", side_effect=changed),
            self.assertRaisesRegex(credits.CreditError, "repreflight"),
        ):
            self.admit()
        self.assert_empty()

    def test_audit_failure_rolls_back_hold_reserve_job(self):
        with (
            patch.object(
                self.repo, "_emit", side_effect=RuntimeError("fixture audit failure")
            ),
            self.assertRaisesRegex(RuntimeError, "audit failure"),
        ):
            self.admit()
        self.assert_empty()

    def test_forged_role_price_and_account_selection_are_refused(self):
        for change in (
            lambda execution: execution["credit_quote"]["budget_rows"][0].update(
                budget_role="enforced"
            ),
            lambda execution: execution["credit_quote"].update(credits=0),
            lambda execution: execution["credit_quote"].update(price_revision="stale"),
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(credits.CreditError, "repreflight"),
            ):
                self.admit(self.plan(alter=change))
        with self.assertRaisesRegex(credits.CreditError, "repreflight"):
            self.admit(self.plan(), account_ids=())
        self.assert_empty()

    def test_shared_usd_cap_and_manual_instance_cap_stay_enforced(self):
        self.repo.configure_budget(
            "shared-budget", tenant_id=self.scope.tenant_id, limit_microusd=0
        )
        identifiers = ("owner-budget", "shared-budget")
        with self.assertRaises(BudgetExceeded):
            self.admit(self.plan(account_ids=identifiers), account_ids=identifiers)
        self.assert_empty()
        with self.repo.transaction() as connection, self.assertRaises(BudgetExceeded):
            self.repo._reserve(
                connection,
                self.scope,
                ("owner-budget",),
                "instance",
                "manual-test",
                100_000,
            )

    def test_other_owner_cannot_be_relabelled_accounting(self):
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == "owner-budget")
                .values(owner_id=self.other.owner_id)
            )
        with self.assertRaisesRegex(credits.CreditError, "credit_quote_invalid"):
            self.plan()

    def test_protected_policy_preflight_bypasses_only_owner_usd_row(self):
        from studio_platform.capabilities import compile_request
        from studio_platform.execution_policy import ExecutionPolicies
        from studio_platform.settings import Settings
        from test_platform_api import generation_request
        from test_platform_execution_policy import policy

        path = Path(self.temp.name) / "protected-policy.json"
        value = policy(self.now)
        value["budget_accounts"] = ["owner-budget"]
        path.write_text(json.dumps(value), encoding="utf-8")
        settings = Settings(
            Path(self.temp.name),
            tenant_id=self.scope.tenant_id,
            execution_backend="comfy-worker",
            generation_enabled=True,
            execution_policy_file=path,
        )
        policies = ExecutionPolicies(settings, self.repo)
        compiled, fingerprint = compile_request(generation_request(), lambda _: None)
        with patch.object(
            policies.control, "pool_status", return_value={"ready": 1, "busy": 0}
        ):
            admitted = policies.evaluate(compiled, self.scope, fingerprint)
        self.assertTrue(admitted.execution["enabled"])
        self.assertEqual(admitted.execution["credit_quote"]["admission"], "credits")
        self.assertEqual(admitted.cost, 500_000)
        self.assertEqual(
            admitted.execution["credit_quote"]["budget_rows"][0]["budget_role"],
            "accounting",
        )

    def test_public_admission_roles_hide_protected_account_ids(self):
        from studio_platform.generation_services import GenerationRead

        execution = self.plan()["execution_plan"]
        public = GenerationRead.execution(execution)
        self.assertEqual(public["admission"], "credits")
        self.assertEqual(
            public["budget_rows"], [{"scope": "owner", "budget_role": "accounting"}]
        )
        self.assertNotIn("owner-budget", str(public))
        self.assertEqual(credits_pricing.public_quote({}), {"admission": "usd"})

    def test_planned_enqueue_uses_same_atomic_path(self):
        job = self.admit(status="planned")
        self.assertEqual(self.balance()["held"], 0)
        self.repo.enqueue(self.scope, job["id"], budget_account_ids=("owner-budget",))
        self.assertEqual(self.balance()["held"], 20)

    def test_006_retains_original_usd_admission_and_007_requires_new_quote(self):
        with self.repo.transaction() as connection:
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num='20261021_0006'"
            )
        with (
            self.repo.engine.connect() as connection,
            self.assertRaisesRegex(credits.CreditError, "schema_requires_review"),
        ):
            credit_admission.ready(connection)
        with self.repo.transaction() as connection:
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num='20261022_0007'"
            )
        plan = self.plan(
            alter=lambda execution: execution["credit_quote"].update(version=1)
        )
        with self.assertRaisesRegex(credits.CreditError, "repreflight"):
            self.admit(plan)
        self.assert_empty()

    def test_accepted_version1_hold_and_terminal_survive007_without_repricing(self):
        legacy = fixtures.CreditTests("runTest")
        legacy.setUp()
        try:
            legacy.seed(100)
            job = legacy.credit_job()
            legacy.hold(job)
            with legacy.repo.transaction() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE platform_budget_reservations ADD COLUMN budget_role VARCHAR(20) NOT NULL DEFAULT 'enforced' CONSTRAINT ck_budget_reservation_role CHECK (budget_role IN ('accounting', 'enforced'))"
                )
                connection.exec_driver_sql(
                    "UPDATE alembic_version SET version_num='20261022_0007'"
                )
            legacy.policy(False)
            legacy.complete(job)
            self.assertEqual(
                (legacy.balance()["balance"], legacy.balance()["held"]), (80, 0)
            )
            with legacy.repo.engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        text("SELECT budget_role FROM platform_budget_reservations")
                    ).scalar_one(),
                    "enforced",
                )
        finally:
            legacy.tearDown()

    def captured_job(self, *, cost=100_000, pending=False, shared=True):
        job = self.admit()
        with self.repo.transaction() as connection:
            connection.execute(
                insert(entries).values(
                    id=str(uuid.uuid4()),
                    tenant=self.scope.tenant_id,
                    user_id=self.user_id,
                    kind="capture",
                    amount=-20,
                    held_delta=-20,
                    balance_after=80,
                    held_after=0,
                    reference_type="job",
                    reference_id=job["id"],
                    actor="fixture",
                    reason="Fixture capture",
                    idempotency_key="fixture-capture",
                    fingerprint="a" * 64,
                    price_revision="price-1",
                    created_at=self.now + 1,
                )
            )
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == job["id"])
                .values(
                    state="invoice_pending" if pending else "settled",
                    actual_cost_microusd=None if pending else cost,
                )
            )
            if shared:
                original = (
                    connection.execute(
                        select(budget_reservations).where(
                            budget_reservations.c.reference_id == job["id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                connection.execute(
                    insert(budget_accounts).values(
                        id="shared-budget",
                        tenant_id=self.scope.tenant_id,
                        limit_microusd=1_000_000,
                        reserved_microusd=0,
                        spent_microusd=0,
                    )
                )
                connection.execute(
                    insert(budget_reservations).values(
                        **{
                            **dict(original),
                            "id": str(uuid.uuid4()),
                            "account_id": "shared-budget",
                        }
                    )
                )
        return job

    def test_margin_deduplicates_layers_and_late_invoice_same_job_cohort(self):
        job = self.captured_job(pending=True)
        self.now += 10
        with self.repo.engine.connect() as connection:
            before = report(connection, self.scope.tenant_id, self.now)
        self.assertEqual(before["invoice_pending_microusd"], 100_000)
        self.assertEqual(before["actual_microusd"], 0)
        self.assertEqual(before["captured_value_microusd"], 200_000)
        self.assertEqual(before["status"], "estimated_pending")
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == job["id"])
                .values(state="settled", actual_cost_microusd=180_000)
            )
            configuration = copy.deepcopy(credits_pricing.DEFAULT_CONFIGURATION)
            configuration["credit_usd"] = 0.50
            connection.execute(
                insert(prices).values(
                    tenant=self.scope.tenant_id,
                    revision="new-current",
                    rates=credits_pricing.DEFAULT_RATES,
                    configuration=configuration,
                    published_by=self.user_id,
                    published_at=self.now,
                )
            )
            connection.execute(
                update(documents)
                .where(documents.c.kind == credits.POLICY_KIND)
                .values(payload={"enabled": True, "active_revision": "new-current"})
            )
        with self.repo.engine.connect() as connection:
            after = report(connection, self.scope.tenant_id, self.now)
        self.assertEqual(after["actual_microusd"], 180_000)
        self.assertEqual(after["invoice_pending_microusd"], 0)
        self.assertEqual(after["captured_value_microusd"], 200_000)
        self.assertTrue(after["below_30_percent"])

    def test_margin_zero_exposure_and_unknown_revision_are_undefined(self):
        self.captured_job(cost=0)
        self.now += 10
        with self.repo.engine.connect() as connection:
            value = report(connection, self.scope.tenant_id, self.now)
        self.assertIsNone(value["margin"])
        self.assertIsNone(value["below_30_percent"])
        with self.repo.transaction() as connection:
            connection.execute(
                update(entries)
                .where(entries.c.kind == "capture")
                .values(price_revision="missing")
            )
        with self.repo.engine.connect() as connection:
            value = report(connection, self.scope.tenant_id, self.now)
        self.assertEqual(value["status"], "unknown")
        self.assertIsNone(value["captured_value_microusd"])

    def test_missing_cost_rows_never_infer_actual_from_zero_or_positive_quote(self):
        job = self.captured_job(cost=0, shared=False)
        self.now += 10
        with self.repo.transaction() as connection:
            connection.execute(
                delete(budget_reservations).where(
                    budget_reservations.c.reference_id == job["id"]
                )
            )
        for estimate in (0, 100_000):
            with self.subTest(estimate=estimate):
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(jobs)
                        .where(jobs.c.id == job["id"])
                        .values(estimated_cost_microusd=estimate)
                    )
                with self.repo.engine.connect() as connection:
                    value = report(connection, self.scope.tenant_id, self.now)
                self.assertEqual(value["unknown_cost_jobs"], 1)
                self.assertEqual(value["status"], "unknown")
                self.assertIsNone(value["actual_microusd"])
                self.assertIsNone(value["exposure_microusd"])
                self.assertIsNone(value["margin"])

    def test_zero_cpu_cost_requires_original_settled_evidence(self):
        from studio_platform.credit_margin import exposure

        self.assertIsNone(exposure([]))
        self.assertIsNone(
            exposure(
                [
                    {
                        "amount_microusd": 0,
                        "state": "reserved",
                        "actual_cost_microusd": None,
                    }
                ]
            )
        )
        self.assertIsNone(
            exposure(
                [
                    {
                        "amount_microusd": 0,
                        "state": "invoice_pending",
                        "actual_cost_microusd": None,
                    }
                ]
            )
        )
        self.assertEqual(
            exposure(
                [
                    {
                        "amount_microusd": 0,
                        "state": "settled",
                        "actual_cost_microusd": 0,
                    }
                ]
            ),
            {"actual_microusd": 0},
        )
        reserved = {
            "amount_microusd": 0,
            "state": "reserved",
            "actual_cost_microusd": 0,
        }
        settled = {**reserved, "state": "settled"}
        self.assertIsNone(exposure([reserved]))
        self.assertIsNone(exposure([reserved, settled]))

    def test_ambiguous_zero_cost_layers_remain_unknown_in_admin_report(self):
        job = self.captured_job(cost=0)
        self.now += 10
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == job["id"])
                .values(amount_microusd=0)
            )
            connection.execute(
                update(budget_reservations)
                .where(
                    budget_reservations.c.reference_id == job["id"],
                    budget_reservations.c.account_id == "owner-budget",
                )
                .values(state="reserved")
            )
        with self.repo.engine.connect() as connection:
            value = report(connection, self.scope.tenant_id, self.now)
        self.assertEqual(value["unknown_cost_jobs"], 1)
        self.assertEqual(value["status"], "unknown")
        self.assertIsNone(value["actual_microusd"])
        self.assertIsNone(value["exposure_microusd"])
        self.assertIsNone(value["margin"])


class CreditAdmissionSchemaTests(unittest.TestCase):
    def family(self, engine, revision, *, role_check=None):
        from studio_platform.schema_baseline import frozen_metadata
        from studio_platform.identity_schema import metadata as identity
        from studio_platform.payment_schema import frozen_metadata as payment_metadata
        from studio_platform.migrations.versions.v20261022_0007_credit_admission import (
            extend,
        )

        baseline = frozen_metadata()
        if revision == CREDIT_ADMISSION_REVISION:
            extend(baseline)
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
                finally:
                    engine.dispose()

    def test_generic_migration_and_constructor_cannot_adopt(self):
        from types import SimpleNamespace
        from studio_platform.migrations.versions import (
            v20261022_0007_credit_admission as migration,
        )
        from studio_platform.schema_baseline import (
            BaselineError,
            adopt_credit_admission,
        )

        for dialect, verified, transaction in (
            ("sqlite", True, True),
            ("postgresql", False, True),
            ("postgresql", True, False),
        ):
            connection = SimpleNamespace(
                dialect=SimpleNamespace(name=dialect),
                in_transaction=lambda: transaction,
            )
            context = SimpleNamespace(
                is_offline_mode=lambda: False,
                config=SimpleNamespace(
                    attributes={"verified_credit_admission_mode": verified}
                ),
            )
            operation = SimpleNamespace(get_bind=lambda: connection)
            with (
                patch("alembic.context", context),
                patch("alembic.op", operation),
                self.assertRaisesRegex(RuntimeError, "verified_adoption_required"),
            ):
                migration.upgrade()
        engine = create_engine("sqlite://")
        try:
            with (
                engine.begin() as connection,
                self.assertRaisesRegex(BaselineError, "verified_transaction"),
            ):
                adopt_credit_admission(
                    connection, expected_fingerprint="a" * 64, tenant_id="sixnine"
                )
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


@unittest.skipUnless(
    os.environ.get("PLATFORM_TEST_DATABASE_URL"),
    "explicit isolated local PostgreSQL only",
)
class CreditAdmissionPostgresTests(unittest.TestCase):
    # Reuse the real adopted workflow/identity/credit/payment chain, including
    # its localhost allowlist and fresh per-test database ownership/cleanup.
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

    def test_guarded006_to007_and_rollback_reader(self):
        from studio_platform.schema_baseline import (
            adopt_credit_admission,
            compare_schema,
        )

        with self.engine.begin() as connection:
            before = compare_schema(connection)
            result = adopt_credit_admission(
                connection,
                expected_fingerprint=before["schema_fingerprint"],
                tenant_id="sixnine",
            )
            self.assertTrue(result["after"]["matches"])
            self.assertEqual(
                result["after"]["recorded_revision"], CREDIT_ADMISSION_REVISION
            )
            self.assertTrue(credit_admission.ready(connection))
