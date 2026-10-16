"""Creation lifecycle on the original ledger; SQLite or guarded local Postgres."""
import copy
import hashlib
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy import insert, select, update

import test_platform_repository as ledger
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.assets import asset_table
from studio_platform.content_lifecycle import (history_cleanup_activation_id, owner_reference_gate,
                                               put_tombstone, reference_ids)
from studio_platform.quick_chat import QuickChatError, objects
from studio_platform.repository import (Conflict, Scope, artifacts, attempts, budget_accounts,
    budget_reservations, documents, jobs, plans)
from studio_platform.settings import Settings
from studio_platform.storage import ObjectNotFound
from studio_platform.storage_asset_journal import receipts


class CreationHistoryTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(Settings(Path(self.temp.name)/"history", tenant_id=self.scope.tenant_id,
            auth_mode="local-test", generation_enabled=True, execution_backend="mock", database_url=self.url),
            repository=self.repo)
        self.service = self.app.state.quick_chat
        self.principal = Principal(self.scope.owner_id, "browser")
        self.session = self.new_session("original")
        self.sid = self.session["id"]

    def new_session(self, key):
        return self.service.create_session(self.principal, {"title": key, "is_test": True}, key)["session"]

    def row(self, sid=None):
        with self.repo.engine.connect() as conn:
            return self.service._get(conn, self.principal, sid or self.sid, kind="session")

    def activate(self):
        with self.repo.transaction() as conn:
            conn.execute(insert(objects).values(id=history_cleanup_activation_id(self.scope.tenant_id),
                tenant=self.scope.tenant_id, owner="__system__", session_id="__history_system",
                kind="history_cleanup_activation", parent_id=None, business_key="v1", version=1,
                payload={"version": 1, "enabled": True, "activated_release": "a"*40},
                created_at=self.now, updated_at=self.now))

    def preview(self, action, sids=None, principal=None):
        body = {"action": action, "sessions": [] if action == "empty_trash" else
            [{"id": sid, "expected_version": self.row(sid)["version"]} for sid in (sids or [self.sid])]}
        return self.service.preview_history(principal or self.principal, body)

    def apply(self, preview, key="history", principal=None):
        return self.service.apply_history(principal or self.principal,
            {"preview_id": preview["preview_id"], "confirmed": True}, key)

    def trash(self, sids=None, key="trash", principal=None):
        return self.apply(self.preview("trash", sids, principal), key, principal)

    def upload(self, key="image", color="navy"):
        source = io.BytesIO()
        Image.new("RGB", (512, 512), color).save(source, "PNG")
        source.seek(0)
        return self.service.upload(self.principal, self.sid, source, "reference.png", key)

    def make_job(self, *, status="queued", prompt="history-private-marker"):
        scope = Scope(self.scope.tenant_id, self.scope.owner_id, self.row()["payload"]["project_id"])
        plan = self.repo.create_plan(scope, {"request": {"prompt": prompt}},
            {"backend": "mock", "pool": "test-pool", "request": {"prompt": prompt},
             "workflow": {"node": {"text": prompt}}, "engine_manifest_digest": "f"*64}, expires_at=self.now+1000)
        return self.repo.create_job(scope, plan["id"], "original-job", initial_status=status)

    def test_tag_boolean_metadata_restore_and_default_off_cleanup(self):
        with self.assertRaises(QuickChatError):
            self.service.create_session(self.principal, {"is_test": "true"}, "bad")
        self.assertTrue(self.session["is_test"])
        self.assertEqual(self.service.list_sessions(self.principal, hide_tests=True)["sessions"], [])
        original = copy.deepcopy(self.row()["payload"])
        moved = self.trash()
        self.assertEqual(moved["state"], "completed")
        self.assertEqual(self.service.list_sessions(self.principal)["sessions"], [])
        self.assertEqual(len(self.service.list_sessions(self.principal, view="trash")["sessions"]), 1)
        blocked = self.preview("purge")
        self.assertEqual(blocked["items"][0]["blocked_reason"], "history_cleanup_not_enabled")
        with self.assertRaises(Conflict):
            self.apply(blocked, "disabled")
        self.assertEqual(self.service.process_history_cleanup(), {"actions_checked": 0, "objects_checked": 0})
        restored = self.apply(self.preview("restore"), "restore")
        self.assertEqual(restored["items"][0]["trash_state"], "active")
        actual = self.row()["payload"]
        for field in original:
            if field not in {"history", "latest_seq"}:
                self.assertEqual(actual[field], original[field])

    def test_expired_consented_trash_never_creates_purge_and_can_restore(self):
        self.activate()
        self.upload()
        self.trash()
        self.assertIsNone(self.row()["payload"]["history"]["purge_at"])
        with self.repo.transaction() as conn:
            row = self.service._get(conn, self.principal, self.sid, kind="session")
            payload = copy.deepcopy(row["payload"])
            payload["history"].update(purge_at=self.now-1, cleanup_consent=True)
            conn.execute(update(objects).where(objects.c.id == self.sid).values(payload=payload))
        original = copy.deepcopy(self.row())
        self.now += 6*86400
        with patch.object(self.service, "preview_history", side_effect=AssertionError("automatic preview")):
            self.assertEqual(self.service.process_history_cleanup(), {"actions_checked": 0, "objects_checked": 0})
        self.assertEqual(self.row(), original)
        visible = self.service.list_sessions(self.principal, view="trash")["sessions"][0]
        self.assertIsNone(visible["purge_at"])
        self.assertFalse(visible["auto_purge_enabled"])
        restored = self.apply(self.preview("restore"), "restore-old-trash")
        self.assertEqual(restored["items"][0]["trash_state"], "active")

    def test_malformed_history_shapes_are_closed_before_database_binding(self):
        for body in (None, [], {"action": []}, {"action": {}, "sessions": []}):
            with self.assertRaises(QuickChatError) as error:
                self.service.preview_history(self.principal, body)
            self.assertEqual(error.exception.status, 422)
        for value in (None, [], {}, 7):
            with self.assertRaises(QuickChatError) as error:
                self.service.apply_history(self.principal, {"preview_id": value, "confirmed": True}, "malformed")
            self.assertEqual(error.exception.status, 422)

    def test_batch_guard_sees_original_jobs_and_pending_import_and_admission(self):
        second = self.new_session("other")
        self.make_job()
        preview = self.preview("trash", [self.sid, second["id"]])
        self.assertEqual(preview["items"][0 if preview["items"][0]["id"] == self.sid else 1]["blocked_reason"],
                         "jobs_active_or_uncertain")
        with self.assertRaises(QuickChatError):
            self.apply(preview)
        self.assertEqual(self.row(second["id"])["payload"].get("history", {}).get("trash_state", "active"), "active")
        for kind, state, expected in (("import", "pending", "authoring_operation_unresolved"),
                                      ("import", "unknown", "authoring_operation_unresolved"),
                                      ("execution", "pending_admission", "admission_unresolved")):
            with self.repo.transaction() as conn:
                self.service._new(conn, self.principal, second["id"], kind, {"status": state, "job_id": None})
            p = self.preview("trash", [second["id"]])
            self.assertEqual(p["items"][0]["blocked_reason"], expected)
            with self.repo.transaction() as conn:
                conn.execute(objects.delete().where(objects.c.session_id == second["id"], objects.c.kind == kind))

    def test_test_acceptance_replay_after_archive_and_review_hold(self):
        body = {"expected_version": self.row()["version"], "test_accepted": True, "review_hold": True}
        marked = self.service.patch_session(self.principal, self.sid, body, "accepted")
        self.assertTrue(marked["session"]["archive_pending"])
        self.service.process_history_cleanup()
        replay = self.service.patch_session(self.principal, self.sid, body, "accepted")
        self.assertEqual(replay["session"]["trash_state"], "trashed")
        self.assertFalse(replay["session"]["archive_pending"])
        self.activate()
        self.now += 6*86400
        self.service.process_history_cleanup()
        self.assertEqual(self.row()["payload"]["history"]["trash_state"], "trashed")
        self.assertTrue(self.preview("purge")["review_hold_release_required"])

    def test_limited_scope_does_not_consent_cleanup_and_empty_trash_is_authorized_set(self):
        second = self.new_session("unauthorized")
        limited = Principal(self.scope.owner_id, "limited", machine=True, all_projects=False,
            project_ids=(self.row()["payload"]["project_id"],), scopes=("projects:read", "projects:write"))
        self.trash(principal=limited)
        self.trash([second["id"]], key="other-trash")
        listed = self.service.list_sessions(limited, view="trash")["sessions"]
        self.assertFalse(listed[0]["auto_purge_enabled"])
        self.activate()
        full = Principal(self.scope.owner_id, "full-scoped", machine=True, all_projects=False,
            project_ids=limited.project_ids, scopes=("projects:read", "projects:write", "assets:read", "assets:write", "jobs:read", "jobs:write"))
        preview = self.preview("empty_trash", principal=full)
        self.assertEqual([item["id"] for item in preview["items"]], [self.sid])
        third = self.new_session("new-other-project")
        self.trash([third["id"]], key="third-trash")
        self.apply(preview, "only-authorized", full)
        self.service.process_history_cleanup()
        self.assertEqual(self.row()["payload"]["history"]["trash_state"], "purged")
        self.assertEqual(self.row(second["id"])["payload"]["history"]["trash_state"], "trashed")

    def test_absolute_media_reference_and_snapshot_attachment_conserve_shared_asset(self):
        self.activate()
        uploaded = self.upload()
        aid = uploaded["asset_id"]
        self.trash()
        preview = self.preview("purge")
        self.assertEqual(preview["removed"]["assets"], 1)
        scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__projects")
        self.repo.put_document(scope, "project", "shared-story", {"title": "Chosen story", "entities": [{
            "data": {"chosen": {"url": "https://studio.example/v1/assets/"+aid+"/content?download=1"}}}]})
        with self.assertRaises(QuickChatError) as changed:
            self.apply(preview)
        self.assertEqual(changed.exception.code, "history_preview_changed")
        updated = self.preview("purge")
        self.assertEqual(updated["kept"]["assets"], 1)
        self.assertEqual(updated["removed"]["objects"], 0)
        self.apply(updated)
        self.service.process_history_cleanup()
        self.assertEqual(self.service.assets.get(self.scope.owner_id, aid)["status"], "ready")
        self.assertEqual(self.repo.get_document(scope, "project", "shared-story")["payload"]["title"], "Chosen story")
        self.assertEqual(reference_ids({"url": "https://studio.example/v1/assets/"+aid+"/content"})[0], {aid})

    def test_lost_delete_ack_confirms_absence_once_and_old_upload_cannot_resurrect(self):
        self.activate()
        uploaded = self.upload()
        aid = uploaded["asset_id"]
        before = self.service.assets.usage(self.scope.owner_id)["accounted_bytes"]
        self.trash()
        preview = self.preview("purge")
        action = self.apply(preview)
        original_delete = self.service.storage.delete
        deleted = []
        def lost_ack(key):
            deleted.append(key)
            original_delete(key)
            raise TimeoutError("lost reply")
        with patch.object(self.service.storage, "delete", side_effect=lost_ack):
            self.service.process_history_cleanup(max_objects=10)
        completed = self.service.get_history_action(self.principal, action["action_id"])
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(len(deleted), len(set(deleted)))
        after = self.service.assets.usage(self.scope.owner_id)["accounted_bytes"]
        self.assertEqual(before-after, preview["removed"]["bytes"])
        self.service.process_history_cleanup()
        self.assertEqual(self.service.assets.usage(self.scope.owner_id)["accounted_bytes"], after)
        with self.repo.engine.connect() as conn:
            self.assertIsNotNone(conn.execute(select(receipts).where(receipts.c.id == aid)).first())
        with self.assertRaises(Exception):
            self.upload()
        with self.assertRaises(Conflict):
            self.repo.put_document(Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"),
                "project", "resurrection", {"asset_id": aid})
        self.assertEqual(self.apply(preview)["state"], "completed")

    def test_wrong_current_bytes_retains_original_intent_and_accounting(self):
        self.activate()
        self.upload()
        self.trash()
        preview = self.preview("purge")
        action = self.apply(preview)
        before = self.service.assets.usage(self.scope.owner_id)["accounted_bytes"]
        with patch.object(self.service.storage, "open", return_value=io.BytesIO(b"different bytes")), \
                patch.object(self.service.storage, "delete") as deleted:
            self.service.process_history_cleanup(max_objects=1)
        deleted.assert_not_called()
        self.assertEqual(self.service.get_history_action(self.principal, action["action_id"])["state"], "pending")
        self.assertEqual(self.service.assets.usage(self.scope.owner_id)["accounted_bytes"], before)
        with self.assertRaises(QuickChatError):
            self.apply(self.preview("restore"), "restore-after-intent")

    def test_terminal_redaction_preserves_ledger_hashes_and_managed_plan_guard(self):
        self.activate()
        job = self.make_job(status="planned")
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="cancelled"))
        with self.repo.engine.connect() as conn:
            original_plan_hash = conn.execute(select(plans.c.plan_hash).where(plans.c.id == job["plan_id"])).scalar_one()
            baseline = {table.name: list(conn.execute(select(table)).mappings()) for table in
                        (budget_accounts, budget_reservations, attempts)}
        # Generic managed-plan creation remains the existing closed boundary.
        with self.assertRaises(Conflict):
            self.app.state.generation_admission.create(self.principal, job["plan_id"], "generic-bypass")
        self.trash()
        self.apply(self.preview("purge"))
        self.service.process_history_cleanup()
        with self.repo.engine.connect() as conn:
            retained = dict(conn.execute(select(jobs).where(jobs.c.id == job["id"])).mappings().one())
            retained_plan = dict(conn.execute(select(plans).where(plans.c.id == job["plan_id"])).mappings().one())
            for table in (budget_accounts, budget_reservations, attempts):
                self.assertEqual(list(conn.execute(select(table)).mappings()), baseline[table.name])
        self.assertEqual(retained["request_hash"], job["request_hash"])
        self.assertEqual(retained_plan["plan_hash"], original_plan_hash)
        self.assertEqual(retained["execution_plan"]["engine_manifest_digest"], "f"*64)
        for value in (retained["request"], retained["execution_plan"], retained_plan["request"], retained_plan["execution_plan"]):
            self.assertNotIn("history-private-marker", json.dumps(value))
        public = self.app.state.public_job(retained)
        self.assertTrue(public["content_deleted"])
        self.assertFalse(public["retry_advice"]["safe_to_retry"])

    def test_reference_gate_serializes_attach_after_irreversible_intent(self):
        aid = "1"*32
        started = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.repo.transaction() as conn:
                owner_reference_gate(conn, self.scope.tenant_id, self.scope.owner_id)
                def attach():
                    started.set()
                    try:
                        self.repo.put_document(Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"),
                            "project", "racing-story", {"asset_id": aid})
                    except Conflict:
                        return "blocked"
                    return "attached"
                future = executor.submit(attach)
                self.assertTrue(started.wait(2))
                put_tombstone(conn, self.scope.tenant_id, self.scope.owner_id, kind="asset", value=aid,
                    action_id="original-intent", now=self.now)
            self.assertEqual(future.result(timeout=10), "blocked")

    def test_original_plan_admission_is_fenced_after_trash_without_auto_expiry(self):
        project = self.row()["payload"]["project_id"]
        scope = Scope(self.scope.tenant_id, self.scope.owner_id, project)
        plan = self.repo.create_plan(scope, {"prompt": "original accepted inputs"},
            {"pool": "test-pool", "backend": "mock"}, expires_at=self.now+1000)
        self.trash()
        with self.assertRaises(Conflict):
            self.repo.create_job(scope, plan["id"], "late-internal-create")
        self.activate()
        self.now += 5*86400+1
        self.service.process_history_cleanup()
        self.assertEqual(self.row()["payload"]["history"]["trash_state"], "trashed")
        with self.repo.engine.connect() as conn:
            self.assertIsNone(conn.execute(select(jobs.c.id).where(jobs.c.plan_id == plan["id"])).first())

    def test_whole_asset_alias_and_retained_derivative_source_close_before_delete(self):
        self.activate()
        source = self.upload("source", "navy")["asset_id"]
        derivative = self.upload("derived", "red")["asset_id"]
        with self.repo.transaction() as conn:
            row = conn.execute(select(asset_table).where(asset_table.c.id == derivative)).mappings().one()
            record = json.loads(row["record"])
            record.update(parent_id=source, selection={"asset_id": source})
            conn.execute(update(asset_table).where(asset_table.c.id == derivative).values(record=json.dumps(record)))
            receipt = conn.execute(select(receipts).where(receipts.c.id == derivative)).mappings().one()
            data = json.loads(receipt["record"])
            data["asset"] = record
            conn.execute(update(receipts).where(receipts.c.id == derivative).values(record=json.dumps(data)))
        self.repo.put_document(Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"), "project",
            "retained-derivative", {"entities": [{"data": {"selectedAssetId": derivative}}]})
        self.trash()
        preview = self.preview("purge")
        self.assertEqual(preview["kept"]["assets"], 2)
        self.assertEqual(preview["removed"]["objects"], 0)
        self.apply(preview)
        self.service.process_history_cleanup()
        self.assertEqual(self.service.assets.get(self.scope.owner_id, source)["status"], "ready")
        self.assertEqual(self.service.assets.get(self.scope.owner_id, derivative)["parent_id"], source)

    def test_chosen_output_retains_original_audio_pair_and_foreign_warning_has_no_label(self):
        self.activate()
        uploaded = self.upload()["asset_id"]
        job = self.make_job(status="planned")
        video, audio, attempt = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="succeeded"))
            conn.execute(insert(attempts).values(id=attempt, job_id=job["id"], number=1, status="succeeded",
                fence=1, worker_id="fixture-worker", upstream_stopped=1, collection_failures=0,
                created_at=self.now, updated_at=self.now))
            for ident, kind in ((video, "video"), (audio, "audio")):
                conn.execute(insert(artifacts).values(id=ident, job_id=job["id"], attempt_id=attempt,
                    metadata={"kind": kind, "size_bytes": 3, "sha256": "a"*64,
                        "object_key": "not-current-store-"+ident}, created_at=self.now))
            conn.execute(insert(documents).values(tenant_id="other-tenant", owner_id=self.scope.owner_id,
                project_id="__projects", kind="project", document_id="private-foreign-title-id", version=1,
                payload={"title": "DO-NOT-LEAK", "cloudAssetId": uploaded}, updated_at=self.now))
        self.repo.put_document(Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"), "project",
            "chosen", {"entities": [{"data": {"cloudArtifactId": video, "chosen": True}}]})
        self.trash()
        preview = self.preview("purge")
        self.assertEqual(preview["kept"]["artifacts"], 2)
        self.assertEqual(preview["kept"]["assets"], 1)
        self.assertTrue(any(w["id"] == audio and w["reason"] == "same_generated_version" for w in preview["warnings"]))
        self.assertNotIn("DO-NOT-LEAK", json.dumps(preview))
        self.assertNotIn("private-foreign-title-id", json.dumps(preview))

