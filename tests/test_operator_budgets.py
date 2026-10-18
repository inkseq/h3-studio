"""Original-ledger isolation, settlement dates and audited limit CAS only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock
import uuid

from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError

from studio_platform.auth import Principal
from studio_platform.operator_budgets import OperatorBudgets
from studio_platform.operator_capacity import OperatorError
from studio_platform.repository import (
    BudgetExceeded,
    Conflict,
    NotFound,
    Repository,
    Scope,
    budget_accounts,
    budget_reservations,
    budget_snapshot,
    outbox,
)
import test_platform_repository as ledger_fixture


class OperatorBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-budget-panel-test-")
        self.now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp()
        self.repo = Repository(
            "sqlite:///" + (Path(self.temp.name) / "ledger.sqlite3").as_posix(),
            clock=lambda: self.now,
        )
        self.repo.create_schema()
        self.settings = SimpleNamespace(
            tenant_id="tenant-a", operator_admin_owners=("superdan",)
        )
        self.service = OperatorBudgets(self.repo, self.settings)
        self.human = Principal("supervan", "browser-supervan")
        self.admin = Principal("superdan", "browser-superdan")
        self.scope = Scope("tenant-a", "supervan", "project-a")
        self.repo.configure_budget(
            "owner", tenant_id="tenant-a", owner_id="supervan", limit_microusd=1000
        )

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def reserve(self, amount=100, *, accounts=("owner",), reference=None):
        reference = reference or str(uuid.uuid4())
        with self.repo.transaction() as connection:
            self.repo._reserve(
                connection, self.scope, accounts, "job", reference, amount
            )
        return reference

    def settle(self, reference, actual):
        with self.repo.transaction() as connection:
            self.repo._settle(connection, "job", reference, actual)

    def rows(self, table):
        with self.repo.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def update_limit(self, new_limit, snapshot=None, account="owner"):
        return self.service.update(
            self.admin,
            account,
            {
                "expected_snapshot": snapshot
                or budget_snapshot(self.repo.get_budget(account)),
                "limit_microusd": new_limit,
            },
        )

    def test_owner_read_is_human_only_and_independent_of_capacity_enabled(self):
        repository = Mock()
        repository.list_budget_projections.return_value = {
            "budgets": [],
            "next_after_id": None,
            "as_of": self.now,
        }
        service = OperatorBudgets(repository, self.settings)
        view = service.owned(self.human)
        self.assertEqual(view["account"], "supervan")
        self.assertEqual(
            {key: view[key] for key in ("basis", "currency", "timezone")},
            {"basis": "cumulative", "currency": "USD", "timezone": "UTC"},
        )
        repository.list_budget_projections.assert_called_once_with(
            "tenant-a", owner_id="supervan", account_ids=None, limit=100, after_id=None
        )
        repository.reset_mock()
        for principal in (None, Principal("supervan", "pat-client", machine=True)):
            with (
                self.subTest(principal=principal),
                self.assertRaises(OperatorError) as raised,
            ):
                service.owned(principal)
            self.assertEqual(raised.exception.status, 403)
        repository.list_budget_projections.assert_not_called()

    def test_owner_scope_includes_project_and_legacy_but_never_global_or_foreign(self):
        for account, tenant, owner, project in (
            ("project", "tenant-a", "supervan", "project-a"),
            ("legacy", "tenant-a", "supervan", "retired-project"),
            ("global", "tenant-a", None, None),
            ("other", "tenant-a", "superdan", None),
            ("foreign", "tenant-b", "supervan", None),
        ):
            self.repo.configure_budget(
                account,
                tenant_id=tenant,
                owner_id=owner,
                project_id=project,
                limit_microusd=1000,
            )
        self.assertEqual(
            {row["id"] for row in self.service.owned(self.human)["budgets"]},
            {"owner", "project", "legacy"},
        )
        self.assertEqual(
            [
                row["id"]
                for row in self.service.owned(
                    self.human, ["owner", "global", "other", "foreign"]
                )["budgets"]
            ],
            ["owner"],
        )
        self.assertEqual(self.service.owned(self.human, [])["budgets"], [])
        admin_view = self.service.admin(self.admin)
        self.assertEqual(admin_view["account"], "superdan")
        self.assertIs(admin_view["admin"], True)
        self.assertEqual(
            {key: admin_view[key] for key in ("basis", "currency", "timezone")},
            {"basis": "cumulative", "currency": "USD", "timezone": "UTC"},
        )
        self.assertEqual(
            {row["id"] for row in admin_view["budgets"]},
            {"owner", "project", "legacy", "global", "other"},
        )
        first = self.service.owned(self.human, limit=1)
        second = self.service.owned(
            self.human, limit=1, after_id=first["next_after_id"]
        )
        self.assertNotEqual(first["budgets"][0]["id"], second["budgets"][0]["id"])
        for method in (
            lambda: self.service.admin(self.human),
            lambda: self.service.audit(self.human),
            lambda: self.service.update(self.human, "owner", {}),
        ):
            with self.assertRaises(OperatorError) as raised:
                method()
            self.assertEqual(raised.exception.status, 403)

    def test_projection_is_readonly_and_unknown_cost_stays_reserved(self):
        self.reserve(850)
        before = (
            self.rows(budget_accounts),
            self.rows(budget_reservations),
            self.rows(outbox),
        )
        writes = []

        def observe(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split(" ", 1)[0].upper() in {
                "INSERT",
                "UPDATE",
                "DELETE",
                "ALTER",
                "CREATE",
            }:
                writes.append(statement)

        event.listen(self.repo.engine, "before_cursor_execute", observe)
        try:
            dto = self.service.owned(self.human)["budgets"][0]
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", observe)
        self.assertEqual(writes, [])
        self.assertEqual(dto["basis"], "cumulative")
        self.assertEqual(dto["status"], "warning")
        self.assertEqual(dto["remaining_microusd"], 150)
        self.assertEqual(dto["daily7"], [])
        self.assertEqual(
            dto["unattributed_legacy"], {"cost_microusd": 0, "reservation_count": 0}
        )
        self.assertIsNone(self.rows(budget_reservations)[0]["settled_at"])

    def test_generation_pending_is_not_free_and_rental_is_a_separate_original_account_layer(
        self,
    ):
        pending = self.reserve(300)
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == pending)
                .values(state='invoice_pending')
            )
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == 'owner')
                .values(reserved_microusd=0)
            )
            self.repo._reserve(
                connection, self.scope, ('owner',), 'instance', 'ended-rental', 200
            )
            self.repo._settle(connection, 'instance', 'ended-rental', 175)
        before = (
            self.rows(budget_accounts),
            self.rows(budget_reservations),
            self.rows(outbox),
        )
        projection = self.service.owned(self.human)['budgets'][0]
        costs = projection['cost_breakdown']
        self.assertEqual(costs['basis'], 'original_account_reservations')
        self.assertIs(costs['account_layers_not_additive'], True)
        self.assertEqual(
            costs['job'],
            {
                'confirmed_cost_microusd': 0,
                'confirmed_reservation_count': 0,
                'pending_invoice_count': 1,
                'pending_estimate_microusd': 300,
                'pending_actual_cost_microusd': None,
            },
        )
        self.assertEqual(costs['instance']['confirmed_cost_microusd'], 175)
        self.assertEqual(costs['instance']['pending_invoice_count'], 0)
        self.assertEqual(projection['spent_microusd'], 175)
        self.assertEqual(projection['reserved_microusd'], 0)
        self.assertEqual(
            before,
            (
                self.rows(budget_accounts),
                self.rows(budget_reservations),
                self.rows(outbox),
            ),
        )
        self.assertEqual(
            before,
            (
                self.rows(budget_accounts),
                self.rows(budget_reservations),
                self.rows(outbox),
            ),
        )

    def test_additive_settlement_migration_preserves_legacy_null_and_balances(self):
        ref = self.reserve(100)
        self.settle(ref, 80)
        before = self.repo.get_budget("owner")
        with self.repo.transaction() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE platform_budget_reservations DROP COLUMN settled_at"
            )
        self.repo.create_schema()
        self.repo.create_schema()
        self.assertEqual(self.repo.get_budget("owner"), before)
        row = self.rows(budget_reservations)[0]
        self.assertEqual(row["state"], "settled")
        self.assertEqual(row["actual_cost_microusd"], 80)
        self.assertIsNone(row["settled_at"])
        self.settle(ref, 80)
        self.assertIsNone(self.rows(budget_reservations)[0]["settled_at"])
        dto = self.service.owned(self.human)["budgets"][0]
        self.assertEqual(dto["daily7"], [])
        self.assertEqual(
            dto["unattributed_legacy"], {"cost_microusd": 80, "reservation_count": 1}
        )

    def test_settlement_uses_actual_date_and_preserves_it_on_repeat(self):
        self.now -= 20 * 86400
        reference = self.reserve(100)
        self.now += 20 * 86400
        settled = self.now
        self.settle(reference, 70)
        self.now += 86400
        self.settle(reference, 70)
        self.assertEqual(self.rows(budget_reservations)[0]["settled_at"], settled)
        self.assertEqual(
            self.service.owned(self.human)["budgets"][0]["daily7"],
            [{"date": "2026-10-15", "cost_microusd": 70}],
        )
        with self.assertRaises(Conflict):
            self.settle(reference, 71)

    def test_daily7_is_sparse_utc_per_account_not_double_counted(self):
        self.repo.configure_budget("global", tenant_id="tenant-a", limit_microusd=1000)
        today = self.now
        for offset, amount in (
            (-8 * 86400, 11),
            (-6 * 86400, 12),
            (-86400, 13),
            (0, 14),
        ):
            self.now = today + offset
            reference = self.reserve(20, accounts=("owner", "global"))
            self.settle(reference, amount)
        self.now = today
        dto = self.service.owned(self.human)["budgets"][0]
        self.assertEqual(
            dto["daily7"],
            [
                {"date": "2026-10-09", "cost_microusd": 12},
                {"date": "2026-10-14", "cost_microusd": 13},
                {"date": "2026-10-15", "cost_microusd": 14},
            ],
        )
        self.assertEqual(dto["spent_microusd"], 50)
        self.assertEqual(
            dto["window"],
            {"timezone": "UTC", "start_date": "2026-10-09", "end_date": "2026-10-15"},
        )
        reference = self.reserve()
        self.settle(reference, 0)
        self.assertEqual(self.rows(budget_reservations)[-1]["state"], "released")
        self.assertEqual(self.rows(budget_reservations)[-1]["settled_at"], today)

    def test_limit_cas_changes_only_limit_and_emits_one_atomic_audit(self):
        reference = self.reserve(200)
        self.settle(reference, 120)
        self.reserve(300)
        reservations = self.rows(budget_reservations)
        before = self.repo.get_budget("owner")
        snapshot = budget_snapshot(before)
        response = self.update_limit(1500, snapshot)
        after = self.repo.get_budget("owner")
        self.assertEqual(after, dict(before, limit_microusd=1500))
        self.assertEqual(self.rows(budget_reservations), reservations)
        self.assertTrue(response["changed"])
        events = self.service.audit(self.admin)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], response["audit_event_id"])
        self.assertEqual(
            events[0]["actor"], {"owner_id": "superdan", "actor_id": "browser-superdan"}
        )
        self.assertEqual(events[0]["owner_id"], "supervan")
        self.assertEqual(
            events[0]["old"],
            {"limit_microusd": 1000, "spent_microusd": 120, "reserved_microusd": 300},
        )
        self.assertEqual(
            events[0]["new"],
            {"limit_microusd": 1500, "spent_microusd": 120, "reserved_microusd": 300},
        )
        with self.assertRaisesRegex(Conflict, "budget_snapshot_changed"):
            self.update_limit(1500, snapshot)
        no_change = self.update_limit(1500)
        self.assertFalse(no_change["changed"])
        self.assertIsNone(no_change["audit_event_id"])
        self.assertEqual(self.service.audit(self.admin)["events"], events)

    def test_committed_floor_and_reservation_change_refuse_without_effects(self):
        snapshot = budget_snapshot(self.repo.get_budget("owner"))
        self.reserve(900)
        with self.assertRaisesRegex(Conflict, "budget_snapshot_changed"):
            self.update_limit(1200, snapshot)
        with self.assertRaisesRegex(BudgetExceeded, "budget_below_committed"):
            self.update_limit(899)
        self.assertEqual(self.repo.get_budget("owner")["limit_microusd"], 1000)
        self.assertEqual(self.service.audit(self.admin)["events"], [])
        response = self.update_limit(900)
        self.assertEqual(response["budget"]["status"], "exhausted")

    def test_concurrent_cas_accepts_only_one_original_snapshot(self):
        snapshot = budget_snapshot(self.repo.get_budget("owner"))

        def attempt(limit):
            try:
                return self.update_limit(limit, snapshot)["changed"]
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, (1200, 1300)))
        self.assertCountEqual(results, [True, "conflict"])
        self.assertEqual(len(self.service.audit(self.admin)["events"]), 1)

    def test_audit_failure_rolls_back_limit(self):
        with self.repo.transaction() as connection:
            connection.exec_driver_sql("""CREATE TRIGGER reject_budget_audit BEFORE INSERT ON platform_outbox
                WHEN NEW.event_type='operator.budget_limit_updated'
                BEGIN SELECT RAISE(ABORT, 'isolated audit failure'); END""")
        before = self.repo.get_budget("owner")
        with self.assertRaises(IntegrityError):
            self.update_limit(1200)
        self.assertEqual(self.repo.get_budget("owner"), before)
        self.assertEqual(self.rows(outbox), [])

    def test_existing_account_only_tenant_scope_and_strict_update_body(self):
        self.repo.configure_budget(
            "foreign", tenant_id="tenant-b", owner_id="supervan", limit_microusd=1000
        )
        for account in ("missing", "foreign"):
            with self.subTest(account=account), self.assertRaises(NotFound):
                self.service.update(
                    self.admin,
                    account,
                    {"expected_snapshot": "a" * 64, "limit_microusd": 1000},
                )
        for value in (True, -1, 1.5, "1200", 9_000_000_000_000_001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.update_limit(value)
        for body in (
            {},
            {
                "limit_microusd": 1,
                "expected_snapshot": "a" * 64,
                "owner_id": "supervan",
            },
        ):
            with self.assertRaises(OperatorError):
                self.service.update(self.admin, "owner", body)
        for snapshot in (None, "A" * 64, "a" * 63):
            with self.assertRaises(ValueError):
                self.service.update(
                    self.admin,
                    "owner",
                    {"limit_microusd": 1, "expected_snapshot": snapshot},
                )
        self.assertEqual(self.repo.get_budget("owner")["limit_microusd"], 1000)
        self.assertEqual(self.rows(outbox), [])

    def test_audit_cursor_keeps_tied_timestamps_and_tenant_isolation(self):
        self.update_limit(1100)
        self.update_limit(1200)
        self.repo.configure_budget("foreign", tenant_id="tenant-b", limit_microusd=1000)
        self.repo.compare_set_budget_limit(
            "foreign",
            tenant_id="tenant-b",
            expected_snapshot=budget_snapshot(self.repo.get_budget("foreign")),
            limit_microusd=1100,
            actor_owner="superdan",
            actor_id="foreign-admin",
        )
        page = self.service.audit(self.admin, limit=1)
        next_page = self.service.audit(self.admin, limit=1, before=page["next_before"])
        self.assertIsNone(next_page["next_before"])
        self.assertNotEqual(page["events"][0]["id"], next_page["events"][0]["id"])
        self.assertEqual(
            {row["account_id"] for row in page["events"] + next_page["events"]},
            {"owner"},
        )


@unittest.skipUnless(
    os.environ.get("PLATFORM_TEST_DATABASE_URL"),
    "explicit isolated test PostgreSQL required",
)
class PostgresOperatorBudgetTests(ledger_fixture.LedgerCase):
    """Run the date SQL, row locks and additive DDL on the original PG fixture."""

    def budget_service(self):
        return OperatorBudgets(
            self.repo,
            SimpleNamespace(
                tenant_id=self.scope.tenant_id,
                operator_admin_owners=(self.scope.owner_id,),
            ),
        )

    def reserve_original(self, reference, amount=100_000):
        with self.repo.transaction() as connection:
            self.repo._reserve(
                connection, self.scope, ("owner-budget",), "job", reference, amount
            )

    def test_postgres_daily_utc_actual_settlement_and_legacy_null(self):
        self.now = datetime(2026, 10, 15, 0, 30, tzinfo=timezone.utc).timestamp()
        self.reserve_original("current")
        self.reserve_original("legacy")
        with self.repo.transaction() as connection:
            self.repo._settle(connection, "job", "current", 30_000)
            self.repo._settle(connection, "job", "legacy", 20_000)
            # Simulate an already confirmed row from the pre-column schema.
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == "legacy")
                .values(settled_at=None)
            )
        with self.repo.transaction() as connection:
            self.repo._settle(connection, "job", "legacy", 20_000)
        view = self.budget_service().owned(
            Principal(self.scope.owner_id, "pg-browser")
        )["budgets"][0]
        self.assertEqual(
            view["daily7"], [{"date": "2026-10-15", "cost_microusd": 30_000}]
        )
        self.assertEqual(
            view["unattributed_legacy"],
            {"cost_microusd": 20_000, "reservation_count": 1},
        )
        self.assertEqual(view["spent_microusd"], 50_000)
        self.assertEqual(view["reserved_microusd"], 0)

    def test_postgres_reservation_lock_invalidates_limit_cas_and_audit_failure_rolls_back(
        self,
    ):
        service = self.budget_service()
        admin = Principal(self.scope.owner_id, "pg-admin")
        snapshot = budget_snapshot(self.repo.get_budget("owner-budget"))
        entered = threading.Event()

        def waiting_cas():
            entered.set()
            return service.update(
                admin,
                "owner-budget",
                {"expected_snapshot": snapshot, "limit_microusd": 11_000_000},
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.repo.transaction() as connection:
                # Original funding holds the account lock until commit; the
                # stale admin snapshot must be rechecked after that lock moves.
                self.repo._reserve(
                    connection, self.scope, ("owner-budget",), "job", "racing", 100_000
                )
                pending = executor.submit(waiting_cas)
                self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(Conflict, "budget_snapshot_changed"):
                pending.result(timeout=10)
        before = self.repo.get_budget("owner-budget")
        self.assertEqual(before["reserved_microusd"], 100_000)
        self.assertEqual(before["limit_microusd"], 10_000_000)
        self.assertEqual(service.audit(admin)["events"], [])
        with self.repo.transaction() as connection:
            # This fixture owns an isolated disposable ledger_test_<UUID>
            # schema. A real PG constraint error proves transaction rollback.
            connection.exec_driver_sql(
                "ALTER TABLE platform_outbox ADD CONSTRAINT test_reject_budget_audit "
                "CHECK (event_type <> 'operator.budget_limit_updated')"
            )
        with self.assertRaises(IntegrityError):
            service.update(
                admin,
                "owner-budget",
                {
                    "expected_snapshot": budget_snapshot(before),
                    "limit_microusd": 11_000_000,
                },
            )
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(service.audit(admin)["events"], [])

    def test_postgres_repeated_additive_column_migration_preserves_old_settled_rows(
        self,
    ):
        self.reserve_original("old-settled")
        with self.repo.transaction() as connection:
            self.repo._settle(connection, "job", "old-settled", 70_000)
        before = self.repo.get_budget("owner-budget")
        with self.repo.transaction() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE platform_budget_reservations DROP COLUMN settled_at"
            )
        self.repo.create_schema()
        self.repo.create_schema()
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        with self.repo.engine.connect() as connection:
            row = (
                connection.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_id == "old-settled"
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(row["state"], "settled")
        self.assertEqual(row["actual_cost_microusd"], 70_000)
        self.assertIsNone(row["settled_at"])


if __name__ == "__main__":
    unittest.main()
