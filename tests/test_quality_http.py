"""Real HTTP identity/projection boundaries; local records and no providers."""

import copy
import secrets
import unittest
import uuid
from unittest import mock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import insert
from starlette.background import BackgroundTask

import test_platform_api as fixture
from studio_platform.auth import Principal
from studio_platform.quality_http import install
from studio_platform.quick_chat import DEFAULT_SETTINGS, EMPTY_INPUTS, QuickChatError
from studio_platform.repository import Conflict, Scope, jobs


PROFILE = "h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6"
MODEL = "MiniMax-H3-Pruned-Rank8-INT8"
RECIPE = "h3-base-fl2va-v1"
PROMPT = "The sign literally reads " + MODEL + " beside " + PROFILE


def selection():
    return {
        "recipe_id": RECIPE,
        "deployment_profile_id": PROFILE,
        "model_id": MODEL,
        "prompt": PROMPT,
        "controls": {"steps": 9, "cfg_scale": 3, "resolution": "720P"},
    }


def capabilities():
    return {
        "capabilities_version": "sentinel-contract",
        "models": [{"id": MODEL, "source": "private-source"}],
        "recipes": [{"id": RECIPE, "mode": "fl", "model_id": MODEL}],
        "default_deployment_profile_id": PROFILE,
        "deployment_profiles": [
            {
                "id": PROFILE,
                "label": MODEL,
                "source": "private-source",
                "model_id": MODEL,
                "generation_support": {
                    mode: {
                        "configured": True,
                        "enabled": True,
                        "controls": {"steps": {"type": "integer", "maximum": 100}},
                    }
                    for mode in ("fl", "ref")
                },
            }
        ],
    }


