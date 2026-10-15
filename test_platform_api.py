"""HTTP contract/isolation tests. Temporary DB and CPU media only; no provider calls."""
import copy
from dataclasses import replace
import io
import os
from pathlib import Path
import secrets
import tempfile
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from studio_platform.api import create_app
from studio_platform.settings import Settings


def project(project_id="story-one"):
    def entity(i, kind, parent=None):
        return dict(id=i, type=kind, parentId=parent, title=i, description="A traveller in a rainy street",
                    version=1, order=0, status="draft", data={"seconds": 5} if kind == "shot" else {})
    return dict(schemaVersion=4, id=project_id, title="测试短剧", logline="A short story",
        entities=[entity("chapter-one", "chapter"), entity("scene-one", "scene", "chapter-one"),
                  entity("shot-one", "shot", "scene-one")], links=[], jobs=[],
        layout={"positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}})


def generation_request(project_id="story-one", **overrides):
    return {"client_ref": {"project_id": project_id, "shot_id": "shot-one", "shot_version": 1},
        "recipe_id": "h3-base-fl2va-v1", "prompt": "A traveller walks down a rainy street.",
        "controls": {"duration": 5, "resolution": "480P", "seed": "18446744073709551615"}, **overrides}


def png():
    out = io.BytesIO()
    Image.new("RGB", (512, 512), "navy").save(out, format="PNG")
    return out.getvalue()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        database_url = ""
        postgres = os.environ.get("PLATFORM_TEST_DATABASE_URL")
        if postgres:
            parsed = make_url(postgres)
            if parsed.host not in {"127.0.0.1", "localhost"} or parsed.database != "sixnine_test":
                raise RuntimeError("API tests only allow the explicit local sixnine_test database")
            schema = "api_test_" + uuid.uuid4().hex
            bootstrap = create_engine(parsed, echo=False, hide_parameters=True)
            with bootstrap.begin() as conn:
                conn.execute(text('CREATE SCHEMA "' + schema + '"'))
            def drop_test_schema():
                try:
                    with bootstrap.begin() as conn:
                        conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                finally:
                    bootstrap.dispose()
            self.addCleanup(drop_test_schema)
            database_url = parsed.update_query_dict({"options": "-csearch_path="+schema}).render_as_string(hide_password=False)
        self.settings = Settings(Path(self.tmp.name), auth_mode="local-test", database_url=database_url)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self, who="superdan"):
        response = self.client.post("/api/auth/login", json={"username": who})
        self.assertEqual(response.status_code, 200, response.text)

    def setup_project(self, ident="story-one"):
        self.login()
        r = self.client.post("/v1/projects", json={"project": project(ident)})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def make_plan(self, **changes):
        r = self.client.post("/v1/generation-plans", json=generation_request(**changes))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_storyboard_owner_flag_default_off_and_never_grants_operator_permissions(self):
        self.login()
        before = self.client.get("/api/auth/me").json()
        self.assertEqual(before["features"], {"storyboard_workspace": False})
        app = create_app(replace(self.settings, storyboard_owners=("superdan",)),
            repository=self.app.state.repository, storage=self.app.state.storage)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
            enabled = client.get("/api/auth/me").json()
            self.assertEqual(enabled["features"], {"storyboard_workspace": True})
            self.assertEqual(enabled["operator_capacity"], before["operator_capacity"])
            client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
            self.assertEqual(client.get("/api/auth/me").json()["features"], {"storyboard_workspace": False})
            token = secrets.token_urlsafe(32)
            app.state.auth.register_client("flag-test", token, "superdan", ["story-one"], ["projects:read"])
            client.cookies.clear()
            machine = client.get("/api/auth/me", headers={"Authorization": "Bearer "+token}).json()
            self.assertTrue(machine["machine"])
            self.assertEqual(machine["features"], {"storyboard_workspace": False})

    def test_activity_summary_counts_only_authorized_owner_project(self):
        self.setup_project()
        plan = self.make_plan()
        response = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
            headers={"Idempotency-Key": "activity-summary-one"})
        self.assertEqual(response.status_code, 202)
        summary = self.client.get("/v1/activity-summary", params={"client_project_id": "story-one"})
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["total"], 1)
        self.assertEqual(summary.json()["counts"]["blocked"], 1)
        self.assertNotIn("prompt", summary.text)
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/activity-summary", params={"client_project_id": "story-one"}).status_code, 404)
        self.client.post("/v1/projects", json={"project": project()}).raise_for_status()
        self.assertEqual(self.client.get("/v1/activity-summary", params={"client_project_id": "story-one"}).json()["total"], 0)
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client("activity-scope-test", token, "superdan", ["story-one"], ["projects:read"])
        headers = {"Authorization": "Bearer "+token}
        self.assertEqual(self.client.get("/v1/activity-summary", params={"client_project_id": "story-one"}, headers=headers).status_code, 404)

    def test_cpu_media_busy_returns_retry_and_same_asset_can_resume(self):
        from studio_platform import media
        self.setup_project()
        gate = media.ProcessingAdmission(wait_seconds=0)
        with mock.patch.object(media, "PROCESSING_ADMISSION", gate), gate.acquire("video"):
            response = self.client.post("/v1/assets", data={"client_project_id": "story-one", "client_asset_id": "busy-original"},
                files={"file": ("reference.png", png(), "image/png")})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers.get("Retry-After"), "5")
        self.assertIn("恢复同一素材", response.json()["detail"])
        items = self.app.state.assets.list("superdan", "story-one")
        self.assertEqual(len(items), 1)
        asset_id = items[0]["id"]
        self.assertEqual(items[0]["status"], "failed")
        self.login("supervan")
        self.assertEqual(self.client.post(f"/v1/assets/{asset_id}/resume").status_code, 404)
        self.login("superdan")
        resumed = self.client.post(f"/v1/assets/{asset_id}/resume")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["id"], asset_id)
        self.assertEqual(resumed.json()["status"], "ready")
        self.assertEqual(len(self.app.state.assets.list("superdan", "story-one")), 1)

    def test_auth_required_and_bad_host_origin(self):
        self.assertEqual(self.client.get("/v1/projects").status_code, 401)
        self.assertEqual(self.client.get("/healthz", headers={"host": "attacker.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "superdan"},
            headers={"origin": "https://attacker.example"}).status_code, 403)
        self.login()
        self.assertEqual(self.client.get("/api/auth/me").json()["username"], "superdan")
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_character_studio_look_bindings_save_and_invalidate_source(self):
        self.login()
        value = project()
        actor = dict(id="actor-one", type="character", parentId=None, title="Traveller", description="A traveller",
            version=1, order=0, status="draft", data={"looks": [{"id": "look-rain", "name": "Raincoat", "description": "Blue coat",
                "version": 1, "gallery": {"front": "portrait-one"}}]})
        picture = dict(id="portrait-one", type="image", parentId=None, title="Portrait", description="Reference",
            version=1, order=0, status="draft", data={"fileId": "local-file-one"})
        value["entities"].extend((actor, picture))
        value["entities"][1]["data"]["cast"] = [{"characterId": "actor-one", "lookId": "look-rain"}]
        r = self.client.post("/v1/projects", json={"project": value})
        self.assertEqual(r.status_code, 201, r.text)
        plan = self.make_plan()
        # A changed image in the character look is relevant even without a
        # visible reference edge or a manually incremented scene/shot version.
        value["entities"][-1]["data"]["fileId"] = "local-file-two"
        self.assertEqual(self.client.put("/v1/projects/story-one", json={"project": value, "expected_version": 1}).status_code, 200)
        self.assertEqual(self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
            headers={"Idempotency-Key": "old-character-reference"}).status_code, 409)
        value["entities"][1]["data"]["cast"][0]["lookId"] = "missing-look"
        self.assertEqual(self.client.put("/v1/projects/story-one", json={"project": value, "expected_version": 2}).status_code, 422)

    def test_projects_version_conflict_and_two_user_isolation(self):
        created = self.setup_project()
        self.assertEqual(created["version"], 1)
        modified = project()
        modified["title"] = "Updated"
        r = self.client.put("/v1/projects/story-one", json={"project": modified, "expected_version": 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 2)
        self.assertEqual(self.client.put("/v1/projects/story-one", json={"project": project(),
            "expected_version": 1}).status_code, 409)
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/projects").json(), {"projects": []})
        self.assertEqual(self.client.get("/v1/projects/story-one").status_code, 404)
        # Names and local IDs can coincide; the owner remains part of the namespace.
        self.assertEqual(self.client.post("/v1/projects", json={"project": project()}).status_code, 201)
        self.assertEqual(self.client.get("/v1/projects/story-one").json()["version"], 1)

    def test_blocked_plan_job_is_idempotent_and_private(self):
        self.setup_project()
        plan = self.make_plan()
        self.assertEqual(plan["status"], "blocked")
        self.assertIsNone(plan["estimate"]["cost_microusd"])
        self.assertEqual(plan["effective_request"]["seed"], "18446744073709551615")
        headers = {"Idempotency-Key": "one-click"}
        a = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers=headers)
        self.assertEqual(a.status_code, 202, a.text)
        b = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers=headers)
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertFalse(b.json()["created"])
        self.assertEqual(a.json()["status"], "blocked")
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/jobs/"+a.json()["id"]).status_code, 404)
        self.assertEqual(self.client.post("/v1/jobs/"+a.json()["id"]+"/cancel").status_code, 404)
        self.assertEqual(self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers=headers).status_code, 404)

    def test_plan_rejects_edited_shot_and_hidden_controls(self):
        self.setup_project()
        plan = self.make_plan()
        changed = project()
        changed["entities"][2]["version"] = 2
        self.client.put("/v1/projects/story-one", json={"project": changed, "expected_version": 1})
        self.assertEqual(self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
            headers={"Idempotency-Key": "late-click"}).status_code, 409)
        self.assertEqual(self.client.post("/v1/generation-plans", json=generation_request(
            controls={"audio_decode": "tiled"})).status_code, 422)
        self.assertEqual(self.client.post("/v1/generation-plans", json=generation_request(
            controls={"mystery": "silently-ignore"})).status_code, 422)

    def test_private_upload_real_normalization_ranges_and_foreign_reference(self):
        self.setup_project()
        raw = png()
        r = self.client.post("/v1/assets", data={"client_project_id": "story-one", "client_asset_id": "local-one"},
                             files={"file": ("人像.png", raw, "image/png")})
        self.assertEqual(r.status_code, 201, r.text)
        asset = r.json()
        self.assertTrue(asset["metadata"]["model_ready"])
        self.assertNotIn("original", asset)
        download = self.client.get(asset["content_url"])
        self.assertEqual(download.content, raw)
        attachment = self.client.get(asset["content_url"]+"?download=1")
        self.assertEqual(attachment.content, raw)
        self.assertTrue(attachment.headers["content-disposition"].startswith("attachment;"))
        self.assertTrue(download.headers["content-disposition"].startswith("inline;"))
        partial = self.client.get(asset["content_url"], headers={"Range": "bytes=2-6"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, raw[2:7])
        self.assertEqual(self.client.head(asset["content_url"]).headers["content-length"], str(len(raw)))
        self.assertEqual(self.client.get(asset["content_url"], headers={"Range": "bytes=100000-"}).status_code, 416)
        self.make_plan(inputs={"first_frame": asset["id"]})
        self.login("supervan")
        self.client.post("/v1/projects", json={"project": project()})
        self.assertEqual(self.client.get(asset["content_url"]).status_code, 404)
        self.assertEqual(self.client.get(asset["content_url"]+"?download=1").status_code, 404)
        self.assertEqual(self.client.post("/v1/generation-plans", json=generation_request(
            inputs={"first_frame": asset["id"]})).status_code, 404)

    def test_rejected_upload_never_becomes_ready(self):
        self.setup_project()
        r = self.client.post("/v1/assets", data={"client_project_id": "story-one"},
                             files={"file": ("fake.png", b"not an image", "image/png")})
        self.assertEqual(r.status_code, 422, r.text)
        entries = self.client.get("/v1/assets", params={"client_project_id": "story-one"}).json()["assets"]
        self.assertEqual(entries[0]["status"], "failed")
        self.assertEqual(self.client.get(entries[0]["content_url"]).status_code, 409)

    def test_service_client_scopes_and_rotation(self):
        self.setup_project()
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client("yingxu-test", token, "superdan", ["story-one"], ["projects:read"])
        auth = {"Authorization": "Bearer "+token}
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/v1/projects/story-one", headers=auth).status_code, 200)
        self.assertEqual(self.client.post("/v1/generation-plans", json=generation_request(), headers=auth).status_code, 404)
        self.assertEqual(self.client.post("/v1/projects", json={"project": project("new")}, headers=auth).status_code, 403)
        self.app.state.auth.register_client("yingxu-test", secrets.token_urlsafe(32), "superdan", ["story-one"], ["projects:read"])
        self.assertEqual(self.client.get("/v1/projects/story-one", headers=auth).status_code, 401)

    def test_login_attempt_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/api/auth/login", json={"username": "invalid"}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "superdan"}).status_code, 429)

    def test_machine_list_pagination_filters_before_sql_limit(self):
        self.login()
        for ident in ("allowed-one", "allowed-two", "private-one", "private-two"):
            self.client.post("/v1/projects", json={"project": project(ident)}).raise_for_status()
            plan = self.make_plan(project_id=ident)
            self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "page-"+ident}).raise_for_status()
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client("paged-client", token, "superdan", ["allowed-one", "allowed-two"],
            ["projects:read", "jobs:read"])
        headers = {"Authorization": "Bearer "+token}
        for path, key, field in (("/v1/projects", "projects", "id"), ("/v1/jobs", "jobs", "project_id")):
            pages = [self.client.get(path, params={"limit": 1, "offset": n}, headers=headers).json()[key] for n in range(3)]
            self.assertEqual([len(page) for page in pages], [1, 1, 0])
            self.assertEqual({page[0][field] for page in pages[:2]}, {"allowed-one", "allowed-two"})
            for invalid in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
                self.assertEqual(self.client.get(path, params=invalid, headers=headers).status_code, 422)
        self.app.state.auth.register_client("paged-client", token, "superdan", ["allowed-one", "allowed-two"], ["projects:read"])
        self.assertEqual(self.client.get("/v1/jobs", headers=headers).json(), {"jobs": []})

    def test_stale_browser_account_cannot_overwrite_new_owners_same_project_id(self):
        self.setup_project()
        stale_draft = project()
        stale_draft["title"] = "superdan unsaved work"
        self.login("supervan")
        self.client.post("/v1/projects", json={"project": project()}).raise_for_status()
        response = self.client.put("/v1/projects/story-one", json={"project": stale_draft, "expected_version": 1},
            headers={"X-Expected-Account": "superdan"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "account_context_changed")
        self.assertEqual(response.headers["x-authenticated-account"], "supervan")
        response = self.client.get("/v1/projects/story-one", headers={"X-Expected-Account": "supervan"})
        self.assertEqual(response.headers["x-authenticated-account"], "supervan")
        self.assertEqual(response.json()["version"], 1)
        self.assertEqual(response.json()["project"]["title"], "测试短剧")
        uploaded = self.client.post("/v1/assets", headers={"X-Expected-Account": "superdan"},
            data={"client_project_id": "story-one"}, files={"file": ("old-account.png", png(), "image/png")})
        self.assertEqual(uploaded.status_code, 409)
        self.assertEqual(self.client.get("/v1/assets?client_project_id=story-one").json()["assets"], [])

    def test_machine_identity_is_not_reinterpreted_by_browser_expected_account_header(self):
        self.setup_project()
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client("expected-header", token, "superdan", ["story-one"], ["projects:read"])
        self.login("supervan")
        response = self.client.get("/v1/projects/story-one", headers={"Authorization": "Bearer "+token, "X-Expected-Account": "supervan"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-authenticated-account"], "superdan")

    def test_unchanged_manual_version_still_invalidates_changed_content(self):
        self.setup_project()
        old = self.make_plan()
        accepted = self.client.post("/v1/jobs", json={"plan_id": old["plan_id"]}, headers={"Idempotency-Key": "accepted"}).json()
        changed = project()
        changed["entities"][1]["description"] = "The scene and costume are different now"
        self.assertEqual(self.client.put("/v1/projects/story-one", json={"project": changed, "expected_version": 1}).status_code, 200)
        self.assertEqual(self.client.post("/v1/jobs", json={"plan_id": old["plan_id"]}, headers={"Idempotency-Key": "new-click"}).status_code, 409)
        retry = self.client.post("/v1/jobs", json={"plan_id": old["plan_id"]}, headers={"Idempotency-Key": "accepted"})
        self.assertEqual(retry.status_code, 202)
        self.assertEqual(retry.json()["id"], accepted["id"])

    def test_forged_chapter_and_scene_sources_rejected(self):
        self.setup_project()
        body = generation_request()
        body["client_ref"]["scene_id"] = "some-other-scene"
        self.assertEqual(self.client.post("/v1/generation-plans", json=body).status_code, 409)

    def test_batch_resume_cancel_and_owner_isolation(self):
        self.setup_project()
        plan = self.make_plan()
        body = {"client_project_id": "story-one", "plan_ids": [plan["plan_id"], "no-such-plan"]}
        head = {"Idempotency-Key": "batch-one"}
        response = self.client.post("/v1/batches", json=body, headers=head)
        self.assertEqual(response.status_code, 202, response.text)
        batch = response.json()
        self.assertEqual(batch["items"][0]["status"], "blocked")
        self.assertEqual(batch["items"][1]["status"], "rejected")
        again = self.client.post("/v1/batches", json=body, headers=head).json()
        self.assertEqual(batch["id"], again["id"])
        self.assertEqual(batch["items"][0]["job_id"], again["items"][0]["job_id"])
        self.assertEqual(len(self.client.get("/v1/batches", params={"client_project_id": "story-one"}).json()["batches"]), 1)
        bad = {**body, "plan_ids": [plan["plan_id"]]}
        self.assertEqual(self.client.post("/v1/batches", json=bad, headers=head).status_code, 409)
        cancelled = self.client.post("/v1/batches/"+batch["id"]+"/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["items"][0]["status"], "cancelled")
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/batches/"+batch["id"]).status_code, 404)
        self.assertEqual(self.client.post("/v1/batches/"+batch["id"]+"/cancel").status_code, 404)

    def test_streaming_limit_without_content_length(self):
        self.login()
        body = (b' ' * 9000 for _ in range(2))
        r = self.client.post("/api/auth/login", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413, r.text)

    def test_different_tenant_rejects_same_session_and_machine_token(self):
        self.setup_project()
        from studio_platform.auth import Auth
        other = Auth(self.app.state.repository.engine, tenant="different", mode="local-test")
        browser = other.login("superdan", "")
        self.assertIsNone(self.app.state.auth.session(browser))
        token = secrets.token_urlsafe(32)
        other.register_client("other-client", token, "superdan", ["story-one"], ["projects:read"])
        self.assertIsNone(self.app.state.auth.bearer(token))

    def test_public_settings_refuse_insecure_auth_and_mock(self):
        with self.assertRaises(ValueError):
            Settings(Path(self.tmp.name), public_origin="https://www.sixnine.art", auth_mode="local-test")
        with self.assertRaises(ValueError):
            Settings(Path(self.tmp.name), public_origin="https://www.sixnine.art", execution_backend="mock")


class NativeMediaAccountTests(unittest.TestCase):
    """Native browser content keeps the same cookie/owner boundary as fetch."""
    setUp = ApiTests.setUp
    login = ApiTests.login
    setup_project = ApiTests.setup_project
    make_plan = ApiTests.make_plan

    def content_fixture(self):
        from sqlalchemy import insert, update
        from studio_platform.repository import artifacts, attempts, jobs

        self.setup_project()
        raw = png()
        uploaded = self.client.post("/v1/assets", data={"client_project_id": "story-one"},
            files={"file": ("native.png", raw, "image/png")})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        asset = uploaded.json()
        private = self.app.state.assets.get("superdan", asset["id"])
        plan = self.make_plan()
        job = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
            headers={"Idempotency-Key": "native-media-fixture"}).json()
        with self.app.state.repository.engine.begin() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="succeeded"))
            connection.execute(insert(attempts).values(id="native-attempt", job_id=job["id"], number=1,
                status="succeeded", fence=1, worker_id="cpu-fixture", created_at=1, updated_at=1))
            connection.execute(insert(artifacts).values(id="native-artifact", job_id=job["id"],
                attempt_id="native-attempt", created_at=1, metadata={"kind": "image", "mime": "image/png",
                    "object_key": private["original"]["key"], "size_bytes": len(raw),
                    "sha256": private["original"]["sha256"]}))
        return (asset["content_url"], "/v1/artifacts/native-artifact/content"), raw, private["original"]["key"]

    def test_native_content_matching_account_preserves_range_head_and_attachment(self):
        paths, raw, _ = self.content_fixture()
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).content, raw)  # Existing URL remains valid.
                response = self.client.get(path, params={"expected_account": "superdan"},
                    headers={"Range": "bytes=2-6"})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, raw[2:7])
                self.assertEqual(response.headers["content-range"], f"bytes 2-6/{len(raw)}")
                head = self.client.head(path, params={"expected_account": "superdan"})
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
                self.assertEqual(head.headers["content-length"], str(len(raw)))
                attachment = self.client.get(path, params={"expected_account": "superdan", "download": 1})
                self.assertEqual(attachment.content, raw)
                self.assertTrue(attachment.headers["content-disposition"].startswith("attachment;"))

    def test_native_content_account_switch_and_foreign_or_anonymous_access_fail_before_storage(self):
        paths, _, _ = self.content_fixture()
        self.login("supervan")
        with mock.patch.object(self.app.state.storage, "stat", side_effect=AssertionError("Must not read storage")), \
             mock.patch.object(self.app.state.storage, "open", side_effect=AssertionError("Must not read body")):
            for path in paths:
                for method in ("GET", "HEAD"):
                    response = self.client.request(method, path, params={"expected_account": "superdan"})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.headers["x-authenticated-account"], "supervan")
                    if method == "GET":
                        self.assertEqual(response.json()["code"], "account_context_changed")
                self.assertEqual(self.client.get(path, params={"expected_account": "supervan"}).status_code, 404)
            self.client.cookies.clear()
            for path in paths:
                self.assertEqual(self.client.get(path, params={"expected_account": "superdan"}).status_code, 401)

    def test_native_account_hint_rejects_ambiguity_only_on_exact_content_reads(self):
        paths, _, _ = self.content_fixture()
        invalid = [({"expected_account": ""}, {}), ({"expected_account": " "}, {}),
            ([('expected_account', 'superdan'), ('expected_account', 'superdan')], {}),
            ({"expected_account": "superdan"}, {"X-Expected-Account": "supervan"}),
            ({"expected_account": "superdan"}, [('X-Expected-Account', 'superdan'), ('X-Expected-Account', 'superdan')])]
        with mock.patch.object(self.app.state.storage, "stat", side_effect=AssertionError("Must not read storage")):
            for path in paths:
                for params, headers in invalid:
                    response = self.client.get(path, params=params, headers=headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["code"], "invalid_account_context")
            self.assertEqual(self.client.post(paths[0], params={"expected_account": "supervan"}).status_code, 405)
            self.assertEqual(self.client.get("/v1/assets/not/an/id/content",
                params={"expected_account": "supervan"}).status_code, 404)
        self.assertEqual(self.client.get("/v1/projects/story-one", params={"expected_account": ""}).status_code, 200)
        self.assertEqual(self.client.post("/v1/projects", params={"expected_account": "supervan"},
            json={"project": project("query-does-not-fence-writes")}).status_code, 201)

    def test_native_account_query_never_reinterprets_or_expands_machine_authority(self):
        paths, raw, _ = self.content_fixture()
        token = secrets.token_urlsafe(32)
        auth = self.app.state.auth
        auth.register_client("native-read", token, "superdan", ["story-one"],
            ["projects:read", "assets:read", "jobs:read"])
        self.login("supervan")
        headers = {"Authorization": "Bearer " + token}
        for path in paths:
            self.assertEqual(self.client.get(path, headers=headers).content, raw)
            response = self.client.get(path, headers=headers, params={"expected_account": "supervan"})
            self.assertEqual(response.content, raw)
            self.assertEqual(response.headers["x-authenticated-account"], "superdan")
        for scopes, owner, projects in ((["projects:read"], "superdan", ["story-one"]),
                                       (["assets:read", "jobs:read"], "superdan", ["different"]),
                                       (["assets:read", "jobs:read"], "supervan", ["story-one"])):
            auth.register_client("native-read", token, owner, projects, scopes)
            for path in paths:
                self.assertEqual(self.client.get(path, headers=headers,
                    params={"expected_account": "superdan"}).status_code, 404)

    def test_native_r2_redirect_and_head_require_account_guard_before_presign(self):
        from studio_platform.storage import S3ObjectStore
        from test_platform_storage import config, FakeS3

        paths, raw, key = self.content_fixture()
        selected = config()
        fake = FakeS3(selected)
        store = S3ObjectStore(selected, client=fake)
        store.put(key, io.BytesIO(raw), content_type="image/png")
        remote_app = create_app(self.settings, repository=self.app.state.repository, storage=store)
        with TestClient(remote_app, follow_redirects=False) as remote:
            remote.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
            for path in paths:
                response = remote.get(path, params={"expected_account": "superdan", "download": 1},
                    headers={"Range": "bytes=0-0", "X-Expected-Account": "superdan"})
                self.assertEqual(response.status_code, 307)
                self.assertEqual(fake.calls[-1][0], "presign")
                self.assertTrue(fake.calls[-1][1]["Params"]["ResponseContentDisposition"].startswith("attachment;"))
                head = remote.head(path, params={"expected_account": "superdan"})
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.headers["content-length"], str(len(raw)))
            remote.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
            fake.calls.clear()
            for path in paths:
                self.assertEqual(remote.get(path, params={"expected_account": "superdan"}).status_code, 409)
                self.assertEqual(remote.head(path, params={"expected_account": "superdan"}).status_code, 409)
                self.assertEqual(remote.get(path, params={"expected_account": "supervan"},
                    headers={"X-Expected-Account": "superdan"}).status_code, 400)
            self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
