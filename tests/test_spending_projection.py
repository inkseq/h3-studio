"""Read-only spend presentation over original, layered ledger obligations."""
from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import delete, insert, select, update

from studio_platform.dstack_test_budget import day_window
from studio_platform.repository import (BudgetExceeded, Scope, budget_accounts,
    budget_reservations, jobs, spending_origins)
from studio_platform.spending_limits import daily_projection, public_usage
from test_platform_repository import LedgerCase


class SpendingProjectionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc).timestamp()
        self.repo.configure_budget("shared", tenant_id=self.scope.tenant_id, limit_microusd=10_000_000)
        self.repo.configure_budget("other-owner", tenant_id=self.scope.tenant_id,
            owner_id=self.other.owner_id, limit_microusd=10_000_000)
        self.repo.configure_budget("other-tenant", tenant_id="other-tenant", limit_microusd=10_000_000)

    def reservation(self, *, ident=None, kind="job", account="owner-budget", amount=100,
                    state="reserved", actual=None, created=None, settled=None,
                    owner="superdan", actor="key:a"):
        ident = ident or uuid.uuid4().hex
        with self.repo.transaction() as conn:
            conn.execute(insert(budget_reservations).values(id=str(uuid.uuid4()), account_id=account,
                reference_type=kind, reference_id=ident, amount_microusd=amount, state=state,
                actual_cost_microusd=actual, created_at=self.now if created is None else created,
                settled_at=settled))
            existing = conn.execute(select(spending_origins).where(
                spending_origins.c.reference_type == kind, spending_origins.c.reference_id == ident)).first()
            if owner is not None and actor is not None and not existing:
                conn.execute(insert(spending_origins).values(reference_type=kind, reference_id=ident,
                    tenant_id=self.scope.tenant_id, owner_id=owner, actor_id=actor,
                    created_at=self.now if created is None else created))
        return ident

    def usage(self, actor=None, owner="superdan"):
        with self.repo.engine.connect() as conn:
            return public_usage(conn, self.scope.tenant_id, owner, self.now, actor_id=actor)

    def snapshot(self):
        with self.repo.engine.connect() as conn:
            values = [[dict(row) for row in conn.execute(select(table).order_by(*table.primary_key)).mappings()]
                for table in (budget_accounts, budget_reservations, spending_origins, jobs)]
        return json.dumps(values, sort_keys=True, separators=(",", ":"))

    def test_buckets_deduplicate_account_layers_and_preserve_original_ledger(self):
        start, _ = day_window(self.now)
        settled = self.reservation(state="settled", amount=400, actual=80, created=start-86400, settled=start+10)
        self.reservation(ident=settled, account="shared", state="settled", amount=500, actual=80,
            created=start-86400, settled=start+10)
        pending = self.reservation(kind="instance", amount=100, state="invoice_pending", created=start-1)
        self.reservation(ident=pending, kind="instance", account="shared", amount=120,
            state="invoice_pending", created=start-1)
        self.reservation(amount=200, created=start-86400)
        self.reservation(state="settled", actual=50, created=start-86400)
        self.reservation(state="settled", actual=70, created=start-86400, settled=start-1)
        before = self.snapshot()
        usage = self.usage()
        value = usage["breakdown"]
        self.assertEqual(value["settled_today_microusd"], 80)
        self.assertEqual(value["reserved_microusd"], 200)
        self.assertEqual(value["awaiting_invoice_estimate_microusd"], 120)
        self.assertEqual(value["awaiting_invoice_count"], 1)
        self.assertEqual(value["prior_day_pending_estimate_microusd"], 120)
        self.assertEqual(value["legacy_settled_unknown_date_microusd"], 50)
        self.assertEqual(value["exposure_microusd"], 450)
        self.assertEqual(value["by_kind"]["instance"]["awaiting_invoice_count"], 1)
        self.assertEqual(value["by_kind"]["job"]["exposure_microusd"], 330)
        self.assertEqual(usage["projected_microusd"], 450)
        self.assertEqual(self.snapshot(), before)

    def test_connections_never_display_collapsed_or_other_actor_dollars(self):
        self.reservation(amount=100, actor="key:a")
        self.reservation(amount=200, actor="key:b")
        self.reservation(amount=300, actor="quick-chat-execution")
        self.reservation(amount=400, actor="browser:superdan")
        self.reservation(account="shared", amount=500, owner=None, actor=None)
        account = self.usage()["breakdown"]
        self.assertEqual(account["exposure_microusd"], 1000)
        self.assertEqual(account["unattributed_exposure_microusd"], 500)
        self.assertEqual(account["unattributed_actor_exposure_microusd"], 300)
        for actor, expected, original in (("key:a", 100, 900), ("key:b", 200, 1000)):
            usage = self.usage(actor)
            value = usage["breakdown"]
            self.assertEqual(value["attribution_scope"], "connection")
            self.assertEqual(value["exposure_microusd"], expected)
            self.assertEqual(value["reserved_microusd"], expected)
            self.assertNotIn("unattributed_exposure_microusd", value)
            self.assertNotIn("unattributed_actor_exposure_microusd", value)
            # Display correction cannot silently relax the existing cap policy.
            self.assertEqual(usage["projected_microusd"], original)

    def test_historical_real_job_actor_and_owner_resolve_shared_only_layers(self):
        scope = Scope(self.scope.tenant_id, "superdan", "project-1", "key:historical")
        plan = self.plan(scope=scope, cost=130)
        job = self.repo.create_job(scope, plan["id"], "historical", budget_account_ids=("shared",))
        with self.repo.transaction() as conn:
            conn.execute(delete(spending_origins).where(spending_origins.c.reference_id == job["id"]))
        self.reservation(account="other-owner", owner="supervan", amount=700, actor="key:other")
        self.reservation(account="other-tenant", owner=None, actor=None, amount=900)
        self.assertEqual(self.usage("key:historical")["breakdown"]["exposure_microusd"], 130)
        self.assertEqual(self.usage()["breakdown"]["exposure_microusd"], 130)
        self.assertEqual(self.usage(owner="supervan")["breakdown"]["exposure_microusd"], 700)
        self.assertEqual(self.usage(owner="supervan")["breakdown"]["unattributed_exposure_microusd"], 0)
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(actor_id="quick-chat-execution"))
        self.assertEqual(self.usage("key:historical")["breakdown"]["exposure_microusd"], 0)
        self.assertEqual(self.usage()["breakdown"]["unattributed_actor_exposure_microusd"], 130)

    def test_mixed_states_and_conflicting_actuals_are_unknown_once(self):
        ref = self.reservation(amount=100)
        self.reservation(ident=ref, account="shared", amount=200, state="invoice_pending")
        ref = self.reservation(amount=100, state="settled", actual=50, settled=self.now)
        self.reservation(ident=ref, account="shared", amount=100, state="settled", actual=60, settled=self.now)
        value = self.usage()["breakdown"]
        self.assertEqual(value["reserved_microusd"], 0)
        self.assertEqual(value["awaiting_invoice_count"], 0)
        self.assertEqual(value["settled_today_microusd"], 0)
        self.assertEqual(value["unconfirmed_reference_count"], 2)
        self.assertEqual(value["unconfirmed_exposure_microusd"], 300)
        self.assertEqual(value["exposure_microusd"], 300)
        self.assertEqual(self.usage()["projected_microusd"], 260)

    def test_unknown_state_and_conflicting_ownership_fail_without_writes(self):
        ref = self.reservation(state="unexpected")
        before = self.snapshot()
        with self.assertRaisesRegex(BudgetExceeded, "spending_usage_unconfirmed"):
            self.usage()
        self.assertEqual(self.snapshot(), before)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(budget_reservations.c.reference_id == ref).values(state="reserved"))
        self.reservation(ident=ref, account="other-owner", owner=None, actor=None)
        before = self.snapshot()
        with self.assertRaisesRegex(BudgetExceeded, "spending_usage_unconfirmed"):
            self.usage()
        self.assertEqual(self.snapshot(), before)

    def _sydney_boundary(self, now, hours):
        self.now = now
        start, end = day_window(now)
        self.assertEqual(end-start, hours*3600)
        self.reservation(amount=100, state="settled", actual=10, settled=start, created=start-1)
        self.reservation(amount=100, state="settled", actual=20, settled=start-1, created=start-2)
        self.reservation(amount=100, state="invoice_pending", created=start-1)
        value = self.usage()["breakdown"]
        self.assertEqual(value["settled_today_microusd"], 10)
        self.assertEqual(value["prior_day_pending_estimate_microusd"], 100)
        self.assertEqual(value["exposure_microusd"], 110)
        self.now = end
        value = self.usage()["breakdown"]
        self.assertEqual(value["settled_today_microusd"], 0)
        self.assertEqual(value["awaiting_invoice_estimate_microusd"], 100)

    def test_sydney_spring_boundary_uses_23_hour_day(self):
        self._sydney_boundary(datetime(2026, 10, 4, 12, tzinfo=timezone.utc).timestamp(), 23)

    def test_sydney_autumn_boundary_uses_25_hour_day(self):
        self._sydney_boundary(datetime(2026, 4, 5, 12, tzinfo=timezone.utc).timestamp(), 25)

    def test_reference_kind_is_part_of_identity_and_dates_are_not_invented(self):
        ref = self.reservation(kind="job", amount=400, state="invoice_pending")
        self.reservation(ident=ref, kind="instance", amount=100, state="invoice_pending")
        self.reservation(state="settled", actual=30, settled=self.now+1)
        value = self.usage()["breakdown"]
        self.assertEqual(value["awaiting_invoice_count"], 2)
        self.assertEqual(value["by_kind"]["job"]["awaiting_invoice_estimate_microusd"], 400)
        self.assertEqual(value["by_kind"]["instance"]["awaiting_invoice_estimate_microusd"], 100)
        self.assertEqual(value["unconfirmed_reference_count"], 1)
        with self.repo.engine.connect() as conn:
            original = daily_projection(conn, self.scope.tenant_id, "superdan", self.now)
        self.assertEqual(original, 530)