class QualityHTTPTests(unittest.TestCase):
    # Reuse the real application fixture, including isolated PostgreSQL schemas
    # when CI supplies its explicitly local test database. Do not inherit its
    # test methods or substitute authentication/projection implementations.
    setUp = fixture.ApiTests.setUp
    login = fixture.ApiTests.login

    def _identity(self, role):
        self.client.cookies.clear()
        self.client.headers.pop("Authorization", None)
        owner = "supervan" if role == "user" else "superdan"
        if role == "machine":
            token = secrets.token_urlsafe(40)
            self.app.state.auth.register_client(
                "tier-http-client",
                token,
                owner,
                ["story-one"],
                ["projects:read", "jobs:read", "jobs:write"],
            )
            self.client.headers["Authorization"] = "Bearer " + token
        elif role != "anonymous":
            self.login(owner)
        return owner

    def _project(self, owner):
        value = fixture.project()
        value["entities"][-1]["description"] = PROMPT
        value["entities"][-1]["data"]["h3"] = {
            "recipeId": RECIPE,
            "deploymentProfileId": PROFILE,
            "model_id": MODEL,
            "controls": {"steps": 9},
        }
        self.app.state.repository.put_document(
            Scope(self.settings.tenant_id, owner, "__projects", "fixture"),
            "project",
            value["id"],
            value,
        )
        return value

    def test_real_roles_project_raw_admin_and_detached_public_aliases(self):
        for owner in ("superdan", "supervan"):
            self._project(owner)
        for role in ("admin", "user", "machine"):
            with self.subTest(role=role):
                owner = self._identity(role)
                response = self.client.get("/v1/projects/story-one")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                shot = response.json()["project"]["entities"][-1]
                self.assertEqual(shot["description"], PROMPT)
                self.assertEqual(shot["data"]["h3"]["controls"]["steps"], 9)
                if role == "admin":
                    self.assertEqual(shot["data"]["h3"]["deploymentProfileId"], PROFILE)
                    self.assertEqual(shot["data"]["h3"]["model_id"], MODEL)
                else:
                    self.assertEqual(
                        shot["data"]["h3"]["deploymentProfileId"], "quality:standard"
                    )
                    self.assertEqual(shot["data"]["h3"]["recipeId"], "video-fl-v1")
                    self.assertNotIn("model_id", shot["data"]["h3"])
                stored = self.app.state.repository.get_document(
                    Scope(self.settings.tenant_id, owner, "__projects"),
                    "project",
                    "story-one",
                )
                self.assertEqual(
                    stored["payload"]["entities"][-1]["data"]["h3"][
                        "deploymentProfileId"
                    ],
                    PROFILE,
                )

    def test_real_caps_and_schema_keep_admin_identity_and_all_public_controls(self):
        # Catalog construction is the backend seam. Projection and route
        # installation, serialization, authentication and headers are real.
        with (
            mock.patch(
                "studio_platform.api.capabilities",
                side_effect=lambda _: copy.deepcopy(capabilities()),
            ),
            mock.patch(
                "studio_platform.quick_chat_routes.capabilities",
                side_effect=lambda _: copy.deepcopy(capabilities()),
            ),
        ):
            for role in ("admin", "user", "machine"):
                with self.subTest(role=role):
                    self._identity(role)
                    response = self.client.get("/v1/capabilities")
                    self.assertEqual(response.status_code, 200, response.text)
                    caps = response.json()
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    schema = self.client.get("/v1/quick-chat/schema")
                    self.assertEqual(schema.status_code, 200, schema.text)
                    if role == "admin":
                        self.assertEqual(caps["deployment_profiles"][0]["id"], PROFILE)
                        self.assertEqual(
                            caps["deployment_profiles"][0]["tier"], "standard"
                        )
                        self.assertIn(MODEL, schema.text)
                    else:
                        self.assertNotIn(MODEL, response.text)
                        self.assertNotIn(PROFILE, response.text)
                        self.assertNotIn("private-source", response.text)
                        self.assertEqual(caps["recipes"][0]["id"], "video-fl-v1")
                        self.assertEqual(
                            [p["tier"] for p in caps["deployment_profiles"]],
                            ["fast", "standard", "refine"],
                        )
                        self.assertNotIn(MODEL, schema.text)
                        self.assertNotIn(PROFILE, schema.text)
                        self.assertEqual(
                            schema.json()["capabilities"]["recipes"][0]["id"],
                            "video-fl-v1",
                        )
                    # The optional conversational model is a separate selector.
                    self.assertTrue(schema.json()["models"])
            self._identity("anonymous")
            response = self.client.get("/v1/capabilities")
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(MODEL, response.text)

    def test_actual_saved_job_serializer_preserves_ids_money_and_frozen_request(self):
        repo = self.app.state.repository
        identities = {}
        for owner in ("superdan", "supervan"):
            self._project(owner)
            compiled = {
                **selection(),
                "client_ref": {"project_id": "story-one", "shot_id": "shot-one"},
                "request": {"model": MODEL, "mode": "fl", "prompt": PROMPT, "steps": 9},
            }
            plan = repo.create_plan(
                Scope(self.settings.tenant_id, owner, "story-one", "fixture"),
                compiled,
                {"backend": "mock", "enabled": True},
                expires_at=repo.clock() + 600,
                estimated_cost_microusd=12345,
            )
            ident = str(uuid.uuid4())
            with repo.engine.begin() as connection:
                connection.execute(
                    insert(jobs).values(
                        id=ident,
                        tenant_id=self.settings.tenant_id,
                        owner_id=owner,
                        project_id="story-one",
                        actor_id="fixture",
                        idempotency_key="saved-job",
                        request_hash=plan["request_hash"],
                        plan_id=plan["id"],
                        request=compiled,
                        execution_plan=plan["execution_plan"],
                        status="succeeded",
                        pool="fixture",
                        expected_runtime_s=60,
                        estimated_cost_microusd=12345,
                        created_at=repo.clock(),
                        updated_at=repo.clock(),
                        not_before=0,
                        attempt_no=0,
                        fence=0,
                        result={"duration": 5, "actual_cost_microusd": 12000},
                    )
                )
            identities[owner] = ident
        for role in ("admin", "user", "machine"):
            with self.subTest(role=role):
                owner = self._identity(role)
                response = self.client.get("/v1/jobs/" + identities[owner])
                self.assertEqual(response.status_code, 200, response.text)
                value = response.json()
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(value["id"], identities[owner])
                self.assertEqual(value["effective_request"]["prompt"], PROMPT)
                self.assertEqual(value["result"]["actual_cost_microusd"], 12000)
                self.assertEqual(
                    value["request_hash"],
                    repo.get_job_for_owner(
                        self.settings.tenant_id, owner, identities[owner]
                    )["request_hash"],
                )
                if role == "admin":
                    self.assertEqual(value["effective_request"]["model"], MODEL)
                    self.assertEqual(value["deployment_profile_id"], PROFILE)
                else:
                    self.assertNotIn("model", value["effective_request"])
                    self.assertEqual(value["deployment_profile_id"], "quality:standard")
                    self.assertEqual(value["recipe_id"], "video-fl-v1")
                self.assertEqual(
                    repo.get_job_for_owner(
                        self.settings.tenant_id, owner, identities[owner]
                    )["request"]["request"]["model"],
                    MODEL,
                )

    def test_quick_chat_timeline_real_saved_revision_and_session_projection(self):
        service = self.app.state.quick_chat
        self._identity("admin")
        principal = Principal("superdan", "fixture")
        session = service.create_session(principal, {}, "tier-session")["session"]
        revision = service.save_card(
            principal,
            session["id"],
            {
                **DEFAULT_SETTINGS,
                "deployment_profile_id": PROFILE,
                "prompt": PROMPT,
                "copies": 1,
                "inputs": copy.deepcopy(EMPTY_INPUTS),
            },
            "tier-card",
        )["revision"]
        for role in ("admin", "machine"):
            with self.subTest(role=role):
                self._identity(role)
                # Legacy client keys intentionally list explicit projects.
                if role == "machine":
                    self.app.state.auth.register_client(
                        "tier-http-client",
                        self.client.headers["Authorization"].split()[1],
                        "superdan",
                        [
                            service._access(principal, session["id"], "projects:read")[
                                "payload"
                            ]["project_id"]
                        ],
                        ["projects:read", "jobs:read", "jobs:write"],
                    )
                response = self.client.get(
                    "/v1/quick-chat/sessions/" + session["id"] + "/timeline"
                )
                self.assertEqual(response.status_code, 200, response.text)
                record = next(
                    e["record"]
                    for e in response.json()["events"]
                    if e["type"].startswith("card.")
                )
                self.assertEqual(record["id"], revision["id"])
                self.assertEqual(record["prompt"], PROMPT)
                self.assertEqual(record["input_hash"], revision["input_hash"])
                if role == "admin":
                    self.assertEqual(record["deployment_profile_id"], PROFILE)
                else:
                    self.assertEqual(
                        record["deployment_profile_id"], "quality:standard"
                    )
                    self.assertEqual(record["recipe_id"], "video-fl-v1")
                current = self.client.get("/v1/quick-chat/sessions/" + session["id"])
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.headers["Cache-Control"], "no-store")
                self.assertEqual(
                    current.json()["session"]["model_id"], session["model_id"]
                )
        self.assertEqual(
            service.get_revision(principal, session["id"], revision["id"])[
                "deployment_profile_id"
            ],
            PROFILE,
        )

    def test_exception_handlers_project_after_route_failure_and_preserve_status(self):
        for owner in ("superdan", "supervan"):
            self._project(owner)
        for role in ("admin", "user", "machine"):
            self._identity(role)
            for error, status in (
                (ValueError("unsafe " + MODEL + " " + PROFILE), 422),
                (Conflict("unsafe " + MODEL + " " + PROFILE), 409),
                (
                    QuickChatError(
                        "unsupported_controls", "unsafe " + MODEL + " " + PROFILE, 422
                    ),
                    422,
                ),
            ):
                with (
                    self.subTest(role=role, status=status),
                    mock.patch.object(
                        self.app.state.repository, "get_document", side_effect=error
                    ),
                ):
                    response = self.client.get("/v1/projects/story-one")
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    if role == "admin":
                        self.assertIn(MODEL, response.text)
                    else:
                        self.assertNotIn(MODEL, response.text)
                        self.assertNotIn(PROFILE, response.text)
            with mock.patch.object(
                self.app.state.repository,
                "get_document",
                side_effect=Conflict("document_version_conflict"),
            ):
                response = self.client.get("/v1/projects/story-one")
                self.assertEqual(response.status_code, 409)
                self.assertIn("document_version_conflict", response.text)


