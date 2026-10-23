"""Closed production-entry guards against isolated test rows only."""

import copy
import hashlib
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from studio_platform.quick_chat_migration import (
    KIND,
    MigrationError,
    _load,
    assert_legacy_mutable,
    migrated_project_id,
)
from studio_platform.quick_chat_production_migration import (
    SOURCE_FILES,
    apply_production_approved,
    load_manifest,
)
from studio_platform.repository import Conflict, documents, request_hash
from studio_platform.quick_chat import objects
from studio_platform.universal_trash import assert_project_archive_purge_available
import test_quick_chat_migration as fixtures
from tools import apply_approved_quick_chat_stories as cli


class ProductionMigrationTests(unittest.TestCase):
    obj = staticmethod(fixtures.MigrationTests.obj)
    table_hash = fixtures.MigrationTests.table_hash
    dry = fixtures.MigrationTests.dry
    tearDown = fixtures.MigrationTests.tearDown

    def setUp(self):
        fixtures.MigrationTests.setUp(self)
        self.settings.deployment_target = "production"
        self.settings.public_origin = "https://inkseq.com"
        self.settings.database_url = "postgresql+psycopg://local/sixnine_test"
        dry = self.dry()
        with self.engine.connect() as connection:
            closure = _load(connection, "tenant", "qa-user", "session-1")
        row = {
            key: dry[key]
            for key in (
                "owner",
                "session_id",
                "project_id",
                "session_version",
                "project_version",
                "source_hash",
                "project_hash",
                "target_hash",
                "counts",
                "archive_payload_bytes",
            )
        }
        row.update(
            index=1,
            session_row_hash=request_hash(closure["session"]),
            raw_children_hash=request_hash(
                sorted(closure["objects"], key=lambda item: (item["kind"], item["id"]))
            ),
        )
        rows = [row]
        for index in range(2, 18):
            rows.append(
                {
                    **copy.deepcopy(row),
                    "index": index,
                    "owner": "qa-user" if index <= 4 else "other-owner",
                    "session_id": "approved-session-" + str(index),
                    "project_id": "approved-project-" + str(index),
                }
            )
        self.manifest = {
            "environment": "production",
            "origin": "https://inkseq.com",
            "batch_size": 17,
            "automatic_retry": False,
            "production_apply_performed": False,
            "owner_pre_authorization_comment_id": 6369552441,
            "claude_number_acceptance_comment_id": 6370536037,
            "source_receipt": {
                "sha256": "1" * 64,
                "snapshot_hash": "2" * 64,
                "original_command_id": "original-dry-run",
            },
            "per_owner": {"qa-user": 4, "other-owner": 13},
            "ready": rows,
            "excluded": [
                {
                    "owner": "qa-user",
                    "session_id": "excluded-session-" + str(n),
                    "project_id": "excluded-project-" + str(n),
                }
                for n in range(23)
            ],
        }
        reader = {
            "platform_sha": "a" * 40,
            "frontend_sha": "b" * 40,
            "image_id": "sha256:" + "c" * 64,
            "archive_contract": 2,
            "story_chat_archive": 1,
            "versions_alias": 1,
            "works_archive": 1,
            "legacy_write_fence": 1,
            "permanent_purge_guard": 1,
            "promotion_receipt_sha256": "d" * 64,
            "self_check_id": "original-check",
        }
        root = Path(__file__).resolve().parents[1]
        self.proof = {
            "environment": "production",
            "origin": "https://inkseq.com",
            "tenant_id": "tenant",
            "actor_user_id": "admin-1",
            "operation_id": "operation-1",
            "batch_operation_id": "batch-1",
            "manifest_sha256": self.manifest_hash(),
            "checked_at": time.time(),
            "current": reader,
            "rollback": {**reader, "platform_sha": "e" * 40},
            "source_sha256": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in SOURCE_FILES
            },
        }

    def manifest_raw(self):
        return json.dumps(self.manifest).encode()

    def manifest_hash(self):
        return hashlib.sha256(self.manifest_raw()).hexdigest()

    def apply(self, connection, **overrides):
        args = dict(
            manifest_raw=self.manifest_raw(),
            manifest_sha256=self.manifest_hash(),
            actor_user_id="admin-1",
            operation_id="operation-1",
            reader_evidence=self.proof,
        )
        args.update(overrides)
        return apply_production_approved(
            connection, self.settings, "qa-user", "session-1", **args
        )

    def assert_refused(self, code):
        before = self.table_hash(documents)
        with (
            self.assertRaisesRegex(MigrationError, code),
            self.engine.begin() as connection,
        ):
            self.apply(connection)
        self.assertEqual(before, self.table_hash(documents))

    def test_atomic_conversion_preserves_original_source_and_retained_readers(self):
        source = self.table_hash(objects)
        original = copy.deepcopy(self.project)
        with self.engine.begin() as connection:
            receipt = self.apply(connection)
        self.assertEqual(self.table_hash(objects), source)
        self.assertEqual(receipt["before_project"], original)
        self.assertEqual(receipt["contract"], 1)
        self.assertEqual(receipt["mapping_version"], 2)
        self.assertEqual(
            receipt["after_project_hash"], self.manifest["ready"][0]["target_hash"]
        )
        self.assertEqual(
            receipt["production_approval"]["manifest_sha256"], self.manifest_hash()
        )
        with self.engine.begin() as connection:
            session = (
                connection.execute(select(objects).where(objects.c.id == "session-1"))
                .mappings()
                .one()
            )
            # Unchanged accepted readers ignore additive production evidence.
            self.assertEqual(
                migrated_project_id(connection, "tenant", "qa-user", session),
                "project-1",
            )
            with self.assertRaisesRegex(Conflict, "quick_chat_migrated_read_only"):
                assert_legacy_mutable(connection, "tenant", "qa-user", ["session-1"])
            with self.assertRaisesRegex(Conflict, "migrated_project_archive_retained"):
                assert_project_archive_purge_available(
                    connection, "tenant", "qa-user", "project-1"
                )
        self.assert_refused("production_receipt_exists_reconcile_only")
        public = cli._summary(receipt)
        self.assertNotIn("before_project", public)
        self.assertNotIn("mapping", public)
        self.assertNotIn("My original question", json.dumps(public))
        self.assertNotIn("Original prompt", json.dumps(public))

    def test_environment_and_active_same_tenant_admin(self):
        for field, value in (
            ("deployment_target", "staging"),
            ("public_origin", "https://staging.inkseq.com"),
        ):
            with self.subTest(field=field):
                old = getattr(self.settings, field)
                setattr(self.settings, field, value)
                self.assert_refused("production_environment_required")
                setattr(self.settings, field, old)
        with (
            self.assertRaisesRegex(MigrationError, "active_admin_actor_required"),
            self.engine.begin() as connection,
        ):
            self.proof["actor_user_id"] = "user-1"
            self.apply(connection, actor_user_id="user-1")

    def test_closed_manifest_membership_exclusions_and_hash_pin(self):
        original = copy.deepcopy(self.manifest)
        changes = (
            lambda m: m["ready"].append(copy.deepcopy(m["ready"][0])),
            lambda m: m["ready"][1].update(session_id="session-1"),
            lambda m: m["excluded"][0].update(session_id="session-1"),
            lambda m: m.update(automatic_retry=True),
            lambda m: m.update(claude_number_acceptance_comment_id=1),
        )
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.manifest = copy.deepcopy(original)
                change(self.manifest)
                self.proof["manifest_sha256"] = self.manifest_hash()
                self.assert_refused("production_")
        self.manifest = original
        with self.assertRaisesRegex(
            MigrationError, "production_manifest_hash_mismatch"
        ):
            load_manifest(self.manifest_raw(), "f" * 64)
        for owner, session in (
            ("other-owner", "session-1"),
            ("qa-user", "excluded-session-0"),
            ("qa-user", "unapproved-18"),
        ):
            with (
                self.subTest(owner=owner, session=session),
                self.assertRaisesRegex(
                    MigrationError, "production_session_not_approved"
                ),
                self.engine.begin() as connection,
            ):
                apply_production_approved(
                    connection,
                    self.settings,
                    owner,
                    session,
                    manifest_raw=self.manifest_raw(),
                    manifest_sha256=self.manifest_hash(),
                    actor_user_id="admin-1",
                    operation_id="operation-1",
                    reader_evidence=self.proof,
                )

    def test_reader_and_runtime_binding_negatives(self):
        original = copy.deepcopy(self.proof)
        changes = (
            lambda p: p.update(environment="staging"),
            lambda p: p.update(origin="https://staging.inkseq.com"),
            lambda p: p.update(tenant_id="other-tenant"),
            lambda p: p.update(operation_id="other-operation"),
            lambda p: p.update(checked_at=time.time() - 901),
            lambda p: p.update(checked_at=time.time() + 30),
            lambda p: p["rollback"].update(permanent_purge_guard=0),
            lambda p: p["rollback"].update(legacy_write_fence=0),
            lambda p: p["rollback"].update(platform_sha=p["current"]["platform_sha"]),
            lambda p: p["current"].update(archive_contract=True),
            lambda p: p["current"].update(promotion_receipt_sha256="missing"),
            lambda p: p["source_sha256"].pop(SOURCE_FILES[0]),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.proof = copy.deepcopy(original)
                change(self.proof)
                self.assert_refused("production_")

    def test_locked_original_target_counts_and_size_are_not_rebased(self):
        original = copy.deepcopy(self.manifest)
        for field, value in (
            ("target_hash", "f" * 64),
            ("project_hash", "f" * 64),
            ("session_row_hash", "f" * 64),
            ("raw_children_hash", "f" * 64),
            ("session_version", 99),
            ("project_version", 99),
            ("counts", {"cards": 999}),
            ("archive_payload_bytes", 1),
            ("source_hash", "f" * 64),
        ):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(original)
                self.manifest["ready"][0][field] = value
                self.proof["manifest_sha256"] = self.manifest_hash()
                self.assert_refused(
                    "migration_source_changed|production_approved_row_changed"
                )

    def test_transaction_failure_after_project_update_rolls_back_both(self):
        before = self.table_hash(documents)
        with patch(
            "studio_platform.quick_chat_migration.insert",
            side_effect=RuntimeError("simulated failure"),
        ):
            with (
                self.assertRaisesRegex(RuntimeError, "simulated failure"),
                self.engine.begin() as connection,
            ):
                self.apply(connection)
        self.assertEqual(before, self.table_hash(documents))

    def test_existing_malformed_receipt_and_later_edits_never_replay(self):
        with self.engine.begin() as connection:
            self.apply(connection)
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(version=99)
            )
            connection.execute(
                update(documents)
                .where(documents.c.kind == KIND)
                .values(payload={"unknown": "sensitive payload"})
            )
        self.assert_refused("production_receipt_exists_reconcile_only")

    def arguments(self):
        folder = Path(self.temp.name)
        manifest = folder / "manifest.json"
        proof = folder / "proof.json"
        manifest.write_bytes(self.manifest_raw())
        proof.write_text(json.dumps(self.proof), encoding="utf-8")
        return SimpleNamespace(
            owner="qa-user",
            session="session-1",
            manifest=str(manifest),
            manifest_sha256=self.manifest_hash(),
            reader_evidence=str(proof),
            reader_evidence_sha256=hashlib.sha256(proof.read_bytes()).hexdigest(),
            actor_user_id="admin-1",
            operation_id="operation-1",
            result=str(folder / "result.json"),
        )

    def test_cli_qualifies_source_before_database_and_records_unknown_without_replay(
        self,
    ):
        args = self.arguments()
        cli.prepare(args, self.settings)
        self.proof["source_sha256"][SOURCE_FILES[0]] = "0" * 64
        bad = self.arguments()
        with self.assertRaisesRegex(MigrationError, "production_entry_source_changed"):
            cli.prepare(bad, self.settings)
        # Use only mocked execution: no external DB/API/credential access.
        argv = [
            part
            for key, value in vars(args).items()
            for part in ("--" + key.replace("_", "-"), value)
        ]
        with (
            patch.object(cli.Settings, "from_environment", return_value=self.settings),
            patch.object(cli, "prepare", return_value=object()),
            patch.object(
                cli, "execute", side_effect=RuntimeError("secret database details")
            ) as execute,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(cli.main(argv), 2)
            self.assertNotIn("secret database details", stdout.getvalue())
            self.assertEqual(
                json.loads(Path(args.result).read_text())["status"],
                "started_reconcile_before_retry",
            )
            self.assertEqual(cli.main(argv), 2)
            self.assertEqual(execute.call_count, 1)

    def test_cli_persists_original_success_before_stdout(self):
        args = self.arguments()
        argv = [
            part
            for key, value in vars(args).items()
            for part in ("--" + key.replace("_", "-"), value)
        ]
        with (
            patch.object(cli.Settings, "from_environment", return_value=self.settings),
            patch.object(cli, "prepare", return_value=object()),
            patch.object(cli, "execute", return_value={"receipt_id": "session-1"}),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(cli.main(argv), 0)
        result = json.loads(Path(args.result).read_text())
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["operation_id"], "operation-1")
        self.assertEqual(result["result"], {"receipt_id": "session-1"})


if __name__ == "__main__":
    unittest.main()
