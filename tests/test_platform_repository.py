"""Run locally against temporary SQLite, or an explicitly supplied TEST Postgres URL.

PLATFORM_TEST_DATABASE_URL opts in to a local test PostgreSQL server. Each test
creates/drops only its own ledger_test_<uuid> schema. Never use a production URL.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from studio_platform.repository import BudgetExceeded, Conflict, NotFound, Repository, Scope


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-ledger-test-")
        self.now = 1000.0
        self.bootstrap = None
        postgres = os.environ.get("PLATFORM_TEST_DATABASE_URL")
        if postgres:
            parsed = make_url(postgres)
            if parsed.host not in ("127.0.0.1", "localhost") or parsed.database != "sixnine_test":
                raise RuntimeError("test_database_must_be_explicit_local_sixnine_test")
            self.schema = "ledger_test_" + uuid.uuid4().hex
            self.bootstrap = create_engine(postgres, echo=False)
            with self.bootstrap.begin() as connection:
                connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
            self.url = parsed.update_query_dict({"options": "-csearch_path=" + self.schema}).render_as_string(hide_password=False)
        else:
            self.url = "sqlite:///" + str(Path(self.temp.name) / "ledger.sqlite3").replace("\\", "/")
        self.repo = Repository(self.url, clock=lambda: self.now)
        self.repo.create_schema()
        self.repo.configure_capacity(max_instances=10, max_physical_gpus=10)
        self.scope = Scope("test-tenant", "superdan", "project-1")
        self.other = Scope("test-tenant", "supervan", "project-1")
        self.repo.configure_budget("owner-budget", tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id, limit_microusd=10_000_000)

    def tearDown(self):
        self.repo.close()
        if self.bootstrap:
            with self.bootstrap.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
            self.bootstrap.dispose()
        self.temp.cleanup()

    def plan(self, scope=None, *, prompt="test", cost=100_000, expires=None, execution=None):
        return self.repo.create_plan(scope or self.scope, {"prompt": prompt, "shot_version": 7},
            execution or {"pool": "test-pool", "expected_runtime_s": 120, "model_id": "test-model-only"},
            expires_at=self.now + 1000 if expires is None else expires, estimated_cost_microusd=cost)

    def job(self, key=None, *, scope=None, cost=100_000, status="queued"):
        scope = scope or self.scope
        plan = self.plan(scope, cost=cost)
        return self.repo.create_job(scope, plan["id"], key or uuid.uuid4().hex,
            initial_status=status, budget_account_ids=("owner-budget",) if scope == self.scope else ())

    def parallel(self, function, count=8):
        with ThreadPoolExecutor(max_workers=count) as executor:
            return list(executor.map(function, range(count)))


class RepositoryTests(LedgerCase):
    def test_100_large_job_summaries_preserve_public_protocol_without_full_snapshots(self):
        from sqlalchemy import event
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        request = {"client_ref": {"project_id": "project-1", "shot_id": "shot", "shot_version": 1},
            "recipe_id": "test-recipe", "request": {"prompt": "Public prompt remains intact", "duration": 5},
            "simulation": False, "assets": {"private-compilation-context": "x"*(256*1024)}}
        plan = self.repo.create_plan(self.scope, request, {"pool": "test-pool", "backend": "mock", "private": "x"*4096},
            expires_at=self.now+1000, estimated_cost_microusd=0)
        for i in range(100):
            self.repo.create_job(self.scope, plan["id"], "summary-"+str(i))
        app = create_app(Settings(Path(self.temp.name)/"summary-api", tenant_id=self.scope.tenant_id,
            auth_mode="local-test", database_url=self.url), repository=self.repo)
        full_reads = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            sql = context.compiled.statement if context.compiled is not None else None
            if sql is not None and getattr(sql, "is_select", False) and {"request", "pool"} <= set(sql.selected_columns.keys()):
                full_reads.append(statement)
        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            summaries = self.repo.list_jobs_for_owner(self.scope.tenant_id, self.scope.owner_id,
                project_id=self.scope.project_id, limit=100, summary=True)
            visible = [app.state.public_job(row) for row in summaries]
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(len(summaries), 100)
        self.assertEqual(full_reads, [])
        self.assertTrue(all("assets" not in row["request"] and "private" not in row["execution_plan"] for row in summaries))
        for index, row in enumerate(summaries):
            full = self.repo.get_job(self.scope, row["id"])
            self.assertEqual(visible[index], app.state.public_job(full))
        # Complete list reads remain opt-in compatible for existing library users.
        self.assertIn("assets", self.repo.list_jobs(self.scope, limit=1)[0]["request"])

    def test_summary_preserves_missing_and_explicit_null_public_fields(self):
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        app = create_app(Settings(Path(self.temp.name)/"summary-api", tenant_id=self.scope.tenant_id,
            auth_mode="local-test", database_url=self.url), repository=self.repo)
        for i, payload in enumerate(({}, {"client_ref": None, "request": None, "recipe_id": None, "simulation": None},
                                      {"client_ref": {}, "request": {}, "recipe_id": "a", "simulation": True})):
            plan = self.repo.create_plan(self.scope, payload, {"pool": "test-pool"}, expires_at=self.now+1000)
            self.repo.create_job(self.scope, plan["id"], "nullable-"+str(i))
        for summary in self.repo.list_jobs(self.scope, summary=True):
            self.assertEqual(app.state.public_job(summary), app.state.public_job(self.repo.get_job(self.scope, summary["id"])))

    def test_summary_preserves_delivery_spec_without_private_execution_snapshot(self):
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        app = create_app(Settings(Path(self.temp.name)/"delivery-summary", tenant_id=self.scope.tenant_id,
            auth_mode="local-test", database_url=self.url), repository=self.repo)
        delivery = {"policy": "native-frames-v1", "fps": 24, "frame_count": 124,
            "duration_s": 124/24, "requested_duration_s": 5}
        for index, public_fields in enumerate(({}, {"delivery_spec": None}, {"delivery_spec": delivery})):
            execution = {"backend": "mock", "pool": "test-pool", "private": "x"*4096, **public_fields}
            plan = self.repo.create_plan(self.scope, {}, execution, expires_at=self.now+1000)
            self.repo.create_job(self.scope, plan["id"], "delivery-summary-"+str(index))
        for summary in self.repo.list_jobs(self.scope, summary=True):
            full = self.repo.get_job(self.scope, summary["id"])
            self.assertEqual(app.state.public_job(summary), app.state.public_job(full))
            self.assertEqual(set(summary["execution_plan"]),
                {"backend", "delivery_spec"} if "delivery_spec" in full["execution_plan"] else {"backend"})

    def test_batch_artifacts_require_exact_tenant_owner_project_and_skip_snapshots(self):
        from sqlalchemy import event
        from studio_platform.queue import TaskQueue
        job = self.job(cost=0)
        queue = TaskQueue(self.repo)
        claim = queue.claim("test-worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "fake-task")
        queue.begin_collection(claim.lease)
        queue.complete(claim.lease, [{"kind": "video", "object_key": "owners/superdan/assets/test/result.mp4",
            "size_bytes": 1, "sha256": "a"*64, "validated": True}], actual_cost_microusd=0)
        reads = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            sql = context.compiled.statement if context.compiled is not None else None
            if sql is not None and getattr(sql, "is_select", False) and "request" in sql.selected_columns.keys():
                reads.append(statement)
        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            single = self.repo.list_artifacts(self.scope, job["id"])
            batch = self.repo.list_artifacts_for_jobs(self.scope.tenant_id, self.scope.owner_id, {job["id"]: "project-1"})
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(reads, [])
        self.assertEqual(batch[job["id"]], single)
        for tenant, owner, mapping in (("other", "superdan", {job["id"]: "project-1"}),
                                       ("test-tenant", "supervan", {job["id"]: "project-1"}),
                                       ("test-tenant", "superdan", {job["id"]: "other-project"}),
                                       ("test-tenant", "superdan", {"not-a-job": "project-1"})):
            with self.assertRaises(NotFound):
                self.repo.list_artifacts_for_jobs(tenant, owner, mapping)
        self.assertEqual(self.repo.list_artifacts_for_jobs("test-tenant", "superdan", {}), {})
        with self.assertRaises(ValueError):
            self.repo.list_artifacts_for_jobs("test-tenant", "superdan", {str(i): "p" for i in range(101)})

    def test_additive_provider_migration_preserves_rows_budgets_and_unknown_capacity(self):
        from studio_platform.repository import instance_intents
        self.repo.configure_pool("legacy-test", max_instances=1, max_physical_gpus=2)
        intent = self.repo.reserve_instance_intent(self.scope, "legacy-test", "legacy-create",
            physical_gpus=2, hard_deadline=2000, reserved_cost_microusd=100_000,
            budget_account_ids=["owner-budget"], dry_run=False)
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(intent["id"], "creation_unknown")
        # Only this test's newly created temporary DB/schema is converted to the
        # previous schema shape. Production migration itself never drops columns.
        with self.repo.transaction() as connection:
            connection.exec_driver_sql("ALTER TABLE platform_instance_intents DROP COLUMN provider")
        self.repo.create_schema()
        self.repo.create_schema()  # The additive migration is idempotent.
        with self.repo.engine.connect() as connection:
            row = dict(connection.execute(instance_intents.select()).mappings().one())
        self.assertEqual(row["id"], intent["id"])
        self.assertEqual(row["provider"], "unknown")
        self.assertEqual((row["state"], row["physical_gpus"], row["reserved_cost_microusd"]), ("creation_unknown", 2, 100_000))
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=2)
        self.repo.configure_pool("new-test", max_instances=1, max_physical_gpus=1)
        with self.assertRaises(BudgetExceeded):
            self.repo.reserve_instance_intent(self.scope, "new-test", "cannot-use-unknown-capacity",
                physical_gpus=1, hard_deadline=2000, reserved_cost_microusd=100_000,
                budget_account_ids=["owner-budget"], dry_run=False)

    def test_document_compare_and_swap_and_owner_isolation(self):
        doc = self.repo.put_document(self.scope, "project", "project-1", {"name": "one"}, expected_version=0)
        self.assertEqual(doc["version"], 1)
        with self.assertRaises(Conflict):
            self.repo.put_document(self.scope, "project", "project-1", {"name": "overwrite"})
        updated = self.repo.put_document(self.scope, "project", "project-1", {"name": "two"}, expected_version=1)
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(NotFound):
            self.repo.get_document(self.other, "project", "project-1")
        self.assertEqual(self.repo.list_documents(self.other, "project"), [])
        for scope in (Scope("other", "superdan", "project-1"), Scope("test-tenant", "superdan", "project-2")):
            with self.assertRaises(NotFound):
                self.repo.get_document(scope, "project", "project-1")

    def test_document_concurrent_updates_only_one_wins(self):
        self.repo.put_document(self.scope, "project", "p", {}, expected_version=0)
        def change(i):
            try:
                return self.repo.put_document(self.scope, "project", "p", {"winner": i}, expected_version=1)
            except Conflict:
                return None
        self.assertEqual(sum(r is not None for r in self.parallel(change)), 1)
        self.assertEqual(self.repo.get_document(self.scope, "project", "p")["version"], 2)

    def test_plan_and_job_snapshots_immutable(self):
        request = {"prompt": "one", "inputs": ["asset-1"]}
        execution = {"pool": "test-pool", "model_id": "original", "settings": {"steps": 20}}
        plan = self.repo.create_plan(self.scope, request, execution, expires_at=self.now + 100)
        request["inputs"].append("not-in-plan")
        execution["settings"]["steps"] = 1
        plan["execution_plan"]["model_id"] = "mutated-return-value"
        stored = self.repo.get_plan(self.scope, plan["id"])
        self.assertEqual(stored["execution_plan"]["model_id"], "original")
        self.assertEqual(stored["execution_plan"]["settings"]["steps"], 20)
        self.assertEqual(stored["request"]["inputs"], ["asset-1"])
        job = self.repo.create_job(self.scope, plan["id"], "snapshot")
        self.assertEqual(job["execution_plan"], stored["execution_plan"])
        with self.assertRaises(NotFound):
            self.repo.get_plan(self.other, plan["id"])

    def test_idempotency_reuse_conflict_and_expiry_recovery(self):
        plan = self.plan(expires=self.now + 1)
        first = self.repo.create_job(self.scope, plan["id"], "same", budget_account_ids=["owner-budget"])
        self.assertEqual(self.repo.lookup_job_by_idempotency(self.scope, "same")["id"], first["id"])
        self.assertIsNone(self.repo.lookup_job_by_idempotency(self.other, "same"))
        self.assertIsNone(self.repo.lookup_job_by_idempotency(self.scope, "missing"))
        self.now += 2
        again = self.repo.create_job(self.scope, plan["id"], "same", budget_account_ids=["owner-budget"])
        self.assertEqual(first["id"], again["id"])
        self.assertFalse(again["created"])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)
        with self.assertRaises(Conflict):
            self.repo.create_job(self.scope, self.plan(prompt="different")["id"], "same")
        with self.assertRaises(Conflict):
            self.repo.create_job(self.scope, plan["id"], "new-key")

    def test_idempotency_race_across_repository_connections(self):
        plan = self.plan()
        def create(_):
            repo = Repository(self.url, clock=lambda: self.now)
            try:
                return repo.create_job(self.scope, plan["id"], "raced", budget_account_ids=["owner-budget"])
            finally:
                repo.close()
        results = self.parallel(create, 12)
        self.assertEqual(len({r["id"] for r in results}), 1)
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)

    def test_idempotency_namespaced_by_actor_owner_project(self):
        scopes = [self.scope, self.other, Scope("test-tenant", "superdan", "project-2"),
                  Scope("test-tenant", "superdan", "project-1", "service-client-test")]
        created = [self.repo.create_job(scope, self.plan(scope, cost=0)["id"], "same") for scope in scopes]
        self.assertEqual(len({r["id"] for r in created}), 4)
        with self.assertRaises(NotFound):
            self.repo.get_job(self.other, created[0]["id"])
        with self.assertRaises(NotFound):
            self.repo.get_job_for_owner("test-tenant", "supervan", created[0]["id"])
        self.assertEqual(len(self.repo.list_jobs_for_owner("test-tenant", "superdan")), 3)
        self.assertEqual(len(self.repo.list_jobs(self.scope)), 2)

    def test_changed_quote_conflicts_with_original_confirmation(self):
        first = self.plan(cost=100_000)
        second = self.plan(cost=150_000)
        self.repo.create_job(self.scope, first["id"], "confirmed", budget_account_ids=["owner-budget"])
        with self.assertRaises(Conflict):
            self.repo.create_job(self.scope, second["id"], "confirmed", budget_account_ids=["owner-budget"])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)

    def test_nonfinite_times_and_unbudgeted_instance_rejected(self):
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.plan(expires=value)
            with self.assertRaises(ValueError):
                self.repo.reserve_instance_intent(self.scope, "gpu", "bad", hard_deadline=value)
        self.repo.configure_pool("gpu", max_instances=1, max_physical_gpus=1)
        with self.assertRaises(BudgetExceeded):
            self.repo.reserve_instance_intent(self.scope, "gpu", "free-assumption", hard_deadline=2000, dry_run=False)


    def test_budget_race_never_overcommits(self):
        self.repo.configure_budget("owner-budget", tenant_id="test-tenant", owner_id="superdan", limit_microusd=200_000)
        plans = [self.plan() for _ in range(12)]
        def create(i):
            try:
                return self.repo.create_job(self.scope, plans[i]["id"], str(i), budget_account_ids=["owner-budget"])
            except BudgetExceeded:
                return None
        result = self.parallel(create, 12)
        self.assertEqual(sum(r is not None for r in result), 2)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 200_000)
        self.assertEqual(len(self.repo.list_jobs(self.scope)), 2)

    def test_multiple_budget_reservations_roll_back_together(self):
        self.repo.configure_budget("global", tenant_id="test-tenant", limit_microusd=50_000)
        with self.assertRaises(BudgetExceeded):
            self.repo.create_job(self.scope, self.plan()["id"], "no", budget_account_ids=["owner-budget", "global"])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        self.assertEqual(self.repo.list_jobs(self.scope), [])
        with self.assertRaises(NotFound):
            self.repo.create_job(self.other, self.plan(self.other)["id"], "cross", budget_account_ids=["owner-budget"])
        with self.assertRaises(BudgetExceeded):
            self.repo.create_job(self.scope, self.plan()["id"], "no-account")

    def test_planned_admission_reserves_once_and_rechecks_expiry(self):
        job = self.job(status="planned")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        self.repo.enqueue(self.scope, job["id"], budget_account_ids=["owner-budget"])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)
        with self.assertRaises(Conflict):
            self.repo.enqueue(self.scope, job["id"], budget_account_ids=["owner-budget"])
        expired = self.job(status="blocked")
        self.now += 2000
        with self.assertRaises(Conflict):
            self.repo.enqueue(self.scope, expired["id"], budget_account_ids=["owner-budget"])

    def test_outbox_and_jobs_survive_process_reopen(self):
        job = self.job()
        reopened = Repository(self.url, clock=lambda: self.now)
        try:
            self.assertEqual(reopened.get_job(self.scope, job["id"])["status"], "queued")
            event = reopened.pending_events()[0]
            reopened.acknowledge_event(event["id"])
            self.assertEqual(reopened.pending_events(), [])
        finally:
            reopened.close()

    def test_instance_defaults_dry_run_and_zero_capacity(self):
        response = self.repo.reserve_instance_intent(self.scope, "gpu", "test", hard_deadline=2000)
        self.assertTrue(response["dry_run"])
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.repo.configure_pool("gpu")
        with self.assertRaises(BudgetExceeded):
            self.repo.reserve_instance_intent(self.scope, "gpu", "test", hard_deadline=2000, dry_run=False)

    def test_instance_creation_unknown_retains_budget_capacity_after_deadline(self):
        self.repo.configure_pool("gpu", max_instances=1, max_physical_gpus=1)
        kwargs = dict(hard_deadline=1100, reserved_cost_microusd=500_000,
                      budget_account_ids=["owner-budget"], dry_run=False)
        instance = self.repo.reserve_instance_intent(self.scope, "gpu", "one", **kwargs)
        again = self.repo.reserve_instance_intent(self.scope, "gpu", "one", **kwargs)
        self.assertEqual(instance["id"], again["id"])
        self.assertFalse(again["created"])
        self.repo.update_instance(instance["id"], "creating")
        self.repo.update_instance(instance["id"], "creation_unknown")
        self.now = 1200
        with self.assertRaises(BudgetExceeded):
            self.repo.reserve_instance_intent(self.scope, "gpu", "two", hard_deadline=1500, dry_run=False)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 500_000)
        with self.assertRaises(Conflict):
            self.repo.update_instance(instance["id"], "destroyed")
        self.repo.update_instance(instance["id"], "destroyed", destruction_confirmed=True, actual_cost_microusd=75_000)
        budget = self.repo.get_budget("owner-budget")
        self.assertEqual(budget["reserved_microusd"], 0)
        self.assertEqual(budget["spent_microusd"], 75_000)

    def test_instance_concurrent_unique_intent_and_capacity(self):
        self.repo.configure_pool("gpu", max_instances=1, max_physical_gpus=1)
        def reserve(i):
            try:
                return self.repo.reserve_instance_intent(self.scope, "gpu", str(i), hard_deadline=2000,
                    reserved_cost_microusd=500_000, budget_account_ids=["owner-budget"], dry_run=False)
            except BudgetExceeded:
                return None
        result = self.parallel(reserve)
        self.assertEqual(sum(r is not None for r in result), 1)
        self.assertEqual(len(self.repo.list_instance_intents()), 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 500_000)


if __name__ == "__main__":
    unittest.main()
