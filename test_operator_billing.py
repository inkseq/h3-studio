"""Disposable original-ledger tests: no providers, real invoices or user writes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import uuid

from sqlalchemy import insert, select, update

from studio_platform.auth import Principal
from studio_platform.operator_billing import BillingRefused, OperatorBilling
from studio_platform.operator_capacity import OperatorError
from studio_platform.repository import (NotFound, Scope, attempts, billing_adjustments,
    budget_accounts, budget_reservations, documents, instance_intents, jobs, outbox,
    registered_workers, spending_origins)
from studio_platform.settlement_recovery import release_pending_invoice
from studio_platform.spending_limits import public_usage
from test_platform_repository import LedgerCase


class OperatorBillingTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.admin = Principal("superdan", "browser:superdan")
        self.owner = Scope(self.scope.tenant_id, "supervan", "project-1", "key:van")
        self.settings = SimpleNamespace(tenant_id=self.scope.tenant_id, operator_admin_owners=("superdan",))
        self.service = OperatorBilling(self.repo, self.settings)
        self.repo.configure_budget("van", tenant_id=self.owner.tenant_id, owner_id="supervan", limit_microusd=100_000_000)
        self.repo.configure_budget("shared", tenant_id=self.owner.tenant_id, limit_microusd=100_000_000)
        self.repo.configure_pool("billing-test", max_instances=10, max_physical_gpus=10)

    def rows(self, table):
        with self.repo.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(table)).mappings()]

    def instance(self, *, scope=None, provider_id=None, estimate=10000):
        scope = scope or self.owner
        ident = self.repo.reserve_instance_intent(scope, "billing-test", uuid.uuid4().hex,
            reserved_cost_microusd=estimate, hard_deadline=self.now+7200,
            budget_account_ids=("van", "shared") if scope == self.owner else ("owner-budget",),
            provider="vast", dry_run=False)["id"]
        worker = "worker-"+uuid.uuid4().hex
        provider_id = provider_id or uuid.uuid4().hex
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == ident).values(
                state="destroyed", provider_instance_id=provider_id))
            release_pending_invoice(self.repo, conn, "instance", ident)
            conn.execute(insert(registered_workers).values(id=worker, pool="billing-test", provider="vast",
                instance_id=provider_id, spec={}, spec_hash="a"*64, state="stopped", current_job_id=None,
                fence=1, expires_at=self.now, updated_at=self.now))
        return ident, worker

    def completed_job(self, worker, *, scope=None, submitted=True, retry=False):
        scope = scope or self.owner
        plan = self.repo.create_plan(scope, {}, {"pool": "billing-test"},
            expires_at=self.now+1000, estimated_cost_microusd=1000)
        job = self.repo.create_job(scope, plan["id"], uuid.uuid4().hex,
            budget_account_ids=("van", "shared") if scope == self.owner else ("owner-budget",))
        with self.repo.transaction() as conn:
            if retry:
                conn.execute(insert(attempts).values(id=str(uuid.uuid4()), job_id=job["id"], number=1,
                    status="deferred", fence=1, worker_id=worker, created_at=self.now, updated_at=self.now))
            attempt = str(uuid.uuid4())
            conn.execute(insert(attempts).values(id=attempt, job_id=job["id"], number=2 if retry else 1,
                status="succeeded", fence=1, worker_id=worker, created_at=self.now, updated_at=self.now,
                submission_started_at=self.now if submitted else None,
                upstream_task_id="original-"+job["id"] if submitted else None, upstream_stopped=1))
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="succeeded",
                current_attempt_id=attempt, result={"billing_status": "pending"}, lease_worker_id=None, lease_expires_at=None))
            release_pending_invoice(self.repo, conn, "job", job["id"])
        return job["id"]

    def views(self):
        return {(r["reference_type"], r["reference_id"]): r
            for r in self.service.references(self.admin, "supervan", limit=100)["items"]}

    def request(self, action, refs):
        rows = self.views()
        return {"owner_id": "supervan", "action": action, "reason": "Owner reviewed prelaunch accounting",
            "evidence_sha256": "b"*64, "evidence_note": "Observed provider charges; internal rental allocation, not a per-job invoice",
            "items": [{"reference_type": kind, "reference_id": ident,
                "expected_snapshot": rows[(kind, ident)]["snapshot"], **extra} for kind, ident, extra in refs]}

    def execute(self, request, key=None):
        preview = self.service.preview(self.admin, request)
        body = {"preview_id": preview["preview_id"], "expected_snapshot": preview["snapshot"], "confirmed": True}
        return preview, body, self.service.apply(self.admin, body, key or uuid.uuid4().hex)

    def usage(self, actor=None):
        with self.repo.engine.connect() as conn:
            return public_usage(conn, self.owner.tenant_id, self.owner.owner_id, self.now, actor_id=actor)

    def test_actual_allocation_waiver_preserves_gross_and_originals(self):
        instance, worker = self.instance()
        first, second = self.completed_job(worker, retry=True), self.completed_job(worker)
        body = self.request("settle", [("instance", instance, {"actual_cost_microusd": 1000}),
            ("job", first, {"actual_cost_microusd": 300, "allocation_instance_id": instance}),
            ("job", second, {"actual_cost_microusd": 700, "allocation_instance_id": instance})])
        preview, apply, result = self.execute(body, "original")
        self.assertEqual(result["request_hash"], preview["request_hash"])
        self.assertEqual(result["totals"]["instance_actual_microusd"], 1000)
        self.assertEqual(result["totals"]["job_allocated_microusd"], 1000)
        rows = self.views()
        self.assertEqual(rows[("instance", instance)]["provider_actual_cost_microusd"], 1000)
        self.assertEqual(rows[("job", first)]["allocated_cost_microusd"], 300)
        self.assertEqual(rows[("job", first)]["settlement_basis"], "rental_allocation")
        self.assertEqual(self.repo.get_job_for_owner(self.owner.tenant_id, "supervan", first)["result"]["rental_allocation"]["provider_per_job_invoice"], False)
        self.assertEqual(self.repo.get_budget("van")["spent_microusd"], 2000)  # distinct nonadditive rental/job layers
        before = {t.name: self.rows(t) for t in (jobs, attempts, instance_intents, budget_accounts, budget_reservations, spending_origins)}
        _, _, waived = self.execute(self.request("waive", [(k, i, {}) for k, i in rows]))
        self.assertEqual(waived["totals"]["waived_reference_count"], 3)
        self.assertTrue(all(r["waived"] and r["user_charge_microusd"] == 0 for r in waived["items"]))
        for table in (jobs, attempts, instance_intents, budget_accounts, budget_reservations, spending_origins):
            self.assertEqual(self.rows(table), before[table.name])
        usage = self.usage()
        self.assertEqual(usage["projected_microusd"], 2000)
        self.assertEqual(usage["breakdown"]["exposure_microusd"], 2000)
        self.assertEqual(usage["charge_breakdown"]["exposure_microusd"], 0)
        self.assertEqual(usage["charge_breakdown"]["waived_exposure_microusd"], 2000)
        self.assertEqual(usage["charge_breakdown"]["waived_reference_count"], 3)
        self.assertEqual(self.service.by_key(self.admin, "original"), result)
        self.assertEqual(self.service.action(self.admin, result["action_id"]), result)
        audit = [r for r in self.rows(outbox) if r["event_type"] == "operator.billing_applied"]
        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[0]["payload"]["request"], body)
        self.assertEqual(len(audit[0]["payload"]["before"]), 3)

    def test_full_reference_waiver_covers_later_actual_without_forgiving_admission(self):
        instance, _ = self.instance(estimate=1000)
        before = self.rows(budget_reservations)
        self.execute(self.request("waive", [("instance", instance, {})]))
        self.assertEqual(self.rows(budget_reservations), before)
        self.assertIsNone(self.views()[("instance", instance)]["provider_actual_cost_microusd"])
        self.assertEqual(self.usage()["projected_microusd"], 1000)
        self.repo.settle_instance_cost(instance, actual_cost_microusd=900)
        self.assertEqual(self.views()[("instance", instance)]["provider_actual_cost_microusd"], 900)
        self.assertEqual(self.usage()["charge_breakdown"]["exposure_microusd"], 0)
        self.assertEqual(self.usage()["projected_microusd"], 900)
        self.assertEqual(self.rows(billing_adjustments)[0]["basis_exposure_microusd"], 1000)

    def test_idempotency_snapshot_expiry_and_unknown_write_rollback(self):
        instance, _ = self.instance()
        preview = self.service.preview(self.admin, self.request("settle", [("instance", instance, {"actual_cost_microusd": 500})]))
        body = {"preview_id": preview["preview_id"], "expected_snapshot": preview["snapshot"], "confirmed": True}
        before = {t.name: self.rows(t) for t in (budget_accounts, budget_reservations, instance_intents, outbox)}
        from studio_platform import operator_billing
        original_save = operator_billing._save
        def fail_last(conn, tenant, owner, kind, ident, value, now):
            if kind == "billing_action_key": raise RuntimeError("simulated_receipt_failure")
            return original_save(conn, tenant, owner, kind, ident, value, now)
        with patch.object(operator_billing, "_save", fail_last), self.assertRaisesRegex(RuntimeError, "simulated_receipt_failure"):
            self.service.apply(self.admin, body, "original-key")
        for table in (budget_accounts, budget_reservations, instance_intents, outbox):
            self.assertEqual(self.rows(table), before[table.name])
        with self.assertRaisesRegex(BillingRefused, "billing_action_not_found"):
            self.service.by_key(self.admin, "original-key")
        result = self.service.apply(self.admin, body, "original-key")
        self.now += 1000
        self.assertEqual(self.service.apply(self.admin, body, "original-key"), result)
        changed = {**body, "expected_snapshot": "a"*64}
        with self.assertRaisesRegex(BillingRefused, "billing_idempotency_conflict"):
            self.service.apply(self.admin, changed, "original-key")
        other, _ = self.instance()
        preview = self.service.preview(self.admin, self.request("waive", [("instance", other, {})]))
        body = {"preview_id": preview["preview_id"], "expected_snapshot": preview["snapshot"], "confirmed": True}
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).where(budget_accounts.c.id == "van").values(limit_microusd=99999999))
        with self.assertRaisesRegex(BillingRefused, "billing_snapshot_changed"):
            self.service.apply(self.admin, body, "stale")
        preview = self.service.preview(self.admin, self.request("waive", [("instance", other, {})]))
        self.now = preview["expires_at"]
        with self.assertRaisesRegex(BillingRefused, "billing_preview_expired"):
            self.service.apply(self.admin, {**body, "preview_id": preview["preview_id"], "expected_snapshot": preview["snapshot"]}, "expired")
        self.assertEqual(self.rows(billing_adjustments), [])

    def test_allocation_requires_complete_exact_closed_lineage_and_conservation(self):
        instance, worker = self.instance()
        a, b = self.completed_job(worker), self.completed_job(worker)
        instance_item = ("instance", instance, {"actual_cost_microusd": 1000})
        job = lambda ident, amount: ("job", ident, {"actual_cost_microusd": amount, "allocation_instance_id": instance})
        for refs, error in (([instance_item, job(a, 1000)], "billing_allocation_incomplete"),
                            ([instance_item, job(a, 300), job(b, 600)], "billing_allocation_total_mismatch")):
            with self.subTest(error=error), self.assertRaisesRegex(BillingRefused, error):
                self.service.preview(self.admin, self.request("settle", refs))
        outsider = self.completed_job(worker, scope=self.scope)
        with self.assertRaisesRegex(BillingRefused, "billing_allocation_unconfirmed"):
            self.service.preview(self.admin, self.request("settle", [instance_item, job(a, 300), job(b, 700)]))
        self.assertEqual(self.rows(billing_adjustments), [])
        self.assertTrue(all(r["actual_cost_microusd"] is None for r in self.rows(budget_reservations)))

    def test_terminal_pending_corruption_and_busy_worker_fail_closed(self):
        instance, worker = self.instance()
        job = self.completed_job(worker)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(budget_reservations.c.reference_id == job).values(actual_cost_microusd=99))
        row = self.views()[("job", job)]
        self.assertEqual(row["block_reason"], "billing_ledger_unconfirmed")
        self.assertEqual(row["eligible_actions"], [])
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(budget_reservations.c.reference_id == job).values(actual_cost_microusd=None))
            conn.execute(update(registered_workers).where(registered_workers.c.id == worker).values(current_job_id="unrelated-job"))
        self.assertIn("waive", self.views()[("job", job)]["eligible_actions"])
        self.assertEqual(self.views()[("instance", instance)]["block_reason"], "billing_upstream_unconfirmed")
        with self.repo.transaction() as conn:
            conn.execute(update(attempts).where(attempts.c.job_id == job).values(upstream_stopped=0))
        self.assertEqual(self.views()[("job", job)]["block_reason"], "billing_upstream_unconfirmed")

    def test_inconsistent_settled_job_result_cannot_skip_original_ledger_settlement(self):
        instance, worker = self.instance()
        job = self.completed_job(worker)
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job).values(
                result={"billing_status": "settled", "actual_cost_microusd": 1000}))
        view = self.views()[("job", job)]
        self.assertEqual(view["eligible_actions"], [])
        self.assertEqual(view["block_reason"], "billing_ledger_unconfirmed")
        with self.assertRaisesRegex(BillingRefused, "billing_ledger_unconfirmed"):
            self.service.preview(self.admin, self.request("settle", [
                ("instance", instance, {"actual_cost_microusd": 1000}),
                ("job", job, {"actual_cost_microusd": 1000, "allocation_instance_id": instance})]))
        self.assertTrue(all(row["actual_cost_microusd"] is None for row in self.rows(budget_reservations)))

    def test_authority_actor_tenant_and_connection_display_are_isolated(self):
        instance, worker = self.instance()
        self.completed_job(worker)
        body = self.request("waive", [("instance", instance, {})])
        for actor in (None, Principal("supervan", "browser:van"), Principal("superdan", "key:admin", machine=True)):
            for call in (lambda: self.service.references(actor, "supervan"), lambda: self.service.preview(actor, body),
                         lambda: self.service.by_key(actor, "key")):
                with self.assertRaises(OperatorError): call()
        foreign = OperatorBilling(self.repo, SimpleNamespace(tenant_id="foreign", operator_admin_owners=("superdan",)))
        self.assertEqual(foreign.references(self.admin, "supervan")["items"], [])
        with self.assertRaises((NotFound, BillingRefused)): foreign.preview(self.admin, body)
        with self.assertRaises(NotFound): self.service.preview(self.admin, {**body, "owner_id": "superdan"})
        preview, apply, result = self.execute(body, "actor-bound")
        second_admin = Principal("superdan", "browser:second")
        with self.assertRaisesRegex(BillingRefused, "billing_action_not_found"):
            self.service.by_key(second_admin, "actor-bound")
        with self.assertRaises(NotFound): self.service.action(second_admin, result["action_id"])
        self.assertEqual(self.usage("key:other")["charge_breakdown"]["waived_reference_count"], 0)
        self.assertEqual(self.usage("key:other")["charge_breakdown"]["waived_exposure_microusd"], 0)
        self.assertEqual(self.usage("key:van")["charge_breakdown"]["waived_reference_count"], 1)

    def test_97_reference_batch_and_stable_tied_date_pagination(self):
        one, worker1 = self.instance()
        two, worker2 = self.instance()
        refs = [("instance", one, {"actual_cost_microusd": 670}), ("instance", two, {"actual_cost_microusd": 280})]
        for index in range(95):
            instance, worker = (one, worker1) if index < 67 else (two, worker2)
            refs.append(("job", self.completed_job(worker, retry=index == 0), {"actual_cost_microusd": 10, "allocation_instance_id": instance}))
        seen, cursor = [], None
        while True:
            page = self.service.references(self.admin, "supervan", limit=17, after=cursor,
                created_after=self.now, created_before=self.now+1)
            seen.extend((r["reference_type"], r["reference_id"]) for r in page["items"])
            cursor = page["next_cursor"]
            if not cursor: break
        self.assertEqual(len(seen), 97); self.assertEqual(len(set(seen)), 97)
        self.assertEqual(self.service.references(self.admin, "supervan", created_before=self.now)["items"], [])
        _, _, settled = self.execute(self.request("settle", refs))
        self.assertEqual(settled["totals"]["reference_count"], 97)
        _, _, waived = self.execute(self.request("waive", [(k, i, {}) for k, i, _ in refs]))
        self.assertEqual(len(waived["items"]), 97)
        self.assertEqual(self.repo.get_budget("van")["spent_microusd"], 1900)
        self.assertEqual(self.usage()["charge_breakdown"]["exposure_microusd"], 0)

    def test_strict_requests_and_finite_clock_never_write_on_refusal(self):
        instance, _ = self.instance()
        body = self.request("waive", [("instance", instance, {})])
        invalid = []
        for field, value in (("reason", ""), ("reason", "line\nbreak"), ("evidence_note", "x\x00y"),
                             ("evidence_sha256", "A"*64), ("items", body["items"]*201)):
            invalid.append({**body, field: value})
        invalid.extend([{**body, "items": body["items"]*2}, {**body, "unexpected": True},
            {**body, "action": "settle", "items": [{**body["items"][0], "actual_cost_microusd": True}]}])
        before = self.rows(documents)
        for request in invalid:
            with self.assertRaises(ValueError): self.service.preview(self.admin, request)
        self.now = float("nan")
        with self.assertRaisesRegex(BillingRefused, "billing_clock_unconfirmed"):
            self.service.preview(self.admin, body)
        self.assertEqual(self.rows(documents), before)
        for cursor in ("invalid", "=", "[]", "a"*513):
            with self.assertRaisesRegex(BillingRefused, "billing_cursor_invalid"):
                self.service.references(self.admin, "supervan", after=cursor)

    def test_parallel_same_key_commits_one_immutable_adjustment(self):
        instance, _ = self.instance()
        preview = self.service.preview(self.admin, self.request("waive", [("instance", instance, {})]))
        body = {"preview_id": preview["preview_id"], "expected_snapshot": preview["snapshot"], "confirmed": True}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.apply(self.admin, body, "one-original"), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.rows(billing_adjustments)), 1)
        self.assertEqual(len([r for r in self.rows(outbox) if r["event_type"] == "operator.billing_applied"]), 1)

    def test_existing_rental_actual_is_reused_and_other_machine_lineage_refused(self):
        instance, worker = self.instance()
        a, b = self.completed_job(worker), self.completed_job(worker)
        self.repo.settle_instance_cost(instance, actual_cost_microusd=1000)
        refs = [("job", a, {"actual_cost_microusd": 300, "allocation_instance_id": instance}),
                ("job", b, {"actual_cost_microusd": 700, "allocation_instance_id": instance})]
        other, worker2 = self.instance()
        with self.repo.transaction() as conn:
            conn.execute(insert(attempts).values(id=str(uuid.uuid4()), job_id=a, number=2,
                status="succeeded", fence=1, worker_id=worker2, created_at=self.now, updated_at=self.now,
                submission_started_at=self.now, upstream_task_id="another-original", upstream_stopped=1))
        with self.assertRaisesRegex(BillingRefused, "billing_allocation_unconfirmed"):
            self.service.preview(self.admin, self.request("settle", refs))
        self.assertIsNone(self.views()[("job", a)]["allocated_cost_microusd"])
        self.assertEqual(self.views()[("instance", instance)]["provider_actual_cost_microusd"], 1000)

    def test_http_admin_scoping_exact_absent_and_stale_receipt(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        app = create_app(Settings(Path(self.temp.name)/"api", tenant_id=self.owner.tenant_id,
            auth_mode="local-test", database_url=self.url, operator_admin_owners=("superdan",),
            operator_capacity_owners=("superdan", "supervan")), repository=self.repo)
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/operator/admin/billing/references", params={"owner_id": "supervan"}).status_code, 401)
            self.assertEqual(client.post("/api/auth/login", json={"username": "superdan", "password": "unused-local-test"}).status_code, 200)
            response = client.get("/v1/operator/admin/billing/actions/by-key", params={"key": "never-sent"})
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json(), {"code": "billing_action_not_found", "no_mutation_committed": True})
            self.assertEqual(response.headers["cache-control"], "no-store")
            instance, _ = self.instance()
            preview = client.post("/v1/operator/admin/billing/previews", json=self.request("waive", [("instance", instance, {})])).json()
            self.now = preview["expires_at"]
            expired = client.post("/v1/operator/admin/billing/actions", headers={"Idempotency-Key": "original"},
                json={"preview_id": preview["preview_id"], "expected_snapshot": preview["snapshot"], "confirmed": True})
            self.assertEqual(expired.status_code, 409, expired.text)
            self.assertEqual(expired.json(), {"code": "billing_preview_expired", "no_mutation_committed": True})
            client.post("/api/auth/login", json={"username": "supervan", "password": "unused-local-test"})
            self.assertEqual(client.get("/v1/operator/admin/billing/references", params={"owner_id": "supervan"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
