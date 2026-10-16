"""Opt-in real local PG snapshot + Local media → isolated portable recovery.

Uses only LedgerCase's explicitly supplied local sixnine_test database and its
new unique schema. Never creates a role/database or uses production credentials.
"""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from sqlalchemy import event, insert, select, update, null, JSON

from studio_platform.assets import AssetService, AssetNotFound
from studio_platform.auth import Auth, accounts, sessions, clients
from studio_platform.backup import backup_postgres_engine, backup_postgres_local, verify_local, restore_local, BackupError
from studio_platform.queue import TaskQueue
from studio_platform.repository import Repository, Scope, capacity_approvals, capacity_waiters, capacity_cycles, budget_accounts, scaler_actions
from studio_platform.storage import LocalObjectStore
from studio_platform.project_activity import activity, metadata as activity_metadata, append_activity
from studio_platform.auth import Principal
from test_platform_assets import png
from test_platform_repository import LedgerCase


@unittest.skipUnless(os.environ.get("PLATFORM_TEST_DATABASE_URL"), "Explicit isolated local PG URL required")
class PostgresPortableBackupTests(LedgerCase):
    def setUp(self):
        super().setUp()
        # Also clean up if this subclass fixture construction raises; unittest
        # does not run tearDown after a failed setUp.
        self.addCleanup(super().tearDown)
        self.root = Path(self.temp.name)
        self.store = LocalObjectStore(self.root/"source-objects")
        self.assets = AssetService(self.repo.engine, self.store, self.root, tenant=self.scope.tenant_id, max_bytes=1024*1024)
        self.asset = self.assets.upload("superdan", "project-1", io.BytesIO(png()), "original.png")
        self.repo.put_document(self.scope, "project", "project-1", {"title": "Snapshot original", "asset_id": self.asset["id"]})
        self.job_row = self.job("snapshot-unknown-job", cost=100)
        queue = TaskQueue(self.repo)
        lease = queue.claim("lost-backup-worker", "test-pool").lease
        queue.begin_submission(lease)
        queue.record_submitted(lease, "fake-upstream-needs-review")
        self.repo.configure_pool("test-pool", max_instances=2, max_physical_gpus=2)
        self.intent = self.repo.reserve_instance_intent(self.scope, "test-pool", "fake-portable-backup-intent",
            physical_gpus=1, reserved_cost_microusd=23, hard_deadline=9000,
            budget_account_ids=["owner-budget"], dry_run=False)
        self.repo.update_instance(self.intent["id"], "creating")
        self.repo.update_instance(self.intent["id"], "creation_unknown")
        Auth(self.repo.engine, tenant=self.scope.tenant_id)
        activity_metadata.create_all(self.repo.engine)
        self.canary = "synthetic-auth-bytes-never-export-to-backup"
        with self.repo.engine.begin() as conn:
            append_activity(conn,tenant_id=self.scope.tenant_id,principal=Principal("superdan","superdan"),
                project_id="project-1",version=1,occurred_at=1234.5,before={},
                after={"entities":[{"id":"shot-one"}]},actions=[{"op":"entity.update","entity_id":"shot-one"}])
            conn.execute(insert(accounts).values(tenant=self.scope.tenant_id, username="superdan", password_hash=self.canary, disabled=0, updated=1))
            conn.execute(insert(sessions).values(tenant=self.scope.tenant_id, token_hash=self.canary, username="superdan",
                auth_mode="password", created=1, expires=9000, password_version=1))
            conn.execute(insert(clients).values(tenant=self.scope.tenant_id, id="fake-client", token_hash=self.canary,
                owner="superdan", projects='["project-1"]', scopes='["read"]', disabled=0))
            conn.execute(insert(capacity_approvals).values(id="fake-portable-approval", tenant_id=self.scope.tenant_id,
                pool="test-pool", configuration_id="test-configuration", approval_hash="b"*64,
                payload={"approved_cost_microusd": 23}, enabled=1, expires_at=9000, created_at=1))
            conn.execute(insert(capacity_cycles).values(approval_id="fake-portable-approval", intent_id=self.intent["id"], created_at=1))
            conn.execute(insert(capacity_waiters).values(job_id=self.job_row["id"], approval_id="fake-portable-approval",
                approval_hash="b"*64, deadline=9000, intent_id=self.intent["id"], state="waiting", created_at=1))
            conn.execute(insert(scaler_actions).values(intent_id=self.intent["id"], pool="test-pool",
                launch_spec={"provider": "fake"}, last_observation=null()))

    def tearDown(self):
        pass  # Registered cleanup above runs exactly once even after setUp fails.

    def test_concurrent_commit_does_not_split_business_and_media_snapshot(self):
        evidence, late = [], []
        def after_query(connection, cursor, statement, parameters, context, executemany):
            if "FROM information_schema.tables" in statement and not evidence:
                evidence.append((connection.exec_driver_sql("SHOW transaction_isolation").scalar_one(),
                                 connection.exec_driver_sql("SHOW transaction_read_only").scalar_one()))
                # These commits occur AFTER the source snapshot exists, on a
                # different connection. The backup must see none of them.
                self.repo.put_document(self.scope, "project", "project-1", {"title": "Concurrent replacement"}, expected_version=1)
                late.append(self.assets.upload("superdan", "project-1", io.BytesIO(png()), "late.png"))
                with self.repo.engine.begin() as second:
                    append_activity(second,tenant_id=self.scope.tenant_id,principal=Principal("superdan","superdan"),
                        project_id="project-1",version=2,occurred_at=2345.5,before={},after={},event_type="project.saved")
        event.listen(self.repo.engine, "after_cursor_execute", after_query)
        target = self.root/"portable-backup"
        try:
            result = backup_postgres_engine(self.repo.engine, self.store.root, target, schema=self.schema)
        finally:
            event.remove(self.repo.engine, "after_cursor_execute", after_query)
        self.assertEqual(evidence, [("repeatable read", "on")])
        self.assertEqual(result["restoration_target"], "isolated-sqlite-and-local-objects")
        self.assertFalse(result["native_postgres_restore"])
        manifest = verify_local(target)
        self.assertEqual(manifest["source_database"], "postgresql")
        self.assertEqual(manifest["tables"]["platform_assets"], 1)
        self.assertEqual(manifest["tables"]["platform_project_activity"],1)
        with closing(sqlite3.connect(target/"database.sqlite3")) as db:
            raw = db.execute("SELECT payload FROM platform_documents").fetchone()[0]
            self.assertEqual(json.loads(raw)["title"], "Snapshot original")
            self.assertEqual(db.execute("SELECT reserved_microusd FROM platform_budget_accounts WHERE id='owner-budget'").fetchone()[0], 123)
            self.assertEqual(db.execute("SELECT last_observation IS NULL FROM platform_scaler_actions").fetchone()[0], 1)
        for item in manifest["objects"]:
            self.assertEqual(hashlib.sha256((target/"media"/item["file"]).read_bytes()).hexdigest(), item["sha256"])
        self.assertNotIn(self.canary.encode(), (target/"database.sqlite3").read_bytes())
        self.assertNotIn(self.canary.encode(), (target/"manifest.json").read_bytes())
        recovery = self.root/"new-isolated-recovery"
        restore_local(target, recovery)
        restored = Repository("sqlite:///"+(recovery/"platform.sqlite3").as_posix(), clock=lambda: self.now)
        try:
            self.assertEqual(restored.get_job(self.scope, self.job_row["id"])["status"], "recovery_hold")
            self.assertEqual(TaskQueue(restored).get_attempt(self.scope, self.job_row["id"])["upstream_task_id"], "fake-upstream-needs-review")
            with restored.engine.connect() as conn:
                saved=list(conn.execute(select(activity)).mappings())
                self.assertEqual(len(saved),1)
                self.assertEqual((saved[0]["tenant_id"],saved[0]["owner_id"],saved[0]["project_id"],saved[0]["project_version"]),
                                 (self.scope.tenant_id,"superdan","project-1",1))
                self.assertEqual(saved[0]["operations"],["entity.update"])
                self.assertEqual(saved[0]["target_entity_ids"],["shot-one"])
                self.assertEqual(saved[0]["occurred_at"],1234.5)
                self.assertEqual(conn.execute(select(capacity_approvals.c.enabled)).scalar_one(), 0)
                self.assertEqual(conn.execute(select(capacity_waiters.c.state)).scalar_one(), "recovery_hold")
                self.assertEqual(conn.execute(select(capacity_cycles.c.intent_id)).scalar_one(), self.intent["id"])
                self.assertEqual(conn.execute(select(budget_accounts.c.reserved_microusd).where(budget_accounts.c.id == "owner-budget")).scalar_one(), 123)
            assets = AssetService(restored.engine, LocalObjectStore(recovery/"objects"), recovery,
                tenant=self.scope.tenant_id, max_bytes=1024*1024)
            self.assertEqual(assets.get("superdan", self.asset["id"])["id"], self.asset["id"])
            with self.assertRaises(AssetNotFound):
                assets.get("supervan", self.asset["id"])
            with self.assertRaises(AssetNotFound):
                assets.get("superdan", late[0]["id"])
            self.assertFalse(Auth(restored.engine, tenant=self.scope.tenant_id).ready())
        finally:
            restored.close()
        self.assertEqual(self.repo.get_job(self.scope, self.job_row["id"])["status"], "running")
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(capacity_approvals.c.enabled)).scalar_one(), 1)
            self.assertEqual(list(conn.execute(select(activity.c.project_version).order_by(activity.c.project_version)).scalars()),[1,2])

    def test_interrupted_snapshot_or_missing_media_never_publishes_manifest(self):
        target = self.root/"failed-snapshot"
        def fail(connection, cursor, statement, parameters, context, executemany):
            if "row_to_json(t)" in statement:
                raise RuntimeError("synthetic-private-diagnostic-must-not-escape")
        event.listen(self.repo.engine, "after_cursor_execute", fail)
        try:
            with self.assertRaises(BackupError) as caught:
                backup_postgres_engine(self.repo.engine, self.store.root, target, schema=self.schema)
        finally:
            event.remove(self.repo.engine, "after_cursor_execute", fail)
        self.assertNotIn("synthetic-private", str(caught.exception))
        self.assertFalse((target/"manifest.json").exists())
        missing = self.root/"missing-media"
        with patch.object(LocalObjectStore, "open", side_effect=OSError("synthetic missing object")):
            with self.assertRaises(BackupError):
                backup_postgres_engine(self.repo.engine, self.store.root, missing, schema=self.schema)
        self.assertFalse((missing/"manifest.json").exists())
        self.assertEqual(self.assets.get("superdan", self.asset["id"])["id"], self.asset["id"])

    def test_protected_runtime_url_wrapper_and_existing_target_refusal(self):
        source = self.root/"synthetic-runtime-dsn"
        # The opt-in fixture is explicitly local/synthetic and already validated
        # by LedgerCase. No real account credentials enter a test file/log.
        source.write_text(os.environ["PLATFORM_TEST_DATABASE_URL"], encoding="utf-8")
        source.chmod(0o600)
        target = self.root/"runtime-url-backup"
        result = backup_postgres_local(source, self.store.root, target, schema=self.schema)
        self.assertEqual(result["source_database"], "postgresql")
        original = (target/"manifest.json").read_bytes()
        with self.assertRaises(BackupError):
            backup_postgres_local(source, self.store.root, target, schema=self.schema)
        self.assertEqual((target/"manifest.json").read_bytes(), original)
        self.assertNotIn(source.read_bytes(), (target/"database.sqlite3").read_bytes())

    def test_json_null_preserves_its_distinction_from_sql_null(self):
        with self.repo.engine.begin() as conn:
            conn.execute(update(scaler_actions).values(last_observation=JSON.NULL))
        target = self.root/"json-null-backup"
        backup_postgres_engine(self.repo.engine, self.store.root, target, schema=self.schema)
        with closing(sqlite3.connect(target/"database.sqlite3")) as db:
            value = db.execute("SELECT last_observation,last_observation IS NULL FROM platform_scaler_actions").fetchone()
            self.assertEqual(value, ("null", 0))

    def test_r2_snapshot_restoration_retains_unknown_budget_and_source_identity(self):
        from dataclasses import replace
        from studio_platform.assets import asset_table
        local = self.store
        with self.repo.engine.begin() as conn:
            raw = conn.execute(select(asset_table.c.record)).scalar_one()
            record = json.loads(raw)
            for role in ("original", "model"):
                record[role]["provider"] = "r2"
            conn.execute(update(asset_table).values(record=json.dumps(record)))
        class ReadOnlyR2:
            def stat(self, key): return replace(local.stat(key), provider="r2")
            def open(self, key): return local.open(key)
        target = self.root/"r2-backup"
        backup_postgres_engine(self.repo.engine, None, target, schema=self.schema, stores={"r2": ReadOnlyR2()})
        manifest = verify_local(target)
        self.assertEqual({x.get("provider") for x in manifest["objects"]}, {"r2"})
        restored = self.root/"r2-restored"
        restore_local(target, restored)
        with closing(sqlite3.connect(restored/"platform.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT reserved_microusd FROM platform_budget_accounts WHERE id='owner-budget'").fetchone()[0], 123)
            value = json.loads(db.execute("SELECT record FROM platform_assets").fetchone()[0])
            self.assertEqual(value["original"]["provider"], "local")
            self.assertEqual(db.execute("SELECT status FROM platform_jobs WHERE id=?", (self.job_row["id"],)).fetchone()[0], "recovery_hold")
        self.assertEqual(self.assets.get("superdan", self.asset["id"])["original"]["provider"], "r2")


if __name__ == "__main__":
    unittest.main()