class QualityHTTPTransportTests(unittest.TestCase):
    def test_json_and_media_keep_status_headers_background_and_stream_bytes(self):
        app = FastAPI()
        app.state.settings = None
        completed = []

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.principal = Principal(
                "owner", "pat", machine=True, role="admin", identity_source="canonical"
            )
            return await call_next(request)

        @app.get("/v1/jobs/transport")
        def json_record():
            return JSONResponse(
                {"job": {"id": "job", "status": "running", **selection()}},
                status_code=202,
                headers={"Cache-Control": "no-store", "ETag": "original"},
                background=BackgroundTask(completed.append, "json"),
            )

        @app.get("/v1/artifacts/video/content")
        def media():
            return StreamingResponse(
                iter([b"original", b"-video"]),
                status_code=206,
                media_type="video/mp4",
                headers={
                    "Cache-Control": "private, max-age=60",
                    "Content-Range": "bytes 0-13/14",
                    "Content-Disposition": "attachment; filename=original.mp4",
                },
                background=BackgroundTask(completed.append, "media"),
            )

        @app.get("/v1/artifacts/redirect/content")
        def redirect():
            return RedirectResponse(
                "https://example.invalid/original",
                status_code=307,
                headers={"Cache-Control": "private, max-age=60"},
                background=BackgroundTask(completed.append, "redirect"),
            )

        install(app)
        with TestClient(app) as client:
            response = client.get("/v1/jobs/transport")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["ETag"], "original")
            self.assertEqual(
                response.json()["job"]["deployment_profile_id"], "quality:standard"
            )
            self.assertEqual(response.json()["job"]["prompt"], PROMPT)
            self.assertEqual(
                int(response.headers["Content-Length"]), len(response.content)
            )
            media_response = client.get("/v1/artifacts/video/content")
            self.assertEqual(media_response.status_code, 206)
            self.assertEqual(media_response.content, b"original-video")
            self.assertEqual(media_response.headers["Content-Range"], "bytes 0-13/14")
            self.assertEqual(
                media_response.headers["Content-Disposition"],
                "attachment; filename=original.mp4",
            )
            self.assertEqual(
                media_response.headers["Cache-Control"], "private, max-age=60"
            )
            redirect_response = client.get(
                "/v1/artifacts/redirect/content", follow_redirects=False
            )
            self.assertEqual(redirect_response.status_code, 307)
            self.assertEqual(
                redirect_response.headers["Location"],
                "https://example.invalid/original",
            )
            self.assertEqual(
                redirect_response.headers["Cache-Control"], "private, max-age=60"
            )
        self.assertEqual(completed, ["json", "media", "redirect"])


if __name__ == "__main__":
    unittest.main()
