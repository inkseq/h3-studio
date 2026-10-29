"""Real HTTP identity/projection boundaries; local records and no providers."""

import copy
import secrets
import unittest
import uuid
from unittest import mock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from starlette.background import BackgroundTask

import test_platform_api as fixture
from studio_platform.auth import Principal
from studio_platform.capabilities import VERSION, compile_request, public_version
from studio_platform.quality_http import install
from studio_platform.quick_chat import (
    DEFAULT_SETTINGS,
    EMPTY_INPUTS,
    QuickChatError,
    objects,
    operations,
)
from studio_platform.repository import Conflict, Scope, jobs, plans, request_hash


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

    def _assistant_machine(self):
        self._identity("admin")
        response = self.client.post(
            "/v1/api-keys",
            json={
                "name": "Assistant alias test",
                "scopes": [
                    "projects:create",
                    "projects:read",
                    "projects:write",
                    "assistant:run",
                ],
                "all_projects": True,
                "project_ids": [],
                "expires_in_days": 1,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.client.cookies.clear()
        self.client.headers["Authorization"] = "Bearer " + response.json()["api_key"]

    def test_assistant_catalog_is_neutral_for_users_and_admin_owned_agents(self):
        from studio_platform.quick_chat_assistant import model_schema

        original = model_schema()
        aliases = {"assistant:text-image-v1", "assistant:multimodal-v1"}
        for role in ("admin", "user", "machine", "admin"):
            with self.subTest(role=role):
                if role == "machine":
                    self._assistant_machine()
                else:
                    self._identity(role)
                response = self.client.get("/v1/quick-chat/schema")
                self.assertEqual(response.status_code, 200, response.text)
                shown = response.json()
                if role == "admin":
                    self.assertEqual(shown["models"], original)
                    self.assertEqual(
                        shown["history_titles"]["model_id"], "gemma-4-31b-it"
                    )
                else:
                    self.assertEqual({item["id"] for item in shown["models"]}, aliases)
                    self.assertEqual(
                        shown["history_titles"]["model_id"], "assistant:text-image-v1"
                    )
                    for name in (
                        "gemma-4-31b-it",
                        "gemini-3.8-flash",
                        "Gemma",
                        "Gemini",
                    ):
                        self.assertNotIn(name, response.text)
                    support = {item["id"]: item["inputs"] for item in shown["models"]}
                    self.assertFalse(
                        support["assistant:text-image-v1"]["video"]["implemented"]
                    )
                    self.assertTrue(
                        support["assistant:multimodal-v1"]["video"]["implemented"]
                    )
        self.assertEqual(model_schema(), original)

    def test_assistant_alias_requests_preserve_raw_storage_and_original_wire_replay(
        self,
    ):
        self._identity("user")
        path = "/v1/quick-chat/sessions"
        body = {
            "title": "User wrote gemma-4-31b-it",
            "model_id": "assistant:text-image-v1",
        }
        headers = {"Idempotency-Key": "assistant-session"}
        response = self.client.post(path, json=body, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        session = response.json()["session"]
        self.assertEqual(session["model_id"], "assistant:text-image-v1")
        self.assertEqual(session["title"], body["title"])
        sid = session["id"]
        with mock.patch(
            "studio_platform.quick_chat.resolve_assistant", return_value=None
        ):
            self.assertEqual(
                self.client.post(path, json=body, headers=headers).json()["session"][
                    "id"
                ],
                sid,
            )
        conflict = self.client.post(
            path, json={**body, "model_id": "gemma-4-31b-it"}, headers=headers
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        patch = {
            "expected_version": session["version"],
            "model_id": "assistant:multimodal-v1",
        }
        response = self.client.patch(
            path + "/" + sid, json=patch, headers={"Idempotency-Key": "assistant-patch"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["session"]["model_id"], "assistant:multimodal-v1"
        )
        turn_body = {
            "expected_version": response.json()["session"]["version"],
            "text": "User wrote Gemini 3.8 Flash",
            "model_id": "assistant:text-image-v1",
            "assistant_mode": "none",
        }
        turns = path + "/" + sid + "/turns"
        turn_headers = {"Idempotency-Key": "assistant-turn"}
        response = self.client.post(turns, json=turn_body, headers=turn_headers)
        self.assertEqual(response.status_code, 201, response.text)
        turn = response.json()
        self.assertEqual(turn["model_id"], "assistant:text-image-v1")
        self.assertEqual(turn["text"], turn_body["text"])
        with mock.patch(
            "studio_platform.quick_chat.resolve_assistant", return_value=None
        ):
            replay = self.client.post(turns, json=turn_body, headers=turn_headers)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["id"], turn["id"])
        changed = self.client.post(
            turns,
            json={**turn_body, "model_id": "gemma-4-31b-it"},
            headers=turn_headers,
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        read = self.client.get(turns + "/" + turn["id"])
        self.assertEqual(read.json()["model_id"], "assistant:text-image-v1")
        listed = self.client.get(path).json()["sessions"]
        self.assertEqual(listed[0]["model_id"], "assistant:text-image-v1")
        events = self.client.get(path + "/" + sid + "/timeline").json()["events"]
        recorded = next(
            item["record"] for item in events if item["type"] == "turn.created"
        )
        self.assertEqual(recorded["model_id"], "assistant:text-image-v1")
        with self.app.state.repository.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(objects).where(objects.c.id.in_([sid, turn["id"]]))
                ).mappings()
            )
            stored = {row["id"]: row["payload"] for row in rows}
            self.assertEqual(stored[sid]["model_id"], "gemma-4-31b-it")
            self.assertEqual(stored[turn["id"]]["model_id"], "gemma-4-31b-it")
            fingerprints = dict(
                conn.execute(select(operations.c.key, operations.c.fingerprint)).all()
            )
            self.assertEqual(fingerprints["assistant-session"], request_hash(body))
            self.assertEqual(fingerprints["assistant-turn"], request_hash(turn_body))
        invalid = self.client.patch(
            path + "/" + sid,
            json={
                "expected_version": listed[0]["version"],
                "model_id": "assistant:unavailable",
            },
            headers={"Idempotency-Key": "unavailable-assistant"},
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self._identity("admin")
        self.assertEqual(self.client.get(path + "/" + sid).status_code, 404)
        self.assertEqual(self.client.get("/v1/jobs").json()["jobs"], [])

    def test_agent_assistant_alias_resolves_to_original_provider_once(self):
        from test_platform_quick_chat import FakeAssistant

        self._assistant_machine()
        service = self.app.state.quick_chat
        service.assistant = FakeAssistant()
        service.assistant_enabled = True
        response = self.client.post(
            "/v1/quick-chat/sessions",
            json={
                "title": "Alias provider test",
                "model_id": "assistant:multimodal-v1",
            },
            headers={"Idempotency-Key": "agent-assistant-session"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        session = response.json()["session"]
        body = {
            "expected_version": session["version"],
            "text": "Discuss a blue cup",
            "model_id": "assistant:multimodal-v1",
            "assistant_mode": "discuss",
        }
        path = "/v1/quick-chat/sessions/" + session["id"] + "/turns"
        headers = {"Idempotency-Key": "agent-assistant-turn"}
        response = self.client.post(path, json=body, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["model_id"], "assistant:multimodal-v1")
        self.assertEqual(len(service.assistant.calls), 1)
        self.assertEqual(service.assistant.calls[0][0], "gemini-3.8-flash")
        with mock.patch(
            "studio_platform.quick_chat.resolve_assistant", return_value=None
        ):
            replay = self.client.post(path, json=body, headers=headers)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["id"], response.json()["id"])
        self.assertEqual(len(service.assistant.calls), 1)

    def test_builtin_openapi_projects_per_role_without_changing_cached_schema(self):
        original = self.app.openapi()
        frozen = copy.deepcopy(original)
        self.assertIn(
            RECIPE,
            original["components"]["schemas"]["CreateCardRequest"]["properties"][
                "recipe_id"
            ]["anyOf"][0]["enum"],
        )
        for role in ("admin", "user", "machine", "admin"):
            with self.subTest(role=role):
                self._identity(role)
                response = self.client.get("/openapi.json")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                public = response.json()
                for name in ("CreateCardRequest", "ReviseCardRequest"):
                    recipe = public["components"]["schemas"][name]["properties"][
                        "recipe_id"
                    ]
                    if role == "admin":
                        self.assertIn(RECIPE, recipe["anyOf"][0]["enum"])
                        self.assertEqual(public, frozen)
                    else:
                        self.assertEqual(
                            recipe["anyOf"][0]["enum"], ["video-fl-v1", "video-ref-v1"]
                        )
                        self.assertNotIn(RECIPE, str(public["components"]["schemas"]))
                self.assertIs(self.app.openapi(), original)
                self.assertEqual(original, frozen)
        self._identity("anonymous")
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(RECIPE, response.text)

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

    def test_actual_render_plan_post_projects_creator_without_changing_saved_plan(self):
        from dataclasses import replace
        from studio_platform.generation_services import GenerationRead
        from test_platform_render_plans import BODY, story

        repo = self.app.state.repository
        policies = self.app.state.generation_planning.policies
        project = story()
        project["entities"][2]["data"]["seconds"] = 9
        asset = {
            "status": "ready",
            "kind": "video",
            "metadata": {"source_duration": 9},
            "original": {
                "key": "owners/synthetic/assets/clip.mp4",
                "sha256": "a" * 64,
                "size_bytes": 1234,
                "content_type": "video/mp4",
            },
        }
        for owner in ("superdan", "supervan"):
            repo.put_document(
                Scope(self.settings.tenant_id, owner, "__projects", "fixture"),
                "project",
                project["id"],
                project,
            )

        unchanged = None
        with (
            mock.patch.object(
                policies, "settings", replace(policies.settings, render_enabled=True)
            ),
            mock.patch.object(
                policies.control, "pool_status", return_value={"ready": 1, "busy": 0}
            ),
            mock.patch.object(self.app.state.assets, "get", return_value=asset),
        ):
            # Real auth, actual POST route, compiler, persistence and installed
            # HTTP projection. Only media/capacity reads are synthetic seams.
            for role in ("user", "admin", "machine"):
                with self.subTest(role=role):
                    owner = self._identity(role)
                    response = self.client.post("/v1/render-plans", json=BODY)
                    self.assertEqual(response.status_code, 201, response.text)
                    shown = response.json()
                    self.assertEqual(shown["status"], "ready", shown)
                    self.assertFalse(shown["simulation"])
                    self.assertEqual(shown["blockers"], [])
                    self.assertTrue(shown["execution"]["enabled"])
                    self.assertEqual(shown["output_spec"]["frame_count"], 216)
                    self.assertEqual(shown["timeline"]["duration"], 9)
                    self.assertEqual(shown["estimate"]["cost_microusd"], 0)
                    stored = repo.get_plan(
                        Scope(self.settings.tenant_id, owner, "story-one"),
                        shown["plan_id"],
                    )
                    frozen = copy.deepcopy(stored)
                    self.assertEqual(shown["request_hash"], stored["request_hash"])
                    self.assertEqual(
                        stored["request_hash"], request_hash(stored["request"])
                    )
                    self.assertEqual(
                        stored["plan_hash"], request_hash(stored["execution_plan"])
                    )
                    self.assertEqual(
                        shown["output_spec"], stored["request"]["output_spec"]
                    )
                    self.assertEqual(stored["execution_plan"]["backend"], "cpu-render")
                    self.assertEqual(
                        stored["execution_plan"]["configuration_id"], "cpu-render-v3"
                    )
                    if role == "admin":
                        self.assertEqual(
                            shown["execution"],
                            GenerationRead.execution(stored["execution_plan"]),
                        )
                        self.assertEqual(shown["execution"]["backend"], "cpu-render")
                    else:

                        def check_public(value):
                            if isinstance(value, dict):
                                self.assertFalse(
                                    {
                                        "backend",
                                        "configuration_id",
                                        "model",
                                        "model_id",
                                        "provider",
                                    }
                                    & value.keys()
                                )
                                for item in value.values():
                                    check_public(item)
                            elif isinstance(value, list):
                                for item in value:
                                    check_public(item)

                        check_public(shown)
                    visible = {
                        key: shown[key]
                        for key in ("output_spec", "timeline", "estimate")
                    }
                    if unchanged is None:
                        unchanged = visible
                    self.assertEqual(visible, unchanged)
                    self.assertEqual(
                        repo.get_plan(
                            Scope(self.settings.tenant_id, owner, "story-one"),
                            shown["plan_id"],
                        ),
                        frozen,
                    )
            self._identity("anonymous")
            count = self._plan_count()
            self.assertEqual(
                self.client.post("/v1/render-plans", json=BODY).status_code, 401
            )
            self.assertEqual(self._plan_count(), count)
        with repo.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(jobs.c.id))), [])

    def _version_project(self):
        self._identity("user")
        response = self.client.post("/v1/projects", json={"project": fixture.project()})
        self.assertEqual(response.status_code, 201, response.text)
        version = self.client.get("/v1/capabilities").json()["capabilities_version"]
        self.assertEqual(version, public_version(VERSION))
        self.assertNotEqual(version, VERSION)
        return version

    def _plan_count(self):
        with self.app.state.repository.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(plans))

    def test_public_capability_version_compiles_and_round_trips_direct_and_saved_preflight(
        self,
    ):
        version = self._version_project()
        repo = self.app.state.repository
        body = fixture.generation_request()
        raw, raw_hash = compile_request(body, lambda _: None)
        public, public_hash = compile_request(
            {**body, "capabilities_version": version}, lambda _: None
        )
        self.assertEqual(public, raw)
        self.assertEqual(public_hash, raw_hash)
        self.assertEqual(public["capabilities_version"], VERSION)

        retained = []
        for supplied in (version, VERSION):
            response = self.client.post(
                "/v1/generation-plans",
                json={**body, "capabilities_version": supplied},
            )
            self.assertEqual(response.status_code, 201, response.text)
            stored = repo.get_plan(
                Scope(self.settings.tenant_id, "supervan", "story-one"),
                response.json()["plan_id"],
            )
            self.assertEqual(stored["request"]["capabilities_version"], VERSION)
            self.assertEqual({key: stored["request"][key] for key in raw}, raw)
            self.assertEqual(stored["request_hash"], request_hash(stored["request"]))
            retained.append(stored)
            response = self.client.post(
                "/v1/projects/story-one/shots/shot-one/generation-plans",
                json={"expected_version": 1, "capabilities_version": supplied},
            )
            self.assertEqual(response.status_code, 201, response.text)
            saved = repo.get_plan(
                Scope(self.settings.tenant_id, "supervan", "story-one"),
                response.json()["plan_id"],
            )
            self.assertEqual(saved["request"]["capabilities_version"], VERSION)
        self.assertEqual(retained[0]["request"], retained[1]["request"])
        self.assertEqual(retained[0]["request_hash"], retained[1]["request_hash"])
        count = self._plan_count()
        stale = public_version(VERSION + "-stale")
        for path, invalid in (
            ("/v1/generation-plans", {**body, "capabilities_version": stale}),
            (
                "/v1/projects/story-one/shots/shot-one/generation-plans",
                {"expected_version": 1, "capabilities_version": stale},
            ),
        ):
            response = self.client.post(path, json=invalid)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._plan_count(), count)
        self.assertEqual(self.client.get("/v1/jobs").json()["jobs"], [])

    def test_machine_quick_chat_preflight_accepts_public_version_and_preserves_original_replay(
        self,
    ):
        self._identity("admin")
        session_response = self.client.post(
            "/v1/quick-chat/sessions",
            json={},
            headers={"Idempotency-Key": "version-session"},
        )
        self.assertEqual(session_response.status_code, 201, session_response.text)
        session_id = session_response.json()["session"]["id"]
        authored = self.client.post(
            "/v1/quick-chat/sessions/" + session_id + "/cards",
            json={
                **DEFAULT_SETTINGS,
                "prompt": "A blue paper bird unfolds on a desk.",
                "copies": 1,
                "inputs": copy.deepcopy(EMPTY_INPUTS),
            },
            headers={"Idempotency-Key": "version-card"},
        )
        self.assertEqual(authored.status_code, 201, authored.text)
        revision = authored.json()["revision"]
        service, repo = self.app.state.quick_chat, self.app.state.repository
        principal = Principal("superdan", "fixture")
        project_id = service._access(principal, session_id, "projects:read")["payload"][
            "project_id"
        ]
        self._identity("machine")
        self.app.state.auth.register_client(
            "tier-http-client",
            self.client.headers["Authorization"].split()[1],
            "superdan",
            [project_id],
            ["projects:read", "jobs:read", "jobs:write"],
        )
        version = self.client.get("/v1/quick-chat/schema").json()["capabilities"][
            "capabilities_version"
        ]
        self.assertEqual(version, public_version(VERSION))
        path = (
            "/v1/quick-chat/sessions/"
            + session_id
            + "/revisions/"
            + revision["id"]
            + "/preflights"
        )
        for index, supplied in enumerate((version, VERSION)):
            body = {
                "capabilities_version": supplied,
                "revision_hash": revision["input_hash"],
            }
            headers = {"Idempotency-Key": "version-preflight-" + str(index)}
            response = self.client.post(path, json=body, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            plan_id = response.json()["items"][0]["plan"]["plan_id"]
            scope = Scope(self.settings.tenant_id, "superdan", project_id)
            stored = repo.get_plan(scope, plan_id)
            self.assertEqual(stored["request"]["capabilities_version"], VERSION)
            count = self._plan_count()
            replay = self.client.post(path, json=body, headers=headers)
            self.assertEqual(replay.status_code, 201, replay.text)
            self.assertEqual(replay.json()["id"], response.json()["id"])
            self.assertEqual(repo.get_plan(scope, plan_id), stored)
            self.assertEqual(self._plan_count(), count)
        count = self._plan_count()
        stale = self.client.post(
            path,
            json={
                "capabilities_version": public_version(VERSION + "-stale"),
                "revision_hash": revision["input_hash"],
            },
            headers={"Idempotency-Key": "version-stale-preflight"},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "preflight_stale")
        self.assertEqual(self._plan_count(), count)
        self.assertEqual(
            service.get_revision(principal, session_id, revision["id"])["input_hash"],
            revision["input_hash"],
        )
        self.assertEqual(self.client.get("/v1/jobs").json()["jobs"], [])

    def test_storyboard_preflight_accepts_public_version_and_refuses_stale_without_new_plan(
        self,
    ):
        version = self._version_project()
        repo = self.app.state.repository
        path = "/v1/projects/story-one/storyboard/preflights"
        for index, supplied in enumerate((version, VERSION)):
            body = {
                "expected_version": 1,
                "shot_ids": ["shot-one"],
                "copies": 1,
                "capabilities_version": supplied,
            }
            headers = {"Idempotency-Key": "story-version-preflight-" + str(index)}
            response = self.client.post(path, json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            plan_id = response.json()["items"][0]["plan_id"]
            scope = Scope(self.settings.tenant_id, "supervan", "story-one")
            stored = repo.get_plan(scope, plan_id)
            self.assertEqual(stored["request"]["capabilities_version"], VERSION)
            count = self._plan_count()
            replay = self.client.post(path, json=body, headers=headers)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["id"], response.json()["id"])
            self.assertEqual(repo.get_plan(scope, plan_id), stored)
            self.assertEqual(self._plan_count(), count)
        count = self._plan_count()
        stale = self.client.post(
            path,
            json={
                "expected_version": 1,
                "shot_ids": ["shot-one"],
                "copies": 1,
                "capabilities_version": public_version(VERSION + "-stale"),
            },
            headers={"Idempotency-Key": "story-version-stale"},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("capabilities_changed", stale.text)
        self.assertEqual(self._plan_count(), count)
        self.assertEqual(self.client.get("/v1/jobs").json()["jobs"], [])

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
                if role == "admin":
                    self.assertEqual(shot["data"]["h3"]["controls"]["steps"], 9)
                    self.assertEqual(shot["data"]["h3"]["deploymentProfileId"], PROFILE)
                    self.assertEqual(shot["data"]["h3"]["model_id"], MODEL)
                else:
                    self.assertEqual(
                        shot["data"]["h3"]["deploymentProfileId"], "quality:standard"
                    )
                    self.assertEqual(shot["data"]["h3"]["recipeId"], "video-fl-v1")
                    self.assertNotIn("model_id", shot["data"]["h3"])
                    # Tuning stays ours; the stored document keeps it (#798 B8b).
                    self.assertNotIn("steps", shot["data"]["h3"]["controls"])
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
                    current.json()["session"]["model_id"],
                    session["model_id"]
                    if role == "admin"
                    else "assistant:multimodal-v1",
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
