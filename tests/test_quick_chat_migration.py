"""Adoption guards on local existing SQLite rows; never a provider or real DB."""

import copy
import json
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, delete, event, insert, select, text, update
from sqlalchemy.engine import make_url

from studio_platform.assets import asset_table
from studio_platform.artifact_writer import write_receipts
from studio_platform.content_lifecycle import history_cleanup_activation_id
from studio_platform.content_references import history_blocker
from studio_platform.identity_schema import users
from studio_platform.auth import Principal
from studio_platform.quick_chat import QuickChatService, events, objects
from studio_platform.quick_chat_history import QuickChatHistoryMixin
from studio_platform.quick_chat_migration import (
    KIND,
    MigrationError,
    apply_staging,
    assert_legacy_mutable,
    migrated_project_id,
    preview,
    rollback_staging,
    _load,
    _analyze,
)
from studio_platform.repository import (
    Conflict,
    artifacts,
    documents,
    request_hash,
    Repository,
    Scope,
    jobs,
)
from studio_platform.storage_asset_journal import receipts
from studio_platform.story_chat import new_story
from studio_platform.guided import empty_project
from studio_platform.source_snapshot import source_snapshot
from tools.migrate_quick_chat_stories import run


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-quick-migration-test-")
        self.bootstrap = None
        postgres = os.environ.get("PLATFORM_TEST_DATABASE_URL")
        if postgres:
            parsed = make_url(postgres)
            if (
                parsed.host not in ("127.0.0.1", "localhost")
                or parsed.database != "sixnine_test"
            ):
                raise RuntimeError("test_database_must_be_explicit_local_sixnine_test")
            self.schema = "ledger_test_" + uuid.uuid4().hex
            self.bootstrap = create_engine(postgres, echo=False, hide_parameters=True)
            with self.bootstrap.begin() as connection:
                connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
            self.url = parsed.update_query_dict(
                {"options": "-csearch_path=" + self.schema}
            )
        else:
            self.url = "sqlite:///" + str(
                Path(self.temp.name) / "migration.sqlite3"
            ).replace("\\", "/")
        self.engine = create_engine(self.url, echo=False, hide_parameters=True)
        for metadata in {
            table.metadata
            for table in (
                objects,
                events,
                documents,
                asset_table,
                users,
                receipts,
                write_receipts,
            )
        }:
            metadata.create_all(self.engine)
        self.settings = SimpleNamespace(
            deployment_target="staging",
            public_origin="https://staging.inkseq.com",
            tenant_id="tenant",
            max_project_bytes=8 * 1024**2,
        )
        self.reader_evidence = {
            "environment": "staging",
            "platform_sha": "a" * 40,
            "frontend_sha": "b" * 40,
            "archive_contract": 2,
            "story_chat_archive": 1,
            "versions_alias": 1,
            "works_archive": 1,
            "self_check_id": "local-fixture-reader-self-check",
        }
        self.project = new_story("project-1", "Original title", "en")
        self.project["journey"].pop("storyChat")
        self.project["journey"].update(
            integration_kind="quick_chat", quickChatSessionId="session-1"
        )
        self.project["integration_kind"] = "quick_chat"
        scene = next(e["id"] for e in self.project["entities"] if e["type"] == "scene")
        self.project["entities"].append(
            dict(
                id="shot-1",
                type="shot",
                parentId=scene,
                title="Original shot",
                description="",
                version=2,
                status="review",
                order=0,
                data={
                    "prompt": "Original prompt",
                    "seconds": 5,
                    "quickChatProjection": {
                        "session_id": "session-1",
                        "revision_id": "revision-1",
                        "item_id": "item-1",
                        "requested_input_hash": "input-hash",
                    },
                },
            )
        )
        with self.engine.begin() as connection:
            connection.execute(
                insert(users).values(
                    id="user-1",
                    tenant="tenant",
                    owner="qa-user",
                    role="user",
                    status="active",
                    display_name="QA user",
                    email_verified=0,
                    created_at=1,
                    updated_at=1,
                )
            )
            connection.execute(
                insert(users).values(
                    id="admin-1",
                    tenant="tenant",
                    owner="qa-admin",
                    role="admin",
                    status="active",
                    display_name="QA admin",
                    email_verified=0,
                    created_at=1,
                    updated_at=1,
                )
            )
            connection.execute(
                insert(documents).values(
                    tenant_id="tenant",
                    owner_id="qa-user",
                    project_id="__projects",
                    kind="project",
                    document_id="project-1",
                    version=4,
                    payload=self.project,
                    updated_at=1,
                )
            )
            self.obj(
                connection,
                "session-1",
                "session",
                {"project_id": "project-1", "title": "Original title", "history": {}},
            )
            self.obj(
                connection, "card-1", "card", {"current_revision_id": "revision-1"}
            )
            self.obj(
                connection,
                "revision-1",
                "revision",
                {
                    "card_id": "card-1",
                    "version": 1,
                    "requested_input_hash": "input-hash",
                    "inputs": {},
                    "items": [
                        {
                            "id": "item-1",
                            "index": 0,
                            "shot_id": "shot-1",
                            "projection_hash": source_snapshot(self.project, "shot-1"),
                        }
                    ],
                },
                parent="card-1",
            )
            self.obj(
                connection,
                "turn-1",
                "turn",
                {
                    "text": "My original question",
                    "reply": "Original answer",
                    "status": "completed",
                    "seq": 1,
                },
            )

    def tearDown(self):
        self.engine.dispose()
        if self.bootstrap:
            with self.bootstrap.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
            self.bootstrap.dispose()
        self.temp.cleanup()

    @staticmethod
    def obj(connection, ident, kind, payload, *, parent=None, session="session-1"):
        connection.execute(
            insert(objects).values(
                id=ident,
                tenant="tenant",
                owner="qa-user",
                session_id=session,
                kind=kind,
                parent_id=parent,
                business_key=ident,
                version=1,
                payload=payload,
                created_at=1,
                updated_at=1,
            )
        )

    def dry(self):
        with self.engine.connect() as connection:
            return preview(connection, "tenant", "qa-user", "session-1")

    def apply(self, connection, dry):
        return apply_staging(
            connection,
            self.settings,
            "qa-user",
            "session-1",
            source_hash=dry["source_hash"],
            project_version=dry["project_version"],
            actor_user_id="admin-1",
            reader_evidence=self.reader_evidence,
            now=2,
        )

    def table_hash(self, table):
        with self.engine.connect() as connection:
            return request_hash(
                [dict(r) for r in connection.execute(select(table)).mappings()]
            )

    def add_revision(
        self,
        *,
        revision_id="revision-2",
        card_id="card-1",
        number=2,
        created_at=2,
        copies=1,
    ):
        with self.engine.begin() as connection:
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
            base = next(e for e in project["entities"] if e["id"] == "shot-1")
            items = []
            for index in range(copies):
                shot = copy.deepcopy(base)
                shot["id"] = revision_id + "-shot-" + str(index)
                shot["order"] = len(project["entities"])
                shot["data"]["prompt"] = "Prompt owned by " + revision_id
                shot["data"]["quickChatProjection"].update(
                    revision_id=revision_id, item_id=revision_id + "-item-" + str(index)
                )
                project["entities"].append(shot)
                items.append(
                    {
                        "id": shot["data"]["quickChatProjection"]["item_id"],
                        "index": index,
                        "shot_id": shot["id"],
                        "projection_hash": source_snapshot(project, shot["id"]),
                    }
                )
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
            if card_id != "card-1":
                self.obj(
                    connection, card_id, "card", {"current_revision_id": revision_id}
                )
            else:
                connection.execute(
                    update(objects)
                    .where(objects.c.id == card_id)
                    .values(payload={"current_revision_id": revision_id})
                )
            self.obj(
                connection,
                revision_id,
                "revision",
                {
                    "card_id": card_id,
                    "version": number,
                    "requested_input_hash": "input-hash",
                    "inputs": {},
                    "items": items,
                },
                parent=card_id,
            )
            connection.execute(
                update(objects)
                .where(objects.c.id == revision_id)
                .values(created_at=created_at)
            )
        return items

    def choose_upload(self, shot_ids):
        with self.engine.begin() as connection:
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
            project["entities"].append(
                {
                    "id": "uploaded-video",
                    "type": "video",
                    "parentId": None,
                    "title": "Original media",
                    "description": "",
                    "version": 1,
                    "status": "draft",
                    "order": 0,
                    "data": {
                        "cloudAssetId": "c" * 32,
                        "fileId": "cloud_asset_" + "c" * 32,
                    },
                }
            )
            connection.execute(
                insert(asset_table).values(
                    id="c" * 32,
                    tenant="tenant",
                    owner="qa-user",
                    project_id="project-1",
                    status="ready",
                    created=1,
                    record=json.dumps({"kind": "video"}),
                )
            )
            for shot in project["entities"]:
                if shot["id"] in shot_ids:
                    shot["data"].update(
                        selectedAssetId="uploaded-video",
                        candidateIds=["uploaded-video"],
                    )
                    shot["version"] += 1
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )

    def test_persisted_old_choice_beats_newer_star_without_changing_its_content(self):
        newest = self.add_revision(copies=2)
        self.choose_upload(["shot-1"])
        with self.engine.begin() as connection:
            self.obj(
                connection,
                "star-1",
                "take_annotation",
                {"starred": True, "item_id": newest[1]["id"]},
            )
        dry = self.dry()
        self.assertEqual(dry["state"], "ready")
        self.assertEqual(dry["counts"]["cards_with_choice"], 1)
        self.assertEqual(
            dry["canonical_shots"]["shot-1"]["selection_reason"], "persisted_selection"
        )
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        canonical = next(e for e in project["entities"] if e["id"] == "shot-1")
        self.assertEqual(canonical["data"]["prompt"], "Original prompt")
        self.assertEqual(canonical["data"]["selectedAssetId"], "uploaded-video")
        self.assertEqual(receipt["counts"]["aliased_takes"], 2)

    def test_multiple_choices_stale_current_and_clock_inversion_refuse(self):
        newest = self.add_revision()
        self.choose_upload(["shot-1", newest[0]["shot_id"]])
        self.assertEqual(self.dry()["refusals"], ["multiple_persisted_choices"])
        with self.engine.begin() as connection:
            connection.execute(
                update(objects)
                .where(objects.c.id == "card-1")
                .values(payload={"current_revision_id": "revision-1"})
            )
        self.assertEqual(self.dry()["refusals"], ["current_revision_not_latest"])
        with self.engine.begin() as connection:
            connection.execute(
                update(objects)
                .where(objects.c.id == "card-1")
                .values(payload={"current_revision_id": "revision-2"})
            )
            connection.execute(
                update(objects).where(objects.c.id == "revision-2").values(created_at=0)
            )
        self.assertEqual(self.dry()["refusals"], ["revision_clock_order_ambiguous"])

    def test_index_zero_is_first_even_when_array_is_shuffled_and_starred(self):
        newest = self.add_revision(copies=2)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(objects.c.payload).where(objects.c.id == "revision-2")
            ).scalar_one()
            row["items"].reverse()
            connection.execute(
                update(objects).where(objects.c.id == "revision-2").values(payload=row)
            )
            self.obj(
                connection,
                "star-1",
                "take_annotation",
                {"starred": True, "item_id": newest[1]["id"]},
            )
        dry = self.dry()
        self.assertEqual(list(dry["canonical_shots"]), [newest[0]["shot_id"]])
        self.assertEqual(dry["counts"]["cards_without_choice"], 1)
        self.assertIsNone(
            dry["canonical_shots"][newest[0]["shot_id"]]["selected_asset_id"]
        )

    def test_card_order_uses_first_revision_time_not_newest_or_chosen(self):
        self.add_revision(
            revision_id="revision-card2", card_id="card-2", number=1, created_at=2
        )
        self.add_revision(created_at=10)
        dry = self.dry()
        with self.engine.begin() as connection:
            self.apply(connection, dry)
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        canonicals = sorted(
            (
                e
                for e in project["entities"]
                if e["data"].get("migration", {}).get("canonical")
            ),
            key=lambda e: e["order"],
        )
        self.assertEqual(
            [e["id"] for e in canonicals],
            ["revision-2-shot-0", "revision-card2-shot-0"],
        )

    def test_empty_factory_placeholder_retained_and_edited_placeholder_refuses(self):
        factory = empty_project(
            {"id": "project-1", "title": "Original title", "workspace": "freestyle"}
        )
        factory["integration_kind"] = "quick_chat"
        factory["journey"].update(
            integration_kind="quick_chat", quickChatSessionId="session-1"
        )
        with self.engine.begin() as connection:
            connection.execute(
                delete(objects).where(objects.c.kind.in_(("card", "revision")))
            )
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=factory)
            )
        dry = self.dry()
        self.assertEqual(dry["state"], "ready")
        self.assertEqual(dry["counts"]["canonical_shots"], 0)
        self.assertEqual(dry["counts"]["retained_shots"], 0)
        self.assertEqual(dry["counts"]["archived_shots"], 1)
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        self.assertFalse(any(e["type"] == "shot" for e in project["entities"]))
        self.assertNotIn("reviewShotId", project["journey"])
        archived = list(receipt["archived_shots"].values())
        self.assertEqual(len(archived), 1)
        self.assertIsNone(archived[0]["projection_hash"])
        self.assertEqual(receipt["before_project"], factory)

    def test_review_focus_maps_to_canonical_without_fabricating_choice(self):
        factory = empty_project(
            {"id": "project-1", "title": "Original title", "workspace": "freestyle"}
        )
        blank = next(e for e in factory["entities"] if e["type"] == "shot")
        with self.engine.begin() as connection:
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
            project["entities"].append(blank)
            project["journey"]["reviewShotId"] = blank["id"]
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
        dry = self.dry()
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
            target = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        self.assertEqual(target["journey"]["reviewShotId"], "shot-1")
        self.assertNotIn(
            "selectedAssetId",
            next(e for e in target["entities"] if e["id"] == "shot-1")["data"],
        )
        self.assertEqual(receipt["counts"]["cards_without_choice"], 1)
        self.assertEqual(receipt["counts"]["review_focus_remapped"], 1)
        self.assertEqual(
            receipt["review_shot_navigation"],
            {"before": blank["id"], "after": "shot-1"},
        )

    def test_missing_readers_and_changed_projection_after_selection_never_write(self):
        dry = self.dry()
        original = self.table_hash(documents)
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "deployed_readers_not_verified"),
        ):
            apply_staging(
                connection,
                self.settings,
                "qa-user",
                "session-1",
                source_hash=dry["source_hash"],
                project_version=dry["project_version"],
                actor_user_id="admin-1",
            )
        self.assertEqual(self.table_hash(documents), original)
        obsolete = {
            key: value
            for key, value in self.reader_evidence.items()
            if key not in {"archive_contract", "story_chat_archive", "works_archive"}
        }
        obsolete.update(story_chat_retained=1, works_retained=1)
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "deployed_readers_not_verified"),
        ):
            apply_staging(
                connection,
                self.settings,
                "qa-user",
                "session-1",
                source_hash=dry["source_hash"],
                project_version=dry["project_version"],
                actor_user_id="admin-1",
                reader_evidence=obsolete,
            )
        self.assertEqual(self.table_hash(documents), original)
        self.choose_upload(["shot-1"])
        with self.engine.begin() as connection:
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
            next(e for e in project["entities"] if e["id"] == "shot-1")["data"][
                "prompt"
            ] = "Illegal edit"
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
        self.assertEqual(self.dry()["refusals"], ["projection_content_changed"])

    def test_apply_writes_only_project_and_original_receipt(self):
        dry = self.dry()
        writes = []

        def observe(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(statement)

        event.listen(self.engine, "before_cursor_execute", observe)
        try:
            with self.engine.begin() as connection:
                self.apply(connection, dry)
        finally:
            event.remove(self.engine, "before_cursor_execute", observe)
        self.assertEqual(len(writes), 2)
        self.assertTrue(all("platform_documents" in statement for statement in writes))

    def rollback(self, connection, receipt):
        return rollback_staging(
            connection,
            self.settings,
            "qa-user",
            "session-1",
            source_hash=receipt["source_hash"],
            actor_user_id="admin-1",
            reason="QA exact restore rehearsal",
            now=4,
        )

    def test_archive_and_rollback_restore_entities_links_positions_and_original_ids(
        self,
    ):
        self.add_revision(copies=2)
        with self.engine.begin() as connection:
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
            project["entities"].append(
                {
                    "id": "reference-image",
                    "type": "image",
                    "parentId": None,
                    "title": "Original ref",
                    "description": "",
                    "version": 1,
                    "status": "draft",
                    "order": 0,
                    "data": {"fileId": "cloud_asset_" + "d" * 32},
                }
            )
            project["links"].append(
                {
                    "id": "original-link",
                    "source": "reference-image",
                    "target": "shot-1",
                    "role": "reference",
                }
            )
            project["layout"]["positions"]["shot-1"] = {"x": 10, "y": 20}
            revision = connection.execute(
                select(objects.c.payload).where(objects.c.id == "revision-1")
            ).scalar_one()
            revision["items"][0]["projection_hash"] = source_snapshot(project, "shot-1")
            connection.execute(
                update(objects)
                .where(objects.c.id == "revision-1")
                .values(payload=revision)
            )
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
        source_rows = self.table_hash(objects)
        dry = self.dry()
        self.assertEqual(dry["state"], "ready")
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
            target = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        self.assertEqual(receipt["archive_strategy"], "receipt_archive")
        self.assertEqual(receipt["before_project"], project)
        self.assertEqual(receipt["archived_links"], project["links"])
        self.assertEqual(receipt["archived_positions"], {"shot-1": {"x": 10, "y": 20}})
        self.assertFalse(target["links"])
        self.assertNotIn("shot-1", target["layout"]["positions"])
        self.assertEqual(
            {e["id"] for e in target["entities"]} | set(receipt["archived_shots"]),
            {e["id"] for e in project["entities"]},
        )
        with self.engine.begin() as connection:
            outcome = self.rollback(connection, receipt)
            restored = (
                connection.execute(
                    select(documents).where(documents.c.kind == "project")
                )
                .mappings()
                .one()
            )
            self.assertEqual(restored["payload"], project)
            self.assertEqual(restored["version"], receipt["after_project_version"] + 1)
            assert_legacy_mutable(connection, "tenant", "qa-user", ["session-1"])
        self.assertEqual(self.table_hash(objects), source_rows)
        self.assertEqual(outcome["rollback"]["actor_user_id"], "admin-1")
        with self.engine.begin() as connection:
            replay = self.rollback(connection, receipt)
        self.assertTrue(replay["replayed"])
        with self.engine.begin() as connection:
            changed = copy.deepcopy(project)
            changed["title"] = "Normal later QuickChat edit"
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=changed, version=7)
            )
            assert_legacy_mutable(connection, "tenant", "qa-user", ["session-1"])
        self.assertEqual(self.dry()["state"], "rolled_back")

    def test_rollback_refuses_after_project_or_source_drift_and_refuses_production(
        self,
    ):
        dry = self.dry()
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
        with self.engine.begin() as connection:
            connection.execute(
                update(documents).where(documents.c.kind == "project").values(version=6)
            )

        before = self.table_hash(documents)
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "rollback_project_changed"),
        ):
            self.rollback(connection, receipt)
        self.assertEqual(self.table_hash(documents), before)
        with self.engine.begin() as connection:
            connection.execute(
                update(documents).where(documents.c.kind == "project").values(version=5)
            )
            connection.execute(
                update(objects).where(objects.c.id == "turn-1").values(version=2)
            )
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "rollback_source_changed"),
        ):
            self.rollback(connection, receipt)
        production = SimpleNamespace(
            **{
                **vars(self.settings),
                "deployment_target": "production",
                "public_origin": "https://inkseq.com",
            }
        )
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(
                MigrationError, "production_rollback_not_authorized"
            ),
        ):
            rollback_staging(
                connection,
                production,
                "qa-user",
                "session-1",
                source_hash=receipt["source_hash"],
                actor_user_id="admin-1",
                reason="never",
            )

    def test_rollback_rejects_new_upload_import_or_job_without_project_changes(self):
        dry = self.dry()
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
            connection.execute(
                insert(receipts).values(
                    id="new-upload",
                    tenant="tenant",
                    owner="qa-user",
                    project_id="project-1",
                    client_key=None,
                    version=1,
                    record=json.dumps({"busy": True}),
                )
            )
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "rollback_work_or_media_changed"),
        ):
            self.rollback(connection, receipt)
        with self.engine.begin() as connection:
            connection.execute(delete(receipts).where(receipts.c.id == "new-upload"))
            connection.execute(
                insert(documents).values(
                    tenant_id="tenant",
                    owner_id="qa-user",
                    project_id="__library",
                    kind="library_import",
                    document_id="new-import",
                    version=1,
                    updated_at=3,
                    payload={
                        "state": "pending",
                        "request": {"target_project_id": "project-1"},
                    },
                )
            )
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "rollback_work_or_media_changed"),
        ):
            self.rollback(connection, receipt)
        with self.engine.begin() as connection:
            connection.execute(
                delete(documents).where(documents.c.document_id == "new-import")
            )
        repo = Repository(self.url, clock=lambda: 3)
        try:
            scope = Scope("tenant", "qa-user", "project-1")
            plan = repo.create_plan(
                scope,
                {"client_ref": {"shot_id": "shot-1"}},
                {"pool": "test-only", "expected_runtime_s": 1},
                expires_at=100,
                estimated_cost_microusd=0,
            )
            repo.create_job(scope, plan["id"], "new-job")
            with (
                self.engine.begin() as connection,
                self.assertRaisesRegex(
                    MigrationError, "rollback_work_or_media_changed"
                ),
            ):
                self.rollback(connection, receipt)
        finally:
            repo.close()

    def test_surviving_entity_cannot_keep_continuation_to_archived_shot(self):
        items = self.add_revision()
        with self.engine.begin() as connection:
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
            shot = next(
                e for e in project["entities"] if e["id"] == items[0]["shot_id"]
            )
            shot["data"]["storyboard"] = {
                "previousFrame": {"shotId": "shot-1", "frameAssetId": "old-frame"}
            }
            revision = connection.execute(
                select(objects.c.payload).where(objects.c.id == "revision-2")
            ).scalar_one()
            revision["items"][0]["projection_hash"] = source_snapshot(
                project, shot["id"]
            )
            connection.execute(
                update(objects)
                .where(objects.c.id == "revision-2")
                .values(payload=revision)
            )
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
        self.assertEqual(
            self.dry()["refusals"], ["archived_shot_has_authoring_reference"]
        )

    def test_migrated_mapping_refuses_forged_before_archive_or_unknown_rollback(self):
        dry = self.dry()
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
        for change in ("before_project", "archive_strategy", "rollback"):
            bad = copy.deepcopy(receipt)
            bad.pop("replayed", None)
            if change == "before_project":
                bad["before_project"]["title"] = "Changed after protected receipt"
            elif change == "archive_strategy":
                bad["archive_strategy"] = "retained_marker"
            else:
                bad["rollback"] = {"status": "pending"}
            with self.engine.begin() as connection:
                connection.execute(
                    update(documents)
                    .where(documents.c.kind == KIND)
                    .values(payload=bad)
                )
                session = dict(
                    connection.execute(
                        select(objects).where(objects.c.id == "session-1")
                    )
                    .mappings()
                    .one()
                )
                self.assertIsNone(
                    migrated_project_id(connection, "tenant", "qa-user", session)
                )
                with self.assertRaisesRegex(Conflict, "quick_chat_migrated_read_only"):
                    assert_legacy_mutable(
                        connection, "tenant", "qa-user", ["session-1"]
                    )

    def test_verified_generated_media_provenance_survives_but_forgery_refuses(self):
        self.add_revision()
        with self.engine.connect() as connection:
            original = _load(connection, "tenant", "qa-user", "session-1")
        media = {
            "id": "generated-media",
            "type": "video",
            "parentId": None,
            "title": "Original generated output",
            "description": "",
            "version": 1,
            "status": "draft",
            "order": 0,
            "data": {
                "cloudArtifactId": "artifact-1",
                "sourceJobId": "job-1",
                "sourceShotId": "shot-1",
                "sourceShotVersion": 2,
                "sourceHash": "f" * 64,
            },
        }
        original["project"]["payload"]["entities"].append(media)
        original["jobs"] = [
            {
                "id": "job-1",
                "tenant_id": "tenant",
                "owner_id": "qa-user",
                "project_id": "project-1",
                "status": "succeeded",
                "lease_worker_id": None,
                "lease_expires_at": None,
                "request": {
                    "client_ref": {
                        "shot_id": "shot-1",
                        "shot_version": 2,
                        "source_hash": "f" * 64,
                    }
                },
            }
        ]
        original["artifacts"] = [
            {"id": "artifact-1", "job_id": "job-1", "metadata": {"kind": "video"}}
        ]
        result = _analyze(original, max_project_bytes=self.settings.max_project_bytes)
        self.assertEqual(
            next(e for e in result["project"]["entities"] if e["id"] == media["id"]),
            media,
        )
        self.assertIn("shot-1", result["archived_shots"])
        for field in ("owner", "job", "version", "hash", "nested_reference"):
            changed = copy.deepcopy(original)
            entity = next(
                e
                for e in changed["project"]["payload"]["entities"]
                if e["id"] == media["id"]
            )
            if field == "owner":
                changed["jobs"][0]["owner_id"] = "someone-else"
            elif field == "job":
                entity["data"]["sourceJobId"] = "not-this-job"
            elif field == "version":
                entity["data"]["sourceShotVersion"] = 999
            elif field == "hash":
                entity["data"]["sourceHash"] = "forged"
            else:
                entity["data"]["continuation"] = {"previousShotId": "shot-1"}
            with self.assertRaisesRegex(
                MigrationError,
                "archived_media_provenance_unverified|archived_shot_has_authoring_reference",
            ):
                _analyze(changed, max_project_bytes=self.settings.max_project_bytes)

    def test_archive_size_child_and_authoring_references_refuse_before_writes(self):
        newest = self.add_revision(copies=2)
        with self.engine.connect() as connection:
            project = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        limit = (
            len(json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode())
            + 100
        )
        with self.engine.connect() as connection:
            dry = preview(
                connection, "tenant", "qa-user", "session-1", max_project_bytes=limit
            )
        self.assertEqual(dry["refusals"], ["archive_receipt_too_large"])
        project["journey"]["soundTracks"] = {
            "chapter": [{"shotId": "shot-1", "offset": 0}]
        }
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
        self.assertEqual(
            self.dry()["refusals"], ["archived_shot_has_authoring_reference"]
        )
        project["journey"].pop("soundTracks")
        project["entities"].append(
            {
                "id": "shot-note",
                "type": "note",
                "parentId": "shot-1",
                "title": "Keep child",
                "description": "",
                "version": 1,
                "status": "draft",
                "order": 0,
                "data": {},
            }
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
        self.assertEqual(self.dry()["refusals"], ["archived_shot_has_children"])

    def test_refused_owner_census_is_complete_and_read_only(self):
        if self.engine.dialect.name != "sqlite":
            self.skipTest(
                "SQLite byte-for-byte readonly file guard; PostgreSQL transactions run the same core above"
            )
        with self.engine.begin() as connection:
            connection.execute(
                update(objects)
                .where(objects.c.id == "card-1")
                .values(payload={"current_revision_id": "missing"})
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.db"
            source = self.engine.raw_connection()
            target = sqlite3.connect(path)
            source.driver_connection.backup(target)
            target.close()
            source.close()
            original = path.read_bytes()
            settings = SimpleNamespace(
                **{**vars(self.settings), "database_url": "sqlite:///" + str(path)}
            )
            args = SimpleNamespace(
                environment="staging",
                apply=False,
                owner=["qa-user"],
                session=None,
                limit=500,
            )
            result = run(args, settings)
            self.assertEqual(path.read_bytes(), original)
        counts = result["accounts"]["qa-user"]
        self.assertEqual(counts["raw"]["cards"], 1)
        self.assertEqual(counts["raw"]["takes"], 1)
        self.assertEqual(counts["raw"]["text_turns"], 1)
        self.assertEqual(counts["refusal_reasons"], {"current_revision_missing": 1})
        self.assertEqual(counts["refused"], 1)
        self.assertEqual(len(result["snapshot_hash"]), 64)

    def test_concurrent_exact_apply_is_one_conversion_and_one_original_replay(self):
        dry = self.dry()

        def convert(_):
            with self.engine.begin() as connection:
                return self.apply(connection, dry)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(convert, range(2)))
        self.assertEqual(sorted(value["replayed"] for value in outcomes), [False, True])
        self.assertEqual(outcomes[0]["source_hash"], outcomes[1]["source_hash"])
        with self.engine.connect() as connection:
            versions = list(
                connection.execute(
                    select(documents.c.version).where(documents.c.kind == "project")
                ).scalars()
            )
            receipts = list(
                connection.execute(
                    select(documents).where(documents.c.kind == KIND)
                ).mappings()
            )
        self.assertEqual(versions, [5])
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["version"], 1)

    def test_selected_media_requires_ready_upload_or_succeeded_undeleted_artifact(self):
        self.choose_upload(["shot-1"])
        with self.engine.begin() as connection:
            connection.execute(update(asset_table).values(status="purged"))
        self.assertEqual(self.dry()["refusals"], ["selected_media_binding_unknown"])
        with self.engine.connect() as connection:
            closure = _load(connection, "tenant", "qa-user", "session-1")
        project = closure["project"]["payload"]
        media = next(e for e in project["entities"] if e["id"] == "uploaded-video")
        media["data"] = {"cloudArtifactId": "artifact-1"}
        closure["jobs"] = [
            {
                "id": "job-1",
                "status": "succeeded",
                "lease_worker_id": None,
                "lease_expires_at": None,
                "request": {"client_ref": {"shot_id": "shot-1"}},
            }
        ]
        closure["artifacts"] = [
            {"id": "artifact-1", "job_id": "job-1", "metadata": {"kind": "video"}}
        ]
        self.assertEqual(
            _analyze(closure, max_project_bytes=self.settings.max_project_bytes)[
                "counts"
            ]["cards_with_choice"],
            1,
        )
        for status, kind, deleted in (
            ("failed", "video", False),
            ("cancelled", "video", False),
            ("succeeded", "image", False),
            ("succeeded", "video", True),
        ):
            closure["jobs"][0]["status"] = status
            closure["artifacts"][0]["metadata"] = {
                "kind": kind,
                "content_deleted": deleted,
            }
            with self.assertRaisesRegex(
                MigrationError, "selected_artifact_binding_unknown"
            ):
                _analyze(closure, max_project_bytes=self.settings.max_project_bytes)

    def test_selection_normalization_does_not_hide_title_seed_or_ref_drift(self):
        self.choose_upload(["shot-1"])
        with self.engine.connect() as connection:
            original = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        for field in ("title", "seed", "refs", "parent", "order"):
            changed = copy.deepcopy(original)
            shot = next(e for e in changed["entities"] if e["id"] == "shot-1")
            if field == "title":
                shot["title"] = "Unexpected title"
            elif field == "seed":
                shot["data"]["h3"] = {"controls": {"seed": "99"}}
            elif field == "refs":
                shot["data"]["referenceRanges"] = {
                    "unaccepted": {"start": 2, "duration": 1}
                }
            elif field == "parent":
                shot["parentId"] = None
            else:
                shot["order"] = 999
            with self.engine.begin() as connection:
                connection.execute(
                    update(documents)
                    .where(documents.c.kind == "project")
                    .values(payload=changed)
                )
            self.assertEqual(
                self.dry()["refusals"], ["projection_content_changed"], field
            )

    def test_edited_factory_placeholder_is_not_archived(self):
        factory = empty_project(
            {"id": "project-1", "title": "Original title", "workspace": "freestyle"}
        )
        factory["integration_kind"] = "quick_chat"
        factory["journey"].update(
            integration_kind="quick_chat", quickChatSessionId="session-1"
        )
        next(e for e in factory["entities"] if e["type"] == "shot")["data"][
            "prompt"
        ] = "Do not lose this user content"
        with self.engine.begin() as connection:
            connection.execute(
                delete(objects).where(objects.c.kind.in_(("card", "revision")))
            )
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=factory)
            )
        self.assertEqual(
            self.dry()["refusals"], ["unmapped_existing_shots_require_review"]
        )

    def test_preview_read_only_and_atomic_replay_preserves_original_ids(self):
        originals = self.table_hash(objects)
        dry = self.dry()
        self.assertEqual(dry["state"], "ready")
        self.assertEqual(dry["counts"]["messages"], 2)
        self.assertEqual(self.table_hash(objects), originals)
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
        self.assertEqual(self.table_hash(objects), originals)
        self.assertEqual(receipt["actor_user_id"], "admin-1")
        with self.engine.begin() as connection:
            replay = self.apply(connection, dry)
            project = connection.execute(
                select(documents.c.payload).where(
                    documents.c.document_id == "project-1",
                    documents.c.kind == "project",
                )
            ).scalar_one()
            source = dict(
                connection.execute(select(objects).where(objects.c.id == "session-1"))
                .mappings()
                .one()
            )
            self.assertEqual(
                migrated_project_id(connection, "tenant", "qa-user", source),
                "project-1",
            )
            self.assertIsNone(
                migrated_project_id(connection, "tenant", "other-owner", source)
            )
            self.assertEqual(
                [e["id"] for e in project["entities"]],
                [e["id"] for e in self.project["entities"]],
            )
            canonical = project["entities"][-1]
            self.assertEqual(
                canonical["data"]["prompt"],
                self.project["entities"][-1]["data"]["prompt"],
            )
            self.assertEqual(
                canonical["data"]["migration"]["source_shot_ids"], ["shot-1"]
            )
            self.assertEqual(
                receipt["canonical_shots"]["shot-1"]["selected_asset_id"], None
            )
            self.assertEqual(project["journey"]["storyChat"]["turns"], 1)
            self.assertTrue(
                all(m["imported"] for m in project["journey"]["storyChat"]["messages"])
            )
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.dry()["state"], "already_migrated")

    def test_production_refuses_before_engine_or_data_access(self):
        settings = SimpleNamespace(
            **{
                **vars(self.settings),
                "deployment_target": "production",
                "public_origin": "https://inkseq.com",
            }
        )
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "production_apply_not_authorized"),
        ):
            apply_staging(
                connection,
                settings,
                "qa-user",
                "session-1",
                source_hash="unused",
                project_version=4,
                actor_user_id="admin-1",
            )
        args = SimpleNamespace(environment="production", apply=True)
        with (
            patch("tools.migrate_quick_chat_stories.create_engine") as factory,
            self.assertRaisesRegex(MigrationError, "production_apply_not_authorized"),
        ):
            run(args, settings)
        factory.assert_not_called()

    def test_source_drift_and_unverified_admin_refuse_without_writes(self):
        dry = self.dry()
        with self.engine.begin() as connection:
            connection.execute(
                update(objects).where(objects.c.id == "turn-1").values(version=2)
            )
        before = self.table_hash(documents)
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "migration_source_changed"),
        ):
            self.apply(connection, dry)
        self.assertEqual(self.table_hash(documents), before)
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(MigrationError, "active_admin_actor_required"),
        ):
            apply_staging(
                connection,
                self.settings,
                "qa-user",
                "session-1",
                source_hash=dry["source_hash"],
                project_version=4,
                actor_user_id="not-a-user",
            )

    def test_rollback_between_project_and_receipt_keeps_original(self):
        dry, original = self.dry(), self.table_hash(documents)
        with (
            self.assertRaisesRegex(RuntimeError, "simulated_crash"),
            self.engine.begin() as connection,
        ):
            self.apply(connection, dry)
            raise RuntimeError("simulated_crash")
        self.assertEqual(self.table_hash(documents), original)
        self.assertEqual(self.dry()["state"], "ready")

    def test_multitake_or_unmapped_placeholder_requires_review(self):
        project = copy.deepcopy(self.project)
        extra = copy.deepcopy(project["entities"][-1])
        extra["id"] = "legacy-blank"
        extra["data"] = {"prompt": "", "seconds": 5}
        project["entities"].append(extra)
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
        dry = self.dry()
        self.assertIn("unmapped_existing_shots_require_review", dry["refusals"])
        before = self.table_hash(documents)
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(
                MigrationError,
                "unmapped_existing_shots_requires_review|unmapped_existing_shots_require_review",
            ),
        ):
            self.apply(connection, dry)
        self.assertEqual(self.table_hash(documents), before)

    def test_multiple_revision_takes_group_under_current_first_item(self):
        project = copy.deepcopy(self.project)
        take = copy.deepcopy(project["entities"][-1])
        take["id"] = "shot-2"
        take["data"]["quickChatProjection"].update(
            revision_id="revision-2", item_id="item-2"
        )
        project["entities"].append(take)
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=project)
            )
            self.obj(
                connection,
                "revision-2",
                "revision",
                {
                    "card_id": "card-1",
                    "version": 2,
                    "requested_input_hash": "input-hash",
                    "inputs": {},
                    "items": [
                        {
                            "id": "item-2",
                            "index": 0,
                            "shot_id": "shot-2",
                            "projection_hash": source_snapshot(project, "shot-2"),
                        }
                    ],
                },
                parent="card-1",
            )
            connection.execute(
                update(objects)
                .where(objects.c.id == "card-1")
                .values(payload={"current_revision_id": "revision-2"})
            )
        dry = self.dry()
        self.assertEqual(dry["counts"]["takes"], 2)
        self.assertEqual(dry["counts"]["cards"], 1)
        self.assertEqual(dry["state"], "ready")
        self.assertEqual(dry["counts"]["canonical_shots"], 1)
        self.assertEqual(dry["counts"]["retained_shots"], 0)
        self.assertEqual(dry["counts"]["archived_shots"], 1)
        self.assertEqual(
            dry["canonical_shots"]["shot-2"]["source_shot_ids"], ["shot-1", "shot-2"]
        )
        self.assertIsNone(dry["canonical_shots"]["shot-2"]["selected_asset_id"])
        with self.engine.begin() as connection:
            receipt = self.apply(connection, dry)
            target = connection.execute(
                select(documents.c.payload).where(documents.c.kind == "project")
            ).scalar_one()
        shots = {e["id"]: e for e in target["entities"] if e["type"] == "shot"}
        self.assertNotIn("shot-1", shots)
        self.assertEqual(
            receipt["archived_shots"]["shot-1"]["entity"], self.project["entities"][-1]
        )
        self.assertNotIn("selectedAssetId", shots["shot-2"]["data"])

    def test_get_mapping_is_additive_and_legacy_writes_refuse(self):
        service = QuickChatService.__new__(QuickChatService)
        service.repo, service.tenant = (
            SimpleNamespace(engine=self.engine, clock=lambda: 5),
            "tenant",
        )
        service._session_public = lambda row: {
            "id": row["id"],
            "title": row["payload"]["title"],
        }
        principal = Principal("qa-user", "browser")
        before = service.get_session(principal, "session-1")["session"]
        self.assertIsNone(before["migrated_project_id"])
        dry = self.dry()
        with self.engine.begin() as connection:
            self.apply(connection, dry)
            self.obj(
                connection,
                "preflight-old",
                "preflight",
                {"expires_at": 4, "status": "ready"},
            )
        after = service.get_session(principal, "session-1")["session"]
        self.assertEqual(after, {**before, "migrated_project_id": "project-1"})
        self.assertTrue(
            service.get_preflight(principal, "session-1", "preflight-old")["stale"]
        )
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(Conflict, "quick_chat_migrated_read_only"),
        ):
            service._access(principal, "session-1", "projects:write", conn=connection)
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == KIND)
                .values(payload={"status": "completed"})
            )
        self.assertIsNone(
            service.get_session(principal, "session-1")["session"][
                "migrated_project_id"
            ]
        )
        with (
            self.engine.begin() as connection,
            self.assertRaisesRegex(Conflict, "quick_chat_migrated_read_only"),
        ):
            service._access(principal, "session-1", "projects:write", conn=connection)

    def test_pending_old_cleanup_and_unknown_turn_are_not_adopted(self):
        with self.engine.begin() as connection:
            self.obj(
                connection,
                "action-1",
                "history_action",
                {"state": "pending", "plan": {"session_ids": ["session-1"]}},
                session="__history",
            )
        self.assertEqual(self.dry()["refusals"], ["history_action_unresolved"])
        with self.engine.begin() as connection:
            connection.execute(
                update(objects)
                .where(objects.c.id == "action-1")
                .values(payload={"state": "completed"})
            )
            connection.execute(
                update(objects)
                .where(objects.c.id == "turn-1")
                .values(payload={"status": "unknown"})
            )
        self.assertEqual(self.dry()["refusals"], ["authoring_operation_unresolved"])

    def test_projection_input_drift_and_unknown_schema_refuse(self):
        changed = copy.deepcopy(self.project)
        changed["entities"][-1]["data"]["prompt"] = "Modified outside original revision"
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=changed)
            )
        self.assertEqual(self.dry()["refusals"], ["projection_content_changed"])
        changed["schemaVersion"] = 5
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.kind == "project")
                .values(payload=changed)
            )
        self.assertEqual(self.dry()["refusals"], ["project_schema_unsupported"])

    def test_completed_receipt_fences_legacy_purge_before_storage_io(self):
        with self.engine.begin() as connection:
            self.apply(connection, self.dry())
            self.obj(
                connection,
                history_cleanup_activation_id("tenant"),
                "history_cleanup_activation",
                {"version": 1, "enabled": True, "activated_release": "a" * 40},
                session="__history_system",
            )
            connection.execute(
                update(objects)
                .where(objects.c.kind == "history_cleanup_activation")
                .values(owner="__system__", business_key="v1")
            )
            self.obj(
                connection,
                "purge-1",
                "history_action",
                {
                    "state": "pending",
                    "plan": {"session_ids": ["session-1"], "objects": []},
                },
                session="__history",
            )
            with self.assertRaisesRegex(Conflict, "quick_chat_migrated_read_only"):
                assert_legacy_mutable(connection, "tenant", "qa-user", ["session-1"])
            source = dict(
                connection.execute(select(objects).where(objects.c.id == "session-1"))
                .mappings()
                .one()
            )
            # The shared operational blocker is also used by Works trash and
            # account deletion. Migration must fence legacy history only.
            self.assertIsNone(
                history_blocker(
                    connection, SimpleNamespace(tenant="tenant"), "qa-user", source
                )
            )
        repo = SimpleNamespace(
            transaction=self.engine.begin,
            _locked=lambda c, q: c.execute(q).mappings().one(),
        )
        service = QuickChatHistoryMixin()
        service.repo, service.tenant, service.storage = repo, "tenant", Mock()
        record = {"id": "purge-1", "owner": "qa-user"}
        with self.assertRaisesRegex(Conflict, "quick_chat_migrated_read_only"):
            service._cleanup_one_object_locked(record, 0)
        service.storage.assert_not_called()
        with self.assertRaisesRegex(Conflict, "quick_chat_migrated_read_only"):
            service._finish_history_purge(record)

    def test_dry_operator_never_creates_missing_sqlite_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.db"
            settings = SimpleNamespace(
                **{**vars(self.settings), "database_url": "sqlite:///" + str(path)}
            )
            args = SimpleNamespace(environment="staging", apply=False)
            with self.assertRaisesRegex(MigrationError, "existing_database_required"):
                run(args, settings)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
