"""HTTP-free application-service compatibility on isolated SQLite/PostgreSQL."""
import copy
from pathlib import Path
import uuid

from sqlalchemy import func, insert, select, update

import test_platform_repository as ledger
from studio_platform.assets import AssetService
from studio_platform.auth import Principal
from studio_platform.capabilities import capabilities
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.generation_admission import GenerationAdmission
from studio_platform.generation_draft import plan_body
from studio_platform.generation_services import GenerationAccess, GenerationPlanning, GenerationRead
from studio_platform.repository import Conflict, NotFound, artifacts, attempts, jobs, plans, request_hash
from studio_platform.settings import Settings
from studio_platform.storage import LocalObjectStore


class GenerationServicesTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        self.principal = Principal("superdan", "browser")
        self.settings = Settings(Path(self.temp.name), tenant_id=self.scope.tenant_id,
            generation_enabled=True, execution_backend="mock", database_url=self.url)
        self.assets = AssetService(self.repo.engine, LocalObjectStore(Path(self.temp.name)/"objects"),
            Path(self.temp.name), tenant=self.settings.tenant_id)
        self.access = GenerationAccess(self.repo, self.settings.tenant_id)
        self.read = GenerationRead(self.repo, self.access)
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.planning = GenerationPlanning(repo=self.repo, assets=self.assets, settings=self.settings,
            policies=self.policies, access=self.access, read=self.read)
        self.admission = GenerationAdmission(repo=self.repo, assets=self.assets, settings=self.settings,
            policies=self.policies, access=self.access, planning=self.planning)
        self.project = {"id": "story-one", "schemaVersion": 4, "links": [], "entities": [
            {"id": "shot-one", "type": "shot", "parentId": "scene-one", "version": 1,
             "title": "Shot", "description": "Synthetic application-service fixture", "data": {
                 "seconds": 5, "h3": {"recipeId": "h3-base-fl2va-v1", "controls": {
                     "duration": 5, "resolution": "480P", "seed": "18446744073709551615"}}}},
            {"id": "scene-one", "type": "scene", "parentId": "chapter-one", "version": 1, "data": {}},
            {"id": "chapter-one", "type": "chapter", "parentId": None, "version": 1, "data": {}}]}
        self.save_project(self.project)

    def save_project(self, project, *, principal=None, version=None):
        return self.repo.put_document(self.access.project_scope(principal or self.principal),
            "project", project["id"], project, expected_version=version)

    def direct_body(self):
        return plan_body(self.project, "shot-one", capabilities(self.settings),
            lambda *args: self.fail("Text-only preflight must not derive media"))

    def count(self, table):
        with self.repo.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(table))

    def make_job(self, key="one"):
        plan = self.admission.plan(self.principal, self.direct_body())
        return self.admission.create(self.principal, plan["plan_id"], key)

    def test_direct_and_story_preflight_share_snapshot_without_http_or_submission(self):
        direct = self.admission.plan(self.principal, self.direct_body())
        story = self.admission.preflight(self.principal, self.project, "shot-one", expected_version=1)
        self.assertNotEqual(direct["plan_id"], story["plan_id"])
        for field in ("effective_request", "output_spec", "execution", "client_ref"):
            self.assertEqual(direct[field], story[field], field)
        # Story preflight adds immutable authoring provenance. Its complete
        # receipt hash differs while the original execution request stays exact.
        direct_saved = self.access.plan(self.principal, direct["plan_id"])
        story_saved = self.access.plan(self.principal, story["plan_id"])
        authoring = {"authoring_recipe", "authoring_context", "authoring_defaults_version"}
        self.assertTrue(authoring <= story_saved["request"].keys())
        self.assertFalse(authoring & direct_saved["request"].keys())
        self.assertEqual({k: v for k, v in story_saved["request"].items() if k not in authoring},
                         direct_saved["request"])
        self.assertEqual(story_saved["request"]["authoring_context"]["shot"], self.project["entities"][0])
        self.assertEqual(story_saved["request"]["authoring_defaults_version"], 0)
        for value, saved in ((direct, direct_saved), (story, story_saved)):
            self.assertEqual(value["request_hash"], request_hash(saved["request"]))
        self.assertNotEqual(direct["request_hash"], story["request_hash"])
        self.assertEqual(direct["effective_request"]["seed"], "18446744073709551615")
        self.assertTrue(direct["simulation"])
        self.assertEqual(self.count(plans), 2)
        self.assertEqual(self.count(jobs), 0)
        self.assertEqual(self.count(attempts), 0)

    def test_owner_and_pat_scope_refusal_precedes_any_plan_write(self):
        denied = (Principal("supervan", "other"), Principal("superdan", "read", True,
            ("story-one",), ("projects:read", "jobs:read")), Principal("superdan", "other-project", True,
            ("unrelated",), ("jobs:write",)))
        for actor in denied:
            with self.subTest(actor=actor.actor_id), self.assertRaises(NotFound):
                self.admission.plan(actor, self.direct_body())
        self.assertEqual(self.count(plans), 0)
        pat = Principal("superdan", "agent", True, ("story-one",), ("jobs:write", "jobs:read"))
        plan = self.admission.plan(pat, self.direct_body())
        job = self.admission.create(pat, plan["plan_id"], "agent-confirm")
        self.assertEqual(job["actor_id"], "agent")
        self.assertEqual(self.read.job(pat, job["id"])["id"], job["id"])
        for actor in denied:
            with self.subTest(actor=actor.actor_id), self.assertRaises(NotFound):
                self.access.plan(actor, plan["plan_id"])
        foreign_tenant = GenerationAccess(self.repo, "another-tenant")
        with self.assertRaises(NotFound):
            foreign_tenant.job(pat, job["id"])

    def test_confirmation_replay_preserves_original_after_edit_new_key_rejects(self):
        plan = self.admission.plan(self.principal, self.direct_body())
        first = self.admission.create(self.principal, plan["plan_id"], "confirmed-once")
        edited = copy.deepcopy(self.project)
        edited["entities"][0]["description"] = "Different source, same manually reported shot version"
        self.save_project(edited, version=1)
        replay = self.admission.create(self.principal, plan["plan_id"], "confirmed-once")
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["request"], replay["request"])
        with self.assertRaisesRegex(Conflict, "shot_version_conflict"):
            self.admission.create(self.principal, plan["plan_id"], "new-confirmation")
        with self.assertRaisesRegex(Conflict, "shot_version_conflict"):
            self.admission.preflight(self.principal, self.project, "shot-one", expected_version=2)
        self.assertEqual(self.count(jobs), 1)
        self.assertEqual(self.count(attempts), 0)

    def test_managed_projection_only_uses_explicit_business_entry(self):
        managed = copy.deepcopy(self.project)
        managed["integration_kind"] = "quick_chat"
        self.save_project(managed, version=1)
        with self.assertRaisesRegex(Conflict, "quick_chat_managed_resource"):
            self.admission.plan(self.principal, self.direct_body())
        plan = self.admission.preflight(self.principal, managed, "shot-one")
        pat = Principal("superdan", "agent", True, ("story-one",), ("jobs:read", "jobs:write"))
        first = self.admission.create_planned(self.principal, plan["plan_id"], "execution-one")
        replay = self.admission.create_planned(pat, plan["plan_id"], "execution-one")
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["status"], "planned")
        self.assertEqual(first["actor_id"], "quick-chat-execution")
        with self.assertRaisesRegex(Conflict, "quick_chat_managed_resource"):
            self.admission.enqueue(self.principal, first)
        queued = self.admission.enqueue(pat, first, business=True)
        self.assertEqual(queued["id"], first["id"])
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(self.count(jobs), 1)
        self.assertEqual(self.count(attempts), 0)

    def completed_fixture(self):
        """Synthetic ledger output for read projection, not execution evidence."""
        job = self.make_job()
        attempt_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4())
        delivery = {"mode": "native-frames-v1", "frame_count": 124, "duration_s": 124/24}
        media = {"kind": "video", "content_type": "video/mp4", "size_bytes": 123,
            "sha256": "a"*64, "duration_s": 124/24, "object_key": "private/object",
            "provider": "private-provider", "storage_profile": "private-profile"}
        with self.repo.transaction() as conn:
            conn.execute(insert(attempts).values(id=attempt_id, job_id=job["id"], number=1,
                status="succeeded", fence=1, worker_id="fixture", created_at=self.now,
                updated_at=self.now, upstream_stopped=1))
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="succeeded",
                execution_plan={**job["execution_plan"], "delivery_spec": delivery}))
            conn.execute(insert(artifacts).values(id=artifact_id, job_id=job["id"],
                attempt_id=attempt_id, metadata=media, created_at=self.now))
        return job["id"], artifact_id, delivery

    def test_single_and_bulk_reads_preserve_native_delivery_and_safe_artifact_dto(self):
        ident, artifact_id, delivery = self.completed_fixture()
        one = self.read.job(self.principal, ident)
        with self.subTest(path="bulk"):
            self.assertEqual(self.read.list_jobs(self.principal), {"jobs": [one]})
        self.assertEqual(one["delivery_spec"], delivery)
        self.assertEqual(one["artifacts"], [{"id": artifact_id, "job_id": ident, "kind": "video",
            "mime": "video/mp4", "size_bytes": 123, "sha256": "a"*64,
            "metadata": {"kind": "video", "content_type": "video/mp4", "size_bytes": 123,
                "sha256": "a"*64, "duration_s": 124/24},
            "content_url": f"/v1/artifacts/{artifact_id}/content",
            "download_url": f"/v1/artifacts/{artifact_id}/content?download=1",
            "renditions": {}, "rendition_ready": False, "rendition_status": "missing"}])
        self.assertEqual(self.access.artifact(self.principal, artifact_id)["job_id"], ident)
        self.assertEqual(self.read.list_jobs(self.principal, offset=1), {"jobs": []})

    def test_job_list_single_and_download_authorization_are_consistent(self):
        ident, artifact_id, _ = self.completed_fixture()
        for actor in (Principal("supervan", "other"), Principal("superdan", "missing-read", True,
                ("story-one",), ("jobs:write",)), Principal("superdan", "other-project", True,
                ("elsewhere",), ("jobs:read",))):
            with self.subTest(actor=actor.actor_id):
                self.assertEqual(self.read.list_jobs(actor), {"jobs": []})
                with self.assertRaises(NotFound):
                    self.read.job(actor, ident)
                with self.assertRaises(NotFound):
                    self.access.artifact(actor, artifact_id)
                with self.assertRaises(NotFound):
                    self.read.list_jobs(actor, project_id="story-one")
        reader = Principal("superdan", "reader", True, ("story-one",), ("jobs:read",))
        self.assertEqual(self.read.list_jobs(reader)["jobs"][0]["id"], ident)
        self.assertEqual(self.access.artifact(reader, artifact_id)["job_id"], ident)

    def test_execution_projection_does_not_disclose_operator_authority(self):
        delivery = {"mode": "native-frames-v1", "frame_count": 124}
        public = self.read.execution({"admission_state": "waiting_capacity", "enabled": True,
            "quote_known": True, "backend": "wangp-worker", "delivery_spec": delivery,
            "capacity_approval_id": "private-approval", "budget_account_ids": ["private-budget"],
            "engine_manifest_digest": "private-engine"})
        self.assertEqual(public, {"admission_state": "waiting_capacity", "enabled": True,
            "quote_known": True, "backend": "wangp-worker", "delivery_spec": delivery})

    def test_pending_and_completed_cleanup_preserve_job_but_refuse_artifact_reads(self):
        ident, artifact_id, _ = self.completed_fixture()
        from studio_platform.quick_chat import metadata
        from studio_platform.content_lifecycle import owner_reference_gate, put_tombstone
        metadata.create_all(self.repo.engine)
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.settings.tenant_id, self.principal.owner)
            put_tombstone(conn, self.settings.tenant_id, self.principal.owner,
                kind="artifact", value=artifact_id, action_id="original-purge", now=self.now)
        with self.assertRaisesRegex(Conflict, "content_purging_or_purged"):
            self.access.artifact(self.principal, artifact_id)
        restricted = Principal("superdan", "unrelated", True, ("elsewhere",), ("jobs:read",))
        with self.assertRaises(NotFound):
            self.access.artifact(restricted, artifact_id)
        original = self.read.job(self.principal, ident)
        with self.repo.transaction() as conn:
            row = conn.execute(select(artifacts).where(artifacts.c.id == artifact_id)).mappings().one()
            conn.execute(update(artifacts).where(artifacts.c.id == artifact_id).values(
                metadata={**row["metadata"], "content_deleted": True}))
            conn.execute(update(jobs).where(jobs.c.id == ident).values(request={"content_deleted": True}))
        with self.assertRaises(NotFound):
            self.access.artifact(self.principal, artifact_id)
        retained = self.read.job(self.principal, ident)
        self.assertEqual((retained["id"], retained["status"], retained["request_hash"]),
                         (original["id"], original["status"], original["request_hash"]))
        self.assertEqual(retained["artifacts"], [])
        self.assertFalse(retained["retry_advice"]["safe_to_retry"])
