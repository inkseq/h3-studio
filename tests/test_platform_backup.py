"""Temporary SQLite/media tests and opt-in isolated real PostgreSQL drill."""

import hashlib
from contextlib import closing
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from sqlalchemy import insert, select

from studio_platform.backup import (
    backup_local,
    verify_local,
    restore_local,
    dump_postgres,
    BackupError,
)
from studio_platform.assets import AssetService, AssetNotFound
from studio_platform.asset_objects import ASSET_OBJECT_ROLES
from studio_platform.auth import Auth, accounts, sessions, clients
from studio_platform.agent_connect import AgentConnect, audit as connection_audit
from studio_platform.repository import Repository, Scope
from studio_platform.queue import TaskQueue
from studio_platform.storage import LocalObjectStore
from test_platform_assets import png


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sixnine-backup-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.live = self.root / "live"
        self.live.mkdir()
        self.database = self.live / "platform.sqlite3"
        self.repo = Repository("sqlite:///" + self.database.as_posix())
        self.addCleanup(self.repo.close)
        self.repo.create_schema()
        self.scope = Scope("sixnine", "superdan", "my-project")
        self.store = LocalObjectStore(self.live / "objects")
        self.assets = AssetService(
            self.repo.engine, self.store, self.live, max_bytes=1024 * 1024
        )
        self.asset = self.assets.upload(
            "superdan", "my-project", io.BytesIO(png()), "scene.png"
        )
        self.repo.put_document(
            self.scope,
            "project",
            "my-project",
            {"title": "Example chapter", "asset_id": self.asset["id"]},
        )
        plan = self.repo.create_plan(
            self.scope,
            {"prompt": "protected creative draft"},
            {"pool": "test-pool", "enabled": True},
            expires_at=9999999999,
            estimated_cost_microusd=0,
        )
        self.job = self.repo.create_job(self.scope, plan["id"], "unfinished")
        self.repo.configure_capacity(max_instances=2, max_physical_gpus=2)
        auth = Auth(self.repo.engine)
        AgentConnect(auth)
        self.canary = "fake-auth-secret-must-not-enter-backup"
        with self.repo.engine.begin() as conn:
            conn.execute(
                insert(connection_audit).values(
                    tenant="sixnine",
                    id="audit-backup-fixture",
                    connection_id="connection-fixture",
                    owner="superdan",
                    event=self.canary,
                    created_at=1,
                )
            )
            conn.execute(
                insert(accounts).values(
                    tenant="sixnine",
                    username="superdan",
                    password_hash=self.canary,
                    disabled=0,
                    updated=1,
                )
            )
            conn.execute(
                insert(sessions).values(
                    tenant="sixnine",
                    token_hash=self.canary,
                    username="superdan",
                    auth_mode="password",
                    created=1,
                    expires=9999999999,
                    password_version=1,
                )
            )
            conn.execute(
                insert(clients).values(
                    tenant="sixnine",
                    id="fake-client",
                    token_hash=self.canary,
                    owner="superdan",
                    projects='["my-project"]',
                    scopes='["read"]',
                    disabled=0,
                )
            )
        (self.live / ".env").write_text("FAKE_API_KEY=" + self.canary)
        patch = mock.patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("No network in backup tests"),
        )
        patch.start()
        self.addCleanup(patch.stop)

    def backup(self):
        destination = self.root / "backup"
        result = backup_local(self.database, self.store.root, destination)
        self.assertEqual(result["state"], "backed_up")
        return destination

    def remote_backup(
        self,
        *,
        corrupt=False,
        wrong_provider=False,
        missing_sha=False,
        wrong_sha=False,
        receipt=False,
    ):
        from dataclasses import replace
        from studio_platform.backup import (
            backup_postgres_engine,
            _snapshot,
            _export_business_db,
        )

        # Synthetic R2-only records: Local is a fixture byte source, never a
        # configured fallback. The adapter exposes only stat/open reads.
        with closing(sqlite3.connect(self.database)) as db, db:
            for ident, raw in db.execute(
                "SELECT id,record FROM platform_assets"
            ).fetchall():
                record = json.loads(raw)
                for role in ASSET_OBJECT_ROLES:
                    record[role]["provider"] = "r2"
                db.execute(
                    "UPDATE platform_assets SET record=? WHERE id=?",
                    (json.dumps(record), ident),
                )
        local = self.store
        from types import SimpleNamespace

        config = SimpleNamespace(
            provider="r2",
            endpoint_url="https://example.invalid",
            region="auto",
            bucket="backup-test",
            service="r2",
            profile="fixture",
        )
        if receipt:
            from studio_platform.repository import artifacts
            from studio_platform.artifact_writer import write_receipts, _hash

            lease = TaskQueue(self.repo).claim("backup-test-worker", "test-pool").lease
            info = local.write_new(
                "superdan",
                str(uuid.uuid4()),
                io.BytesIO(b"video"),
                content_type="video/mp4",
            )
            spec = dict(
                object_key=info.key,
                size_bytes=info.size_bytes,
                sha256=info.sha256,
                content_type=info.content_type,
                kind="video",
                validated=True,
            )
            tenant = self.job["tenant_id"]
            ident = _hash([tenant, "superdan", self.job["id"], lease.attempt_id])
            record = dict(
                id=ident,
                owner="superdan",
                collection_route="gpu-r2-v1",
                binding=_hash(vars(config)),
                roles={
                    "video": dict(
                        key=info.key,
                        status="verified",
                        **{
                            k: spec[k] for k in ("size_bytes", "sha256", "content_type")
                        },
                    )
                },
            )
            if isinstance(receipt, str):
                if receipt == "binding":
                    record["binding"] = "0" * 64
                elif receipt == "owner":
                    record["owner"] = "supervan"
                elif receipt == "sha":
                    record["roles"]["video"]["sha256"] = "0" * 64
                elif receipt == "route":
                    record["collection_route"] = "unknown"
                elif receipt == "tenant":
                    tenant = "other"
                elif receipt == "cpu":
                    record.pop("collection_route")
                elif receipt == "cpu_binding":
                    record.pop("collection_route")
                    record["binding"] = "0" * 64
                elif receipt == "local":
                    record.pop("collection_route")
                    record["binding"] = _hash(
                        {"provider": "local", "root": str(local.root)}
                    )
            write_receipts.metadata.create_all(self.repo.engine)
            with self.repo.engine.begin() as conn:
                conn.execute(
                    insert(artifacts).values(
                        id=str(uuid.uuid4()),
                        job_id=self.job["id"],
                        attempt_id=lease.attempt_id,
                        created_at=1,
                        metadata=spec,
                    )
                )
                conn.execute(
                    insert(write_receipts).values(
                        id=ident,
                        tenant=tenant,
                        owner="superdan",
                        job_id=self.job["id"],
                        attempt_id=lease.attempt_id,
                        version=1,
                        record=json.dumps(record),
                    )
                )

        class ReadOnlyR2:
            def stat(self, key):
                return replace(
                    local.stat(key),
                    provider="local" if wrong_provider else "r2",
                    sha256=None
                    if missing_sha
                    else "0" * 64
                    if wrong_sha
                    else local.stat(key).sha256,
                )

            def open(self, key):
                return io.BytesIO(b"corrupt") if corrupt else local.open(key)

        ReadOnlyR2.config = config

        def export(engine, destination, **kwargs):
            with closing(_snapshot(self.database)) as snapshot:
                return _export_business_db(snapshot, destination)

        with mock.patch(
            "studio_platform.backup._export_postgres_business_db", side_effect=export
        ):
            backup_postgres_engine(
                object(),
                None,
                self.root / "remote-backup",
                stores={"r2": ReadOnlyR2(), "local": local},
            )
        return self.root / "remote-backup"

    def test_r2_only_media_manifest_and_isolated_restore_preserve_original_evidence(
        self,
    ):
        from studio_platform.repository import artifacts

        lease = TaskQueue(self.repo).claim("backup-test-worker", "test-pool").lease
        expected_media = {}
        for kind, mime in (("video", "video/mp4"), ("audio", "audio/wav")):
            body = ("synthetic-" + kind).encode()
            info = self.store.write_new(
                "superdan", str(uuid.uuid4()), io.BytesIO(body), content_type=mime
            )
            expected_media[info.key] = body
            with self.repo.engine.begin() as conn:
                conn.execute(
                    insert(artifacts).values(
                        id=str(uuid.uuid4()),
                        job_id=self.job["id"],
                        attempt_id=lease.attempt_id,
                        created_at=1,
                        metadata={
                            "provider": "r2",
                            "object_key": info.key,
                            "size_bytes": info.size_bytes,
                            "sha256": info.sha256,
                            "content_type": mime,
                            "kind": kind,
                            "validated": True,
                        },
                    )
                )
        source = self.remote_backup()
        manifest = verify_local(source)
        self.assertEqual({item["provider"] for item in manifest["objects"]}, {"r2"})
        original = (source / "database.sqlite3").read_bytes()
        result = restore_local(source, self.root / "remote-restored")
        self.assertEqual(result["held_jobs"], 1)
        self.assertEqual((source / "database.sqlite3").read_bytes(), original)
        with closing(
            sqlite3.connect(self.root / "remote-restored" / "platform.sqlite3")
        ) as db:
            record = json.loads(
                db.execute("SELECT record FROM platform_assets").fetchone()[0]
            )
            self.assertEqual(record["original"]["provider"], "local")
            self.assertEqual(record["model"]["provider"], "local")
        restored = LocalObjectStore(self.root / "remote-restored" / "objects")
        with restored.open(record["original"]["key"]) as body:
            self.assertEqual(body.read(), png())
        for key, expected in expected_media.items():
            with restored.open(key) as body:
                self.assertEqual(body.read(), expected)
        with closing(
            sqlite3.connect(self.root / "remote-restored" / "platform.sqlite3")
        ) as db:
            specs = [
                json.loads(row[0])
                for row in db.execute("SELECT metadata FROM platform_artifacts")
            ]
            self.assertEqual({s["kind"] for s in specs}, {"video", "audio"})
            self.assertTrue(all(s["provider"] == "local" for s in specs))

    def test_historical_gpu_receipt_without_provider_or_head_sha_restores_locally(self):
        source = self.remote_backup(receipt=True, missing_sha=True)
        manifest = verify_local(source)
        self.assertEqual(sum("storage_binding" in o for o in manifest["objects"]), 1)
        restore_local(source, self.root / "recovered")
        with closing(
            sqlite3.connect(self.root / "recovered" / "platform.sqlite3")
        ) as db:
            spec = json.loads(
                db.execute("SELECT metadata FROM platform_artifacts").fetchone()[0]
            )
            self.assertEqual(spec["provider"], "local")
        with closing(sqlite3.connect(source / "database.sqlite3")) as db:
            self.assertNotIn(
                "provider",
                json.loads(
                    db.execute("SELECT metadata FROM platform_artifacts").fetchone()[0]
                ),
            )

    def test_cpu_r2_receipt_binding_without_route_or_provider_is_preserved(self):
        source = self.remote_backup(receipt="cpu")
        manifest = verify_local(source)
        self.assertEqual(sum("storage_binding" in o for o in manifest["objects"]), 1)
        self.assertTrue(all(o["provider"] == "r2" for o in manifest["objects"]))

    def test_original_local_receipt_keeps_original_manifest_format(self):
        source = self.remote_backup(receipt="local")
        manifest = verify_local(source)
        artifacts = [
            o
            for o in manifest["objects"]
            if any(r["table"] == "artifacts" for r in o["references"])
        ]
        self.assertEqual(len(artifacts), 1)
        self.assertNotIn("provider", artifacts[0])
        self.assertNotIn("storage_binding", artifacts[0])

    def test_missing_head_sha_never_accepts_wrong_body(self):
        with self.assertRaises(BackupError):
            self.remote_backup(missing_sha=True, corrupt=True)
        self.assertFalse((self.root / "remote-backup" / "manifest.json").exists())

    def test_present_wrong_head_sha_rejected(self):
        with self.assertRaises(BackupError):
            self.remote_backup(wrong_sha=True)
        self.assertFalse((self.root / "remote-backup" / "manifest.json").exists())

    def test_gpu_receipt_identity_mismatches_rejected(self):
        # Each fresh fixture protects a distinct original provenance boundary.
        for field in ("binding", "owner", "sha", "route", "tenant", "cpu_binding"):
            case = type(self)("runTest")
            case.setUp()
            try:
                with self.subTest(field=field), self.assertRaises(BackupError):
                    case.remote_backup(receipt=field, missing_sha=True)
                self.assertFalse(
                    (case.root / "remote-backup" / "manifest.json").exists()
                )
            finally:
                case.doCleanups()

    def test_r2_changed_bytes_never_publish_completion_manifest(self):
        with self.assertRaises(BackupError):
            self.remote_backup(corrupt=True)
        self.assertFalse((self.root / "remote-backup" / "manifest.json").exists())

    def test_r2_provider_mismatch_never_falls_back(self):
        with self.assertRaises(BackupError):
            self.remote_backup(wrong_provider=True)
        self.assertFalse((self.root / "remote-backup" / "manifest.json").exists())

    def test_real_asset_project_backup_and_isolated_restore_preserve_owner_and_disable_jobs(
        self,
    ):
        source = self.backup()
        manifest = verify_local(source)
        from studio_platform.backup_cli import main as runtime_main
        from tools.backup_platform import main as script_main

        self.assertIs(runtime_main, script_main)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(runtime_main(["verify", "--backup", str(source)]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "verified")
        self.assertNotIn(str(source), output.getvalue())
        self.assertEqual(len(manifest["objects"]), 4)
        destination = self.root / "recovered"
        result = restore_local(source, destination)
        self.assertEqual(result["held_jobs"], 1)
        self.assertFalse(result["execution_enabled"])
        restored = Repository(
            "sqlite:///" + (destination / "platform.sqlite3").as_posix()
        )
        self.addCleanup(restored.close)
        store = LocalObjectStore(destination / "objects")
        service = AssetService(restored.engine, store, destination)
        asset = service.get("superdan", self.asset["id"])
        with store.open(asset["original"]["key"]) as data:
            self.assertEqual(data.read(), png())
        with self.assertRaises(AssetNotFound):
            service.get("supervan", self.asset["id"])
        self.assertEqual(
            restored.get_document(self.scope, "project", "my-project")["payload"][
                "title"
            ],
            "Example chapter",
        )
        held = restored.get_job(self.scope, self.job["id"])
        self.assertEqual(held["status"], "recovery_hold")
        self.assertFalse(held["execution_plan"]["enabled"])
        self.assertIsNone(TaskQueue(restored).claim("must-not-run", "test-pool"))
        self.assertFalse(Auth(restored.engine).ready())
        self.assertEqual(
            self.repo.get_job(self.scope, self.job["id"])["status"], "queued"
        )

    def test_no_auth_rows_or_secret_files_ever_written_to_backup_database(self):
        source = self.backup()
        self.assertNotIn(
            self.canary.encode(), (source / "database.sqlite3").read_bytes()
        )
        self.assertNotIn(
            self.canary, (source / "manifest.json").read_text(encoding="utf-8")
        )
        with closing(sqlite3.connect(source / "database.sqlite3")) as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertFalse(
                names
                & {"platform_accounts", "platform_sessions", "platform_service_clients"}
            )
            self.assertFalse(
                names
                & {
                    "platform_agent_connections",
                    "platform_agent_exchange_limits",
                    "platform_agent_connection_audit",
                }
            )
        self.assertEqual(
            {p.name for p in source.iterdir()},
            {"database.sqlite3", "media", "manifest.json"},
        )
        self.assertTrue((self.live / ".env").exists())
        if os.name != "nt":
            self.assertEqual(source.stat().st_mode & 0o077, 0)
            self.assertEqual((source / "database.sqlite3").stat().st_mode & 0o077, 0)

    def test_wal_committed_changes_are_in_consistent_snapshot(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
        )
        connection.execute(
            "UPDATE platform_jobs SET error_code='committed-in-wal' WHERE id=?",
            (self.job["id"],),
        )
        connection.commit()
        source = self.backup()
        with closing(sqlite3.connect(source / "database.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT error_code FROM platform_jobs WHERE id=?", (self.job["id"],)
                ).fetchone()[0],
                "committed-in-wal",
            )

    def test_existing_target_never_overwritten_and_no_source_database_change(self):
        source = self.backup()
        marker = self.live / "preserve.txt"
        marker.write_text("preserve")
        with self.assertRaises(BackupError):
            restore_local(source, self.live)
        self.assertEqual(marker.read_text(), "preserve")
        with self.assertRaises(BackupError):
            backup_local(self.database, self.store.root, source)
        self.assertEqual(verify_local(source)["kind"], "sixnine-local-business-backup")

    def test_corrupted_media_and_manifest_path_rejected_before_restore_directory(self):
        source = self.backup()
        manifest = verify_local(source)
        target = source / "media" / manifest["objects"][0]["file"]
        original = target.read_bytes()
        target.write_bytes(b"corrupt")
        with self.assertRaises(BackupError):
            restore_local(source, self.root / "must-not-exist")
        self.assertFalse((self.root / "must-not-exist").exists())
        target.write_bytes(original)
        manifest["objects"][0]["file"] = "../../outside"
        (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(BackupError):
            verify_local(source)

    def test_missing_referenced_object_does_not_publish_success_manifest(self):
        asset = self.assets.get("superdan", self.asset["id"])
        self.store.delete(asset["model"]["key"])
        with self.assertRaises(BackupError):
            self.backup()
        self.assertFalse((self.root / "backup" / "manifest.json").exists())

    def test_unknown_table_requires_review_instead_of_copying_possible_secret(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE unreviewed_secrets(value TEXT)")
            db.execute("INSERT INTO unreviewed_secrets VALUES (?)", (self.canary,))
        with self.assertRaises(BackupError):
            self.backup()
        self.assertFalse((self.root / "backup").exists())

    def test_application_activity_schema_and_history_survive_isolated_restore(self):
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        from studio_platform.auth import Principal
        from studio_platform.project_activity import activity, append_activity

        # Exercise the actual API schema constructors, not only Repository's
        # narrower table set that previously concealed this production omission.
        create_app(
            Settings(data_dir=self.live), repository=self.repo, storage=self.store
        )
        with self.repo.engine.begin() as conn:
            for tenant, owner in (
                ("sixnine", "superdan"),
                ("sixnine", "supervan"),
                ("other-tenant", "superdan"),
            ):
                append_activity(
                    conn,
                    tenant_id=tenant,
                    principal=Principal(owner, owner),
                    project_id="my-project",
                    version=2,
                    occurred_at=1234.5,
                    before={"entities": []},
                    after={"entities": [{"id": "shot-one", "title": "private scene"}]},
                    actions=[
                        {
                            "op": "entity.update",
                            "entity_id": "shot-one",
                            "patch": {"prompt": "not audit content"},
                        }
                    ],
                )
            expected = [
                dict(row)
                for row in conn.execute(
                    select(activity).order_by(activity.c.tenant_id, activity.c.owner_id)
                ).mappings()
            ]
        source = self.backup()
        self.assertEqual(verify_local(source)["tables"]["platform_project_activity"], 3)
        destination = self.root / "activity-recovery"
        restored_result = restore_local(source, destination)
        restored = Repository(
            "sqlite:///" + (destination / "platform.sqlite3").as_posix()
        )
        self.addCleanup(restored.close)
        with restored.engine.connect() as conn:
            actual = [
                dict(row)
                for row in conn.execute(
                    select(activity).order_by(activity.c.tenant_id, activity.c.owner_id)
                ).mappings()
            ]
            mine = list(
                conn.execute(
                    select(activity).where(
                        activity.c.tenant_id == "sixnine",
                        activity.c.owner_id == "superdan",
                    )
                ).mappings()
            )
        self.assertEqual(actual, expected)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["operations"], ["entity.update"])
        self.assertEqual(mine[0]["target_entity_ids"], ["shot-one"])
        self.assertNotIn("not audit content", json.dumps(actual))
        self.assertNotIn(
            self.canary.encode(), (source / "database.sqlite3").read_bytes()
        )
        self.assertFalse(restored_result["execution_enabled"])
        self.assertEqual(
            restored.get_job(self.scope, self.job["id"])["status"], "recovery_hold"
        )

    def test_older_backup_without_activity_restores_empty_history_without_source_change(
        self,
    ):
        source = self.backup()
        with closing(sqlite3.connect(source / "database.sqlite3")) as db, db:
            db.execute("DROP TABLE platform_project_activity")
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["tables"].pop("platform_project_activity")
        manifest["database_sha256"] = hashlib.sha256(
            (source / "database.sqlite3").read_bytes()
        ).hexdigest()
        (source / "manifest.json").write_text(json.dumps(manifest))
        original = (source / "database.sqlite3").read_bytes()
        destination = self.root / "older-activity-recovery"
        restore_local(source, destination)
        with closing(sqlite3.connect(destination / "platform.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM platform_project_activity").fetchone()[
                    0
                ],
                0,
            )
        self.assertEqual((source / "database.sqlite3").read_bytes(), original)

    def test_activity_column_drift_still_refuses_without_publishing_backup(self):
        from studio_platform.project_activity import metadata

        metadata.create_all(self.repo.engine)
        with self.repo.engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE platform_project_activity ADD COLUMN unreviewed_private TEXT"
            )
        with self.assertRaisesRegex(BackupError, "backup_database_version_mismatch"):
            self.backup()
        self.assertFalse((self.root / "backup").exists())

    def test_pg_dump_credentials_only_in_child_memory_no_auth_tables_or_argv_secrets(
        self,
    ):
        url_file = self.root / "protected-url"
        url_file.write_text(
            "postgresql+psycopg://testuser:fake-db-private@127.0.0.1:5432/sixnine_test"
        )
        url_file.chmod(0o600)
        original = subprocess.run
        observed = []

        def fake(command, **kwargs):
            if command[0] != "fake-pg-dump":
                return original(command, **kwargs)  # Windows ACL commands only.
            observed.append(command)
            self.assertFalse(any("fake-db-private" in argument for argument in command))
            self.assertEqual(kwargs["env"]["PGPASSWORD"], "fake-db-private")
            self.assertNotIn("PGSERVICE", kwargs["env"])
            self.assertFalse(
                any(
                    any(
                        name in item
                        for name in (
                            "platform_accounts",
                            "platform_sessions",
                            "platform_service_clients",
                        )
                    )
                    for item in command
                )
            )
            output = Path(
                next(item[7:] for item in command if item.startswith("--file="))
            )
            output.write_bytes(b"fake-custom-format-archive")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch("studio_platform.backup.subprocess.run", side_effect=fake):
            result = dump_postgres(
                url_file, self.root / "pg-backup", pg_dump="fake-pg-dump"
            )
        self.assertEqual(len(observed), 1)
        self.assertFalse(result["restore_verified"])
        self.assertFalse(result["media_included"])
        self.assertNotIn(
            b"fake-db-private", (self.root / "pg-backup" / "manifest.json").read_bytes()
        )
        with self.assertRaises(BackupError):
            restore_local(self.root / "pg-backup", self.root / "not-a-local-backup")

    def test_pg_errors_are_generic_and_missing_explicit_host_does_not_start_process(
        self,
    ):
        url_file = self.root / "protected-url"
        url_file.write_text("postgresql://user:fake-secret@/database")
        url_file.chmod(0o600)
        with mock.patch(
            "studio_platform.backup.subprocess.run",
            side_effect=AssertionError("No default host fallback"),
        ):
            with self.assertRaises(BackupError) as caught:
                dump_postgres(url_file, self.root / "not-created")
        self.assertNotIn("fake-secret", str(caught.exception))
        self.assertFalse((self.root / "not-created").exists())

    def test_private_compose_dsn_requires_explicit_exact_network_opt_in(self):
        from studio_platform.backup import _postgres_environment

        source = self.root / "synthetic-private-url"
        original = "postgresql+psycopg://sixnine_app:synthetic-private-password-only@db:5432/sixnine"
        source.write_text(original)
        source.chmod(0o600)
        with self.assertRaises(BackupError):
            _postgres_environment(source)
        environment = _postgres_environment(source, private_platform_network=True)
        self.assertEqual(environment["PGSSLMODE"], "disable")
        self.assertEqual(environment["PGHOST"], "db")
        for value in (
            original.replace("@db:", "@other-host:"),
            original.replace("sixnine_app:", "postgres:"),
            original.replace(":5432/", ":6543/"),
            original + "?sslmode=disable",
        ):
            source.write_text(value)
            with self.assertRaises(BackupError):
                _postgres_environment(source, private_platform_network=True)

    def test_remote_database_cannot_disable_tls_via_url_query(self):
        from studio_platform.backup import _postgres_environment

        source = self.root / "synthetic-remote-url"
        source.touch()
        source.chmod(0o600)
        for hostname in ("remote.example", "10.0.0.5", "db"):
            source.write_text(
                "postgresql://testuser:fake-tls-secret@"
                + hostname
                + ":5432/sixnine_test?sslmode=disable"
            )
            with (
                self.subTest(hostname=hostname),
                self.assertRaises(BackupError) as caught,
            ):
                _postgres_environment(source)
            self.assertNotIn("fake-tls-secret", str(caught.exception))
        source.write_text(
            "postgresql://testuser:fake-tls-secret@remote.example:5432/sixnine_test?sslmode=verify-full"
        )
        self.assertEqual(_postgres_environment(source)["PGSSLMODE"], "verify-full")
        source.write_text(
            "postgresql://testuser:fake-tls-secret@127.0.0.1:5432/sixnine_test?sslmode=disable"
        )
        self.assertEqual(_postgres_environment(source)["PGSSLMODE"], "disable")

    def test_inflight_upstream_evidence_survives_but_restore_never_reclaims_job(self):
        queue = TaskQueue(self.repo)
        claim = queue.claim("old-worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "fake-upstream-still-needs-reconciliation")
        source = self.backup()
        destination = self.root / "restored-unknown"
        restore_local(source, destination)
        restored = Repository(
            "sqlite:///" + (destination / "platform.sqlite3").as_posix()
        )
        self.addCleanup(restored.close)
        job = restored.get_job(self.scope, self.job["id"])
        self.assertEqual(job["status"], "recovery_hold")
        self.assertGreater(job["fence"], claim.lease.fence)
        self.assertIsNone(job["lease_worker_id"])
        attempt = TaskQueue(restored).get_attempt(self.scope, self.job["id"])
        self.assertEqual(
            attempt["upstream_task_id"], "fake-upstream-still-needs-reconciliation"
        )
        self.assertEqual(attempt["upstream_stopped"], 0)
        self.assertIsNone(
            TaskQueue(restored).claim("new-worker", "test-pool", purpose="reconcile")
        )

    def test_restored_capacity_approval_is_disabled_and_waiter_evidence_is_held(self):
        from sqlalchemy import select, update
        from studio_platform.repository import (
            capacity_approvals,
            capacity_cycles,
            capacity_waiters,
            jobs,
            instance_intents,
            budget_accounts as budgets,
        )

        self.repo.configure_pool("test-pool", max_instances=2, max_physical_gpus=2)
        self.repo.configure_budget(
            "capacity-backup-budget",
            tenant_id="sixnine",
            owner_id="superdan",
            limit_microusd=1000,
        )
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "test-pool",
            "fake-capacity-intent",
            physical_gpus=1,
            hard_deadline=9999999999,
            reserved_cost_microusd=23,
            budget_account_ids=["capacity-backup-budget"],
            dry_run=False,
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(intent["id"], "creation_unknown")
        evidence = {
            "source": "synthetic-test",
            "provider": "fake",
            "max_price_microusd": 23,
        }
        with self.repo.engine.begin() as conn:
            conn.execute(
                insert(capacity_approvals).values(
                    id="backup-capacity",
                    tenant_id="sixnine",
                    pool="test-pool",
                    configuration_id="test-config",
                    approval_hash="a" * 64,
                    payload=evidence,
                    enabled=1,
                    expires_at=9999999999,
                    created_at=1,
                )
            )
            conn.execute(
                insert(capacity_cycles).values(
                    approval_id="backup-capacity", intent_id=intent["id"], created_at=1
                )
            )
            conn.execute(
                insert(capacity_waiters).values(
                    job_id=self.job["id"],
                    approval_id="backup-capacity",
                    approval_hash="a" * 64,
                    deadline=9999999999,
                    intent_id=intent["id"],
                    state="waiting",
                    created_at=1,
                )
            )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == self.job["id"])
                .values(status="waiting_capacity")
            )
            budget_before = dict(
                conn.execute(
                    select(budgets).where(budgets.c.id == "capacity-backup-budget")
                )
                .mappings()
                .one()
            )
        source = self.backup()
        target = self.root / "capacity-recovery"
        restore_local(source, target)
        restored = Repository("sqlite:///" + (target / "platform.sqlite3").as_posix())
        self.addCleanup(restored.close)
        with restored.engine.connect() as conn:
            approval = conn.execute(select(capacity_approvals)).mappings().one()
            self.assertEqual(approval["enabled"], 0)
            self.assertEqual(approval["payload"], evidence)
            self.assertEqual(approval["expires_at"], 9999999999)
            waiter = conn.execute(select(capacity_waiters)).mappings().one()
            self.assertEqual(waiter["state"], "recovery_hold")
            self.assertEqual(waiter["intent_id"], intent["id"])
            self.assertEqual(
                (waiter["approval_hash"], waiter["deadline"]), ("a" * 64, 9999999999)
            )
            self.assertEqual(
                conn.execute(select(capacity_cycles.c.intent_id)).scalar_one(),
                intent["id"],
            )
            self.assertEqual(
                conn.execute(select(instance_intents.c.state)).scalar_one(),
                "creation_unknown",
            )
            self.assertEqual(
                dict(
                    conn.execute(
                        select(budgets).where(budgets.c.id == "capacity-backup-budget")
                    )
                    .mappings()
                    .one()
                ),
                budget_before,
            )
        self.assertEqual(
            restored.get_job(self.scope, self.job["id"])["status"], "recovery_hold"
        )
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(capacity_approvals.c.enabled)).scalar_one(), 1
            )

    def test_older_backup_without_capacity_tables_restores_with_empty_disabled_defaults(
        self,
    ):
        source = self.backup()
        names = (
            "platform_capacity_waiters",
            "platform_capacity_cycles",
            "platform_capacity_approvals",
        )
        with closing(sqlite3.connect(source / "database.sqlite3")) as db, db:
            for name in names:
                db.execute('DROP TABLE "' + name + '"')
        manifest = json.loads((source / "manifest.json").read_text())
        for name in names:
            manifest["tables"].pop(name)
        manifest["database_sha256"] = hashlib.sha256(
            (source / "database.sqlite3").read_bytes()
        ).hexdigest()
        (source / "manifest.json").write_text(json.dumps(manifest))
        verify_local(source)
        target = self.root / "older-version-recovery"
        restore_local(source, target)
        with closing(sqlite3.connect(target / "platform.sqlite3")) as db:
            for name in names:
                self.assertEqual(
                    db.execute('SELECT count(*) FROM "' + name + '"').fetchone(), (0,)
                )
        self.assertEqual(
            hashlib.sha256((source / "database.sqlite3").read_bytes()).hexdigest(),
            manifest["database_sha256"],
        )

    def test_real_cpu_simulation_video_artifact_round_trip_keeps_verified_bytes(self):
        from studio_platform.worker import MockBackend, WorkerRunner
        from studio_platform.repository import artifacts
        from sqlalchemy import select

        plan = self.repo.create_plan(
            self.scope,
            {
                "request": {
                    "duration": 4,
                    "width": 256,
                    "height": 256,
                    "generate_audio": False,
                },
                "output_spec": {"width": 256, "height": 256},
            },
            {"pool": "backup-video", "backend": "mock", "enabled": True},
            expires_at=9999999999,
            estimated_cost_microusd=0,
        )
        job = self.repo.create_job(self.scope, plan["id"], "video-for-backup")
        work = self.root / "worker"
        runner = WorkerRunner(
            self.repo,
            self.store,
            work,
            backend=MockBackend(work / "mock", enabled=True),
        )
        self.assertEqual(
            runner.run_once("offline-cpu", "backup-video")["state"], "succeeded"
        )
        with self.repo.engine.connect() as conn:
            spec = conn.execute(
                select(artifacts.c.metadata).where(artifacts.c.job_id == job["id"])
            ).scalar_one()
        source = self.backup()
        destination = self.root / "restored-video"
        restore_local(source, destination)
        restored_store = LocalObjectStore(destination / "objects")
        with restored_store.open(spec["object_key"]) as media:
            self.assertEqual(hashlib.sha256(media.read()).hexdigest(), spec["sha256"])

    def test_hardlinked_backup_media_is_refused(self):
        source = self.backup()
        item = verify_local(source)["objects"][0]
        media = source / "media" / item["file"]
        os.link(media, self.root / "linked-media")
        with self.assertRaises(BackupError):
            verify_local(source)

    @unittest.skipUnless(
        os.environ.get("SIXNINE_TEST_PG_BACKUP") == "1",
        "Explicit dedicated PostgreSQL test container only",
    )
    def test_real_postgres_dump_restore_into_new_database_and_media_manifest(self):
        from sqlalchemy import MetaData, JSON, Integer
        from sqlalchemy.schema import CreateTable
        from sqlalchemy.dialects.postgresql import dialect
        from studio_platform.backup import _tables

        container = "sixnine-platform-test-db-20261004"
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Name}} {{.Config.Image}} {{.State.Running}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        self.assertEqual(
            inspected.stdout.strip(), "/" + container + " postgres:17-alpine true"
        )
        suffix = uuid.uuid4().hex
        schema, target_db = "backup_test_" + suffix, "sixnine_restore_" + suffix
        self.assertRegex(schema, r"^backup_test_[0-9a-f]{32}$")
        self.assertRegex(target_db, r"^sixnine_restore_[0-9a-f]{32}$")
        source_created = target_created = False

        def run(command, data=None):
            # Only the configured non-secret role name is loaded. Unix-socket
            # authentication is internal to this already-authorized test container.
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    "--user",
                    "postgres",
                    container,
                    "sh",
                    "-c",
                    'export PGUSER="$POSTGRES_USER"; exec "$@"',
                    "backup-test",
                    *command,
                ],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=90,
            )
            self.assertEqual(
                result.returncode,
                0,
                "Dedicated PG test command failed; no secret output echoed",
            )
            return result.stdout

        def sql(database, value):
            return (
                run(
                    [
                        "psql",
                        "-X",
                        "-q",
                        "-A",
                        "-t",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-d",
                        database,
                    ],
                    value.encode(),
                )
                .decode()
                .strip()
            )

        queue = TaskQueue(self.repo)
        claim = queue.claim("lost-test-worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "fake-postgres-upstream-needs-review")
        self.repo.configure_pool("test-pool", max_instances=2, max_physical_gpus=2)
        self.repo.configure_budget(
            "backup-test-budget",
            tenant_id="sixnine",
            owner_id="superdan",
            limit_microusd=1000,
        )
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "test-pool",
            "fake-uncertain-create",
            physical_gpus=1,
            hard_deadline=9999999999,
            reserved_cost_microusd=1,
            budget_account_ids=["backup-test-budget"],
            dry_run=False,
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(intent["id"], "creation_unknown")
        source = self.backup()
        manifest = verify_local(source)
        try:
            sql("sixnine_test", 'CREATE SCHEMA "' + schema + '";')
            source_created = True
            copied = MetaData()
            for table in _tables().values():
                table.to_metadata(copied, schema=schema)
            commands = [
                str(CreateTable(table).compile(dialect=dialect())) + ";"
                for table in copied.sorted_tables
            ]
            with closing(sqlite3.connect(source / "database.sqlite3")) as snapshot:
                snapshot.row_factory = sqlite3.Row
                for table in copied.sorted_tables:
                    name = table.name
                    rows = snapshot.execute('SELECT * FROM "' + name + '"').fetchall()
                    for row in rows:
                        value = dict(row)
                        for column in table.columns:
                            if (
                                isinstance(column.type, JSON)
                                and value[column.name] is not None
                            ):
                                value[column.name] = json.loads(value[column.name])
                        payload = json.dumps(value, ensure_ascii=True).replace(
                            "'", "''"
                        )
                        relation = '"' + schema + '"."' + name + '"'
                        commands.append(
                            "INSERT INTO "
                            + relation
                            + " SELECT * FROM json_populate_record(NULL::"
                            + relation
                            + ",'"
                            + payload
                            + "');"
                        )
                    if len(table.primary_key.columns) == 1:
                        column = next(iter(table.primary_key.columns))
                        if isinstance(column.type, Integer) and rows:
                            relation = '"' + schema + '"."' + name + '"'
                            commands.append(
                                "SELECT setval(pg_get_serial_sequence('"
                                + relation
                                + "','"
                                + column.name
                                + "'), (SELECT MAX(\""
                                + column.name
                                + '") FROM '
                                + relation
                                + "));"
                            )
            commands += [
                'CREATE TABLE "' + schema + '".platform_accounts (secret TEXT);',
                'INSERT INTO "'
                + schema
                + '".platform_accounts VALUES (\''
                + self.canary
                + "');",
            ]
            sql("sixnine_test", "\n".join(commands))
            archive = run(
                [
                    "pg_dump",
                    "--dbname=sixnine_test",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--no-password",
                    "--no-blobs",
                    "--strict-names",
                    *["--table=" + schema + "." + name for name in sorted(_tables())],
                ]
            )
            self.assertTrue(archive.startswith(b"PGDMP"))
            (source / "postgres-test.dump").write_bytes(
                archive
            )  # Existing private backup ACL.
            run(["createdb", "--maintenance-db=sixnine_test", target_db])
            target_created = True
            sql(target_db, 'CREATE SCHEMA "' + schema + '";')
            run(
                [
                    "pg_restore",
                    "--dbname=" + target_db,
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                ],
                archive,
            )
            self.assertEqual(
                sql(
                    target_db,
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='"
                    + schema
                    + "' AND table_name='platform_accounts';",
                ),
                "0",
            )
            for name, count in manifest["tables"].items():
                self.assertEqual(
                    sql(
                        target_db,
                        'SELECT count(*) FROM "' + schema + '"."' + name + '";',
                    ),
                    str(count),
                )
            # Validate real restored PG media references against the same immutable
            # manifest and byte copies selected from the consistent source snapshot.
            records = sql(
                target_db, 'SELECT record FROM "' + schema + '".platform_assets;'
            ).splitlines()
            expected = {item["key"]: item for item in manifest["objects"]}
            for raw in records:
                asset = json.loads(raw)
                for role in ASSET_OBJECT_ROLES:
                    obj = asset[role]
                    item = expected[obj["key"]]
                    self.assertEqual(obj["sha256"], item["sha256"])
                    self.assertEqual(
                        hashlib.sha256(
                            (source / "media" / item["file"]).read_bytes()
                        ).hexdigest(),
                        item["sha256"],
                    )
            # Test-only isolation after restore; no production PG restore CLI is exposed.
            relation = '"' + schema + '".'
            sql(
                target_db,
                "UPDATE "
                + relation
                + "platform_jobs SET status='recovery_hold',fence=fence+1,lease_worker_id=NULL,lease_expires_at=NULL WHERE status NOT IN ('succeeded','failed','cancelled');"
                "UPDATE "
                + relation
                + "platform_jobs SET execution_plan=(execution_plan::jsonb || '{\"enabled\":false}'::jsonb)::json;"
                "UPDATE "
                + relation
                + "platform_plans SET execution_plan=(execution_plan::jsonb || '{\"enabled\":false}'::jsonb)::json,expires_at=0;"
                "UPDATE "
                + relation
                + "platform_registered_workers SET state='unknown',drain_requested=1,fence=fence+1,expires_at=0 WHERE state!='retired';"
                "UPDATE "
                + relation
                + "platform_registered_devices SET state='unknown' WHERE state!='released';"
                "UPDATE " + relation + "platform_cpu_slots SET state='unknown';"
                "UPDATE "
                + relation
                + "platform_pool_limits SET max_instances=0,max_physical_gpus=0;"
                "UPDATE " + relation + "platform_capacity_approvals SET enabled=0;"
                "UPDATE "
                + relation
                + "platform_capacity_waiters SET state='recovery_hold';"
                "UPDATE "
                + relation
                + "platform_scaler_leaders SET expires_at=0,fence=fence+1;"
                "UPDATE "
                + relation
                + "platform_capacity_gate SET max_instances=0,max_physical_gpus=0;",
            )
            self.assertEqual(
                sql(
                    target_db,
                    "SELECT status FROM "
                    + relation
                    + "platform_jobs WHERE id='"
                    + self.job["id"]
                    + "';",
                ),
                "recovery_hold",
            )
            self.assertEqual(
                sql(
                    target_db,
                    "SELECT execution_plan->>'enabled' FROM "
                    + relation
                    + "platform_jobs WHERE id='"
                    + self.job["id"]
                    + "';",
                ),
                "false",
            )
            self.assertEqual(
                sql(
                    target_db,
                    "SELECT upstream_task_id FROM "
                    + relation
                    + "platform_attempts WHERE id='"
                    + claim.lease.attempt_id
                    + "';",
                ),
                "fake-postgres-upstream-needs-review",
            )
            self.assertEqual(
                sql(
                    target_db,
                    "SELECT state FROM "
                    + relation
                    + "platform_instance_intents WHERE id='"
                    + intent["id"]
                    + "';",
                ),
                "creation_unknown",
            )
            self.assertEqual(
                sql(
                    "sixnine_test",
                    "SELECT status FROM "
                    + relation
                    + "platform_jobs WHERE id='"
                    + self.job["id"]
                    + "';",
                ),
                "running",
            )
        finally:
            if target_created:
                # Exact unique DB created successfully by this test; never sixnine_test.
                self.assertNotEqual(target_db, "sixnine_test")
                run(["dropdb", "--maintenance-db=sixnine_test", target_db])
                self.assertEqual(
                    sql(
                        "sixnine_test",
                        "SELECT count(*) FROM pg_database WHERE datname='"
                        + target_db
                        + "';",
                    ),
                    "0",
                )
            if source_created:
                sql("sixnine_test", 'DROP SCHEMA "' + schema + '" CASCADE;')
                self.assertEqual(
                    sql(
                        "sixnine_test",
                        "SELECT count(*) FROM pg_namespace WHERE nspname='"
                        + schema
                        + "';",
                    ),
                    "0",
                )


if __name__ == "__main__":
    unittest.main()
