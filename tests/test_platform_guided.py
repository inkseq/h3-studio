"""Agent creation/editing and account-managed keys on disposable local state."""

from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
from pathlib import Path
import re
import secrets
import unittest
from unittest.mock import patch
import wave
import zipfile

from sqlalchemy import insert, select, update

from studio_platform.auth import API_SCOPES, Auth, accounts, personal_keys
from studio_platform.repository import artifacts, jobs, attempts
import test_platform_api as fixtures
from test_platform_api import png, project


class GuidedTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login
    setup_project = fixtures.ApiTests.setup_project
    make_plan = fixtures.ApiTests.make_plan

    def key(self, *, scopes=None, all_projects=True, project_ids=None, days=90):
        response = self.client.post(
            "/v1/api-keys",
            json={
                "name": "Test agent",
                "scopes": list(API_SCOPES) if scopes is None else scopes,
                "all_projects": all_projects,
                "project_ids": [] if project_ids is None else project_ids,
                "expires_in_days": days,
            },
        )
        self.assertEqual(response.status_code, 201)
        value = response.json()
        return value["key"], {"Authorization": "Bearer " + value["api_key"]}

    def edit(self, actions, version=1, headers=None, ident="story-one", key=None):
        return self.client.post(
            f"/v1/projects/{ident}/actions",
            json={"expected_version": version, "actions": actions},
            headers={**(headers or {}), **({"Idempotency-Key": key} if key else {})},
        )

    def test_large_sound_track_edits_keep_cas_and_atomic_limit(self):
        self.setup_project()
        tracks = [{"id": f"track-{i}", "muted": True} for i in range(128)]
        action = {"op": "sound.set", "chapter_id": "chapter-one", "tracks": tracks}
        accepted = self.edit([action], version=1)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.edit([action], version=1).status_code, 409)
        oversized = {**action, "tracks": tracks + [{"id": "extra", "muted": True}]}
        refused = self.edit([oversized], version=2)
        self.assertEqual(refused.status_code, 422, refused.text)
        saved = self.client.get("/v1/projects/story-one").json()
        self.assertEqual(saved["version"], 2)
        self.assertEqual(
            saved["project"]["journey"]["soundTracks"]["chapter-one"], tracks
        )
        schema = self.client.get("/v1/guided-schema").json()
        self.assertEqual(
            schema["operation_schemas"]["sound.set"]["properties"]["tracks"][
                "maxItems"
            ],
            128,
        )

    def test_shipped_story_card_example_uses_agent_actions_without_generation(self):
        reference = (
            Path(__file__).resolve().parents[1] / "skills/inkseq/reference/api.md"
        ).read_text(encoding="utf-8")
        section = reference.split("### Unified card binding example", 1)[1]
        body = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
        self.login()
        _, headers = self.key()
        created = self.client.post(
            "/v1/projects", json={"title": "Agent card fixture"}, headers=headers
        )
        self.assertEqual(created.status_code, 201, created.text)
        created = created.json()
        body["expected_version"] = created["version"]
        with self.app.state.repository.engine.connect() as conn:
            before = conn.execute(select(jobs)).mappings().all()
        edited = self.client.post(
            f"/v1/projects/{created['id']}/actions",
            json=body,
            headers={**headers, "Idempotency-Key": "documented-card-bindings"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        project_value = self.client.get(
            f"/v1/projects/{created['id']}", headers=headers
        ).json()["project"]
        from studio_platform.generation_draft import read_draft

        draft = read_draft(project_value, "shot-letter")[0]
        shot = next(e for e in project_value["entities"] if e["id"] == "shot-letter")
        self.assertEqual(
            shot["data"]["cast"],
            [{"characterId": "character-mia", "lookId": "raincoat"}],
        )
        self.assertEqual(shot["data"]["storyboard"]["mode"], "ref")
        self.assertEqual(
            shot["data"]["storyboard"]["place"],
            {"locationId": "location-platform", "versionId": "night"},
        )
        self.assertEqual(shot["data"]["storyboard"]["propIds"], ["prop-letter"])
        self.assertTrue(
            {"cast", "place", "prop"}.issubset(
                {item["kind"] for item in draft["storyboard"]["prompt_parts"]}
            )
        )
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs)).mappings().all(), before)

    def test_keys_only_hashed_once_response_and_owner_isolated(self):
        self.login()
        meta, headers = self.key()
        token = headers["Authorization"][7:]
        self.assertNotIn("api_key", meta)
        with self.app.state.repository.engine.connect() as conn:
            row = conn.execute(select(personal_keys)).mappings().one()
        self.assertNotEqual(row["token_hash"], token)
        self.assertNotIn(token, json.dumps(dict(row)))
        listed = self.client.get("/v1/api-keys")
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(token, listed.text)
        self.assertEqual(listed.headers["cache-control"], "no-store")
        for method, path in (
            ("get", "/v1/api-keys"),
            ("post", "/v1/api-keys"),
            ("delete", "/v1/api-keys/" + meta["id"]),
        ):
            kwargs = {"json": {}} if method == "post" else {}
            self.assertEqual(
                getattr(self.client, method)(
                    path, headers=headers, **kwargs
                ).status_code,
                403,
            )
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/api-keys").json()["api_keys"], [])
        self.assertEqual(
            self.client.delete("/v1/api-keys/" + meta["id"]).status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/auth/me", headers=headers).json()["username"],
            "superdan",
        )
        self.login()
        used = self.client.get("/v1/api-keys").json()["api_keys"][0]
        self.assertIsNotNone(used["last_used_at"])
        self.assertEqual(
            self.client.delete("/v1/api-keys/" + meta["id"]).status_code, 200
        )
        self.assertEqual(
            self.client.get("/v1/projects", headers=headers).status_code, 401
        )

    def test_expiry_and_local_keys_cannot_upgrade_to_password_mode(self):
        self.login()
        _, headers = self.key(days=1)
        token = headers["Authorization"][7:]
        self.assertIsNone(
            Auth(self.app.state.repository.engine, mode="password").bearer(token)
        )
        with self.app.state.repository.engine.begin() as conn:
            conn.execute(update(personal_keys).values(expires_at=0))
        self.assertEqual(
            self.client.get("/v1/projects", headers=headers).status_code, 401
        )

    def test_password_change_revokes_own_sessions_keys_only(self):
        auth = self.app.state.auth
        password, replacement = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        for username in ("superdan", "supervan"):
            auth.set_password(username, password)
        auth.mode = "password"
        response = self.client.post(
            "/api/auth/login", json={"username": "superdan", "password": password}
        )
        self.assertEqual(response.status_code, 200)
        _, headers = self.key()
        old_cookie = self.client.cookies.get("sixnine_session")
        other = auth.login("supervan", password, source="other-password-test")
        self.assertEqual(
            self.client.post(
                "/v1/auth/password",
                json={"old_password": password, "new_password": replacement},
                headers=headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/v1/auth/password",
                json={"old_password": "wrong", "new_password": replacement},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/v1/auth/password",
                json={"old_password": password, "new_password": "short"},
            ).status_code,
            422,
        )
        changed = self.client.post(
            "/v1/auth/password",
            json={"old_password": password, "new_password": replacement},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json(), {"changed": True, "reauthenticate": True})
        self.assertIsNone(auth.session(old_cookie))
        self.assertIsNone(auth.bearer(headers["Authorization"][7:]))
        self.assertIsNotNone(auth.session(other))
        self.assertEqual(self.client.get("/v1/api-keys").status_code, 401)
        self.client.post(
            "/api/auth/login", json={"username": "superdan", "password": replacement}
        ).raise_for_status()
        self.assertIsNotNone(
            self.client.get("/v1/api-keys").json()["api_keys"][0]["revoked_at"]
        )

    def test_key_permission_and_project_selection_validation(self):
        self.setup_project()
        for body in [
            {"name": "x", "scopes": ["admin"], "all_projects": True},
            {"name": "x", "scopes": ["projects:write"], "all_projects": True},
            {
                "name": "x",
                "scopes": ["projects:create"],
                "all_projects": False,
                "project_ids": ["story-one"],
            },
            {
                "name": "x",
                "scopes": ["projects:read"],
                "all_projects": True,
                "project_ids": ["story-one"],
            },
            {
                "name": "x",
                "scopes": ["projects:read"],
                "all_projects": True,
                "expires_in_days": 0,
            },
            {
                "name": "x",
                "scopes": ["projects:read"],
                "all_projects": True,
                "expires_in_days": True,
            },
        ]:
            self.assertEqual(
                self.client.post("/v1/api-keys", json=body).status_code, 422
            )
        self.assertEqual(
            self.client.post(
                "/v1/api-keys",
                json={
                    "name": "x",
                    "scopes": ["projects:read"],
                    "project_ids": ["missing"],
                    "all_projects": False,
                },
            ).status_code,
            404,
        )

    def test_existing_write_only_key_cannot_read_through_edit_response(self):
        self.setup_project()
        meta, headers = self.key(
            scopes=["projects:read", "projects:write"],
            all_projects=False,
            project_ids=["story-one"],
        )
        # Simulate a key issued before the scope dependency was enforced.
        with self.app.state.repository.engine.begin() as conn:
            conn.execute(
                update(personal_keys)
                .where(personal_keys.c.id == meta["id"])
                .values(scopes=json.dumps(["projects:write"]))
            )
        self.assertEqual(
            self.client.get("/v1/projects/story-one", headers=headers).status_code, 404
        )
        self.assertEqual(
            self.edit(
                [
                    {
                        "op": "project.update",
                        "patch": {"title": "Must not expose document"},
                    }
                ],
                headers=headers,
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.put(
                "/v1/projects/story-one",
                json={"project": project(), "expected_version": 1},
                headers=headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/v1/projects/story-one/meta").json()["version"], 1
        )

    def test_key_mint_rechecks_password_rotation_after_request_authentication(self):
        auth = self.app.state.auth
        password, replacement = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        for username in ("superdan", "supervan"):
            auth.set_password(username, password)
        auth.mode = "password"
        self.client.post(
            "/api/auth/login", json={"username": "superdan", "password": password}
        ).raise_for_status()
        original = auth.session
        rotated = []

        def authenticate_then_rotate(token):
            principal = original(token)
            if principal:
                auth.set_password("superdan", replacement)
                rotated.append(True)
            return principal

        with patch.object(auth, "session", side_effect=authenticate_then_rotate):
            response = self.client.post(
                "/v1/api-keys",
                json={
                    "name": "Must not survive rotation",
                    "scopes": ["projects:read"],
                    "all_projects": True,
                },
            )
        self.assertEqual(rotated, [True])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(auth.list_keys("superdan"), [])
        # A genuinely new login may still issue a key under the new version.
        self.client.post(
            "/api/auth/login", json={"username": "superdan", "password": replacement}
        ).raise_for_status()
        _, headers = self.key(scopes=["projects:read"])
        self.assertIsNotNone(auth.bearer(headers["Authorization"][7:]))

    def test_key_mint_rechecks_logout_after_request_authentication(self):
        self.login()
        auth, original = self.app.state.auth, self.app.state.auth.session

        def authenticate_then_logout(token):
            principal = original(token)
            auth.logout(token)
            return principal

        with patch.object(auth, "session", side_effect=authenticate_then_logout):
            response = self.client.post(
                "/v1/api-keys",
                json={
                    "name": "Must not survive logout",
                    "scopes": ["projects:read"],
                    "all_projects": True,
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(auth.list_keys("superdan"), [])

    def test_key_mint_rechecks_disabled_account_after_request_authentication(self):
        auth = self.app.state.auth
        password = secrets.token_urlsafe(18)
        for username in ("superdan", "supervan"):
            auth.set_password(username, password)
        auth.mode = "password"
        self.client.post(
            "/api/auth/login", json={"username": "superdan", "password": password}
        ).raise_for_status()
        original = auth.session

        def authenticate_then_disable(token):
            principal = original(token)
            with auth.engine.begin() as conn:
                conn.execute(
                    update(accounts)
                    .where(
                        accounts.c.tenant == auth.tenant,
                        accounts.c.username == "superdan",
                    )
                    .values(disabled=1)
                )
            return principal

        with patch.object(auth, "session", side_effect=authenticate_then_disable):
            response = self.client.post(
                "/v1/api-keys",
                json={
                    "name": "Must not survive disable",
                    "scopes": ["projects:read"],
                    "all_projects": True,
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(auth.list_keys("superdan"), [])

    def test_future_stories_create_retry_scope_and_browser_visibility(self):
        self.login()
        _, headers = self.key()
        create_headers = {**headers, "Idempotency-Key": "first-story"}
        first = self.client.post(
            "/v1/projects",
            json={"title": "First", "logline": "Story"},
            headers=create_headers,
        )
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            "/v1/projects",
            json={"title": "First", "logline": "Story"},
            headers=create_headers,
        )
        self.assertEqual(first.json(), second.json())
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={"title": "Changed"}, headers=create_headers
            ).status_code,
            409,
        )
        ident = first.json()["id"]
        self.assertEqual(
            self.client.get("/v1/projects").json()["projects"][0]["id"], ident
        )
        self.assertEqual(
            self.client.get(f"/v1/projects/{ident}/meta", headers=headers).json()[
                "version"
            ],
            1,
        )
        third = self.client.post(
            "/v1/projects", json={"title": "Second", "id": "second"}, headers=headers
        )
        self.assertEqual(third.status_code, 201)
        self.assertEqual(
            len(self.client.get("/v1/projects", headers=headers).json()["projects"]), 2
        )
        self.login("supervan")
        self.assertEqual(self.client.get(f"/v1/projects/{ident}").status_code, 404)
        self.assertEqual(self.client.get(f"/v1/projects/{ident}/meta").status_code, 404)
        self.assertEqual(self.client.get("/v1/projects").json()["projects"], [])

    def test_restricted_keys_cannot_create_escape_or_write_with_read_only(self):
        self.setup_project()
        _, headers = self.key(
            scopes=["projects:read"], all_projects=False, project_ids=["story-one"]
        )
        self.client.post(
            "/v1/projects", json={"title": "Second", "id": "second"}
        ).raise_for_status()
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={"title": "Denied"}, headers=headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/v1/projects/second", headers=headers).status_code, 404
        )
        self.assertEqual(
            self.edit(
                [{"op": "project.update", "patch": {"title": "Denied"}}],
                headers=headers,
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.put(
                "/v1/projects/story-one",
                json={"project": project(), "expected_version": 1},
                headers=headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            len(self.client.get("/v1/projects", headers=headers).json()["projects"]), 1
        )

    def test_static_machine_remains_project_scoped_and_cannot_create_keys(self):
        self.setup_project()
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client(
            "legacy", token, "superdan", ["story-one"], ["projects:read", "jobs:write"]
        )
        headers = {"Authorization": "Bearer " + token}
        self.assertEqual(
            self.client.get("/v1/projects/story-one", headers=headers).status_code, 200
        )
        self.assertEqual(
            self.client.get("/v1/api-keys", headers=headers).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={"title": "No"}, headers=headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.edit(
                [{"op": "project.update", "patch": {"title": "No"}}], headers=headers
            ).status_code,
            404,
        )

    def test_jobs_list_future_scope_and_asset_attach_needs_read_scope(self):
        self.setup_project()
        _, full = self.key()
        plan = self.make_plan()
        self.client.post(
            "/v1/jobs",
            json={"plan_id": plan["plan_id"]},
            headers={**full, "Idempotency-Key": "one-job"},
        ).raise_for_status()
        self.assertEqual(
            len(self.client.get("/v1/jobs", headers=full).json()["jobs"]), 1
        )
        _, write_only = self.key(scopes=["projects:read", "projects:write"])
        denied = self.edit(
            [{"op": "asset.attach", "asset_id": "does-not-exist"}], headers=write_only
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(
            self.client.get("/v1/jobs", headers=write_only).json()["jobs"], []
        )

    def test_atomic_guided_full_creation_data_merge_and_optimistic_lock(self):
        self.login()
        _, headers = self.key()
        self.client.post(
            "/v1/projects", json={"id": "story-one", "title": "Empty"}, headers=headers
        ).raise_for_status()
        actions = [
            {
                "op": "entity.create",
                "entity": {"id": "chapter", "type": "chapter", "title": "Chapter"},
            },
            {
                "op": "entity.create",
                "entity": {
                    "id": "scene",
                    "type": "scene",
                    "parentId": "chapter",
                    "data": {"script": "Hello"},
                },
            },
            {
                "op": "entity.create",
                "entity": {"id": "shot", "type": "shot", "parentId": "scene"},
            },
            {
                "op": "entity.create",
                "entity": {"id": "actor", "type": "character", "title": "Actor"},
            },
            {
                "op": "link.create",
                "link": {
                    "id": "identity",
                    "source": "actor",
                    "target": "shot",
                    "role": "identity",
                },
            },
            {
                "op": "entity.update",
                "entity_id": "shot",
                "patch": {"data": {"prompt": "Walk", "h3": {"steps": 50}}},
            },
            {
                "op": "journey.update",
                "patch": {"brief": {"aspect": "16:9"}, "sound": {"mode": "silent"}},
            },
            {
                "op": "layout.update",
                "patch": {"positions": {"shot": {"x": 100, "y": 200}}},
            },
        ]
        result = self.edit(actions, headers=headers, key="story-edits")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["version"], 2)
        self.assertEqual(
            result.json(), self.edit(actions, headers=headers, key="story-edits").json()
        )
        shot = next(
            e for e in result.json()["project"]["entities"] if e["id"] == "shot"
        )
        self.assertEqual(shot["data"]["seconds"], 5)
        self.assertEqual(shot["version"], 2)
        self.assertEqual(self.edit(actions, headers=headers).status_code, 409)
        invalid = self.edit(
            [
                {"op": "project.update", "patch": {"title": "Must rollback"}},
                {
                    "op": "entity.create",
                    "entity": {"type": "scene", "parentId": "missing"},
                },
            ],
            version=2,
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 422)
        latest = self.client.get("/v1/projects/story-one").json()
        from studio_platform.quality_views import project as creator_project

        self.assertEqual(creator_project(latest), {**result.json(), "created_by": {}})
        self.assertEqual(
            self.client.get(
                "/v1/projects/story-one/entities?type=shot", headers=headers
            ).json()["entities"],
            [shot],
        )

    def test_discovered_schema_examples_execute_without_generation(self):
        self.login()
        _, headers = self.key()
        schema = self.client.get("/v1/guided-schema", headers=headers).json()
        self.assertEqual(
            schema["version"], 1
        )  # Preserve the original additive contract.
        self.assertEqual(set(schema["actions"]), set(schema["operation_schemas"]))
        self.assertEqual(len(schema["examples"]), 3)
        self.assertEqual(
            schema["operation_schemas"]["shot.trim"]["required"],
            ["op", "end", "shot_id", "start"],
        )
        self.assertEqual(
            schema["operation_schemas"]["sound.generated"]["properties"]["gain"][
                "maximum"
            ],
            1,
        )
        variables = {"portrait_file": "portrait-fixture", "voice_file": "voice-fixture"}
        wav = io.BytesIO()
        with wave.open(wav, "wb") as source:
            source.setnchannels(1)
            source.setsampwidth(2)
            source.setframerate(24000)
            source.writeframes(b"\x00\x00" * 48000)
        files = {"portrait-fixture": png(), "voice-fixture": wav.getvalue()}

        def variable(name):
            value = variables
            for field in name.split("."):
                value = value[field]
            return value

        def substitute(value):
            if isinstance(value, dict):
                return {k: substitute(v) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v) for v in value]
            if isinstance(value, str):
                exact = re.fullmatch(r"\$\{([^}]+)\}", value)
                if exact:
                    return variable(exact[1])
                return re.sub(
                    r"\$\{([^}]+)\}", lambda match: str(variable(match[1])), value
                )
            return value

        # Execute the published bodies, not separately maintained sample copies.
        for example in schema["examples"]:
            for template in example["steps"]:
                # Post-response inspection hints can reference this step's receipt.
                step = substitute({k: v for k, v in template.items() if k != "require"})
                options = {"headers": {**headers, **step.get("headers", {})}}
                if "json" in step:
                    options["json"] = step["json"]
                if "multipart" in step:
                    upload = step["multipart"]
                    file = upload["file"]
                    options.update(
                        data=upload["fields"],
                        files={
                            file["field"]: (
                                file["filename"],
                                files[file["local_path"]],
                                file["content_type"],
                            )
                        },
                    )
                result = self.client.request(step["method"], step["path"], **options)
                self.assertIn(result.status_code, (200, 201), result.text)
                if "save_response_as" in step:
                    variables[step["save_response_as"]] = result.json()
                    if "multipart" in step:
                        self.assertEqual(result.json()["status"], "ready")
        self.assertIn("00:00:00,000 --> 00:00:02,000", result.text)
        self.assertIn("这封信，来自明天。", result.text)
        ident = variables["story"]["id"]
        final = self.client.get(f"/v1/projects/{ident}", headers=headers).json()[
            "project"
        ]
        self.assertEqual(
            {e["type"] for e in final["entities"]},
            {"chapter", "scene", "shot", "image", "audio"},
        )
        shot = next(e for e in final["entities"] if e["id"] == "shot-one")
        self.assertEqual(shot["data"]["selectedAssetId"], "portrait-reference")
        self.assertEqual(final["links"][0]["role"], "firstFrame")
        track = final["journey"]["soundTracks"]["chapter-one"][0]
        self.assertEqual(
            track["fileId"], "cloud_asset_" + variables["audio_upload"]["asset_id"]
        )
        self.assertEqual(
            self.client.get("/v1/jobs", headers=headers).json()["jobs"], []
        )

    def test_action_shape_errors_explain_recovery_without_committing_batch(self):
        before = self.setup_project()
        _, headers = self.key()
        # One otherwise valid write must not commit before a malformed action.
        malformed = [
            ({"op": "shot.trim", "shot_id": "shot-one", "start": 0}, "end"),
            ({"op": "asset.attach"}, "asset_id"),
            ({"op": "image.generate"}, "operation_schemas"),
            (
                {"op": "artifact.adopt", "artifact_id": "missing", "role": "reference"},
                "允许字段",
            ),
        ]
        for action, message in malformed:
            response = self.edit(
                [{"op": "project.update", "patch": {"title": "Do not commit"}}, action],
                headers=headers,
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn(message, response.json()["detail"])
            self.assertIn("/v1/guided-schema", response.json()["detail"])
            self.assertEqual(
                self.client.get("/v1/projects/story-one", headers=headers).json(),
                {**before, "created_by": {}},
            )

    def test_concurrent_actions_commit_once_and_replay_same_receipt(self):
        self.setup_project()
        _, headers = self.key()
        action = [{"op": "project.update", "patch": {"title": "One edit"}}]
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(
                    lambda _: self.edit(action, headers=headers, key="concurrent-edit"),
                    range(4),
                )
            )
        self.assertEqual([r.status_code for r in responses], [200] * 4)
        self.assertEqual(len({r.json()["version"] for r in responses}), 1)
        self.assertEqual(
            self.client.get("/v1/projects/story-one/meta").json()["version"], 2
        )

    def test_upload_attach_select_and_cross_project_rejection(self):
        self.setup_project()
        _, headers = self.key()
        uploaded = self.client.post(
            "/v1/assets",
            data={"client_project_id": "story-one"},
            files={"file": ("portrait.png", png(), "image/png")},
            headers=headers,
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        asset = uploaded.json()["asset_id"]
        action = {
            "op": "asset.attach",
            "asset_id": asset,
            "entity_id": "portrait",
            "shot_id": "shot-one",
            "role": "firstFrame",
            "select": True,
        }
        attached = self.edit([action], headers=headers, key="attach-portrait")
        self.assertEqual(attached.status_code, 200, attached.text)
        value = self.client.get("/v1/projects/story-one").json()["project"]
        picture = next(e for e in value["entities"] if e["id"] == "portrait")
        self.assertEqual(picture["data"]["cloudAssetId"], asset)
        self.assertEqual(value["entities"][2]["data"]["selectedAssetId"], "portrait")
        self.assertEqual(value["links"][0]["role"], "firstFrame")
        self.client.post(
            "/v1/projects", json={"project": project("other")}
        ).raise_for_status()
        self.assertEqual(
            self.edit([action], headers=headers, ident="other").status_code, 404
        )
        self.login("supervan")
        self.client.post("/v1/projects", json={"project": project()}).raise_for_status()
        self.assertEqual(self.edit([action]).status_code, 404)

    def test_entity_delete_requires_explicit_cascade_and_preserves_files(self):
        self.setup_project()
        self.assertEqual(
            self.edit(
                [{"op": "entity.delete", "entity_id": "chapter-one"}]
            ).status_code,
            409,
        )
        response = self.edit(
            [{"op": "entity.delete", "entity_id": "chapter-one", "cascade": True}]
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["project"]["entities"], [])

    def test_captions_confirmation_export_and_changed_timeline_conflict(self):
        self.setup_project()
        _, headers = self.key()
        response = self.edit(
            [
                {
                    "op": "captions.set",
                    "chapter_id": "chapter-one",
                    "cues": [
                        {"id": "cue", "start": 0, "end": 2, "text": "Hello, traveller"}
                    ],
                },
                {
                    "op": "sound.set",
                    "chapter_id": "chapter-one",
                    "tracks": [],
                    "mode": "silent",
                },
                {
                    "op": "captions.confirm",
                    "chapter_id": "chapter-one",
                    "reviewed": True,
                },
            ],
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        srt = self.client.get(
            "/v1/projects/story-one/chapters/chapter-one/subtitles.srt", headers=headers
        )
        self.assertEqual(srt.status_code, 200, srt.text)
        self.assertIn("00:00:00,000 --> 00:00:02,000", srt.text)
        self.assertIn("Hello, traveller", srt.text)
        changed = self.edit(
            [
                {
                    "op": "entity.update",
                    "entity_id": "shot-one",
                    "patch": {"data": {"seconds": 4}},
                }
            ],
            version=2,
            headers=headers,
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/v1/projects/story-one/chapters/chapter-one/subtitles.srt",
                headers=headers,
            ).status_code,
            409,
        )

    def test_json_csv_exports_and_formula_escaping(self):
        self.setup_project()
        self.edit(
            [
                {
                    "op": "entity.update",
                    "entity_id": "shot-one",
                    "patch": {"data": {"prompt": "=DANGEROUS()"}},
                }
            ]
        ).raise_for_status()
        export = self.client.get("/v1/projects/story-one/export?format=json")
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export.json()["schemaVersion"], 4)
        csv = self.client.get("/v1/projects/story-one/export?format=csv")
        self.assertIn("'=DANGEROUS()", csv.text)
        self.assertIn("attachment", csv.headers["content-disposition"])
        self.login("supervan")
        self.assertEqual(
            self.client.get("/v1/projects/story-one/export").status_code, 404
        )

    def test_skill_download_contains_only_fixed_public_sources(self):
        self.assertEqual(self.client.get("/v1/agent-skill.zip").status_code, 401)
        self.login()
        _, headers = self.key(scopes=["projects:read"])
        result = self.client.get("/v1/agent-skill.zip", headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertIn(
            'filename="inkseq-agent-skill.zip"', result.headers["content-disposition"]
        )
        with zipfile.ZipFile(io.BytesIO(result.content)) as bundle:
            from studio_platform.agent_discovery import SKILL_FILES

            self.assertEqual(
                set(bundle.namelist()),
                {"inkseq/README.md", *("inkseq/" + name for name in SKILL_FILES)},
            )
            self.assertEqual(
                bundle.read("inkseq/scripts/inkseq.py"),
                (
                    Path(__file__).resolve().parents[1]
                    / "skills/inkseq/scripts/inkseq.py"
                ).read_bytes(),
            )
        _, restricted = self.key(scopes=["jobs:read"])
        self.assertEqual(
            self.client.get("/v1/agent-skill.zip", headers=restricted).status_code, 403
        )

    def test_artifact_adoption_matching_audio_and_download_authorization(self):
        self.setup_project()
        plan = self.make_plan()
        task = self.client.post(
            "/v1/jobs",
            json={"plan_id": plan["plan_id"]},
            headers={"Idempotency-Key": "fixture-job"},
        ).json()
        # Trusted fixture represents verified worker output without renting a GPU.
        with self.app.state.repository.engine.begin() as conn:
            conn.execute(
                update(jobs).where(jobs.c.id == task["id"]).values(status="succeeded")
            )
            conn.execute(
                insert(attempts).values(
                    id="fixture-attempt",
                    job_id=task["id"],
                    number=1,
                    status="succeeded",
                    fence=1,
                    worker_id="fixture",
                    created_at=1,
                    updated_at=1,
                )
            )
            for kind, mime in (("video", "video/mp4"), ("audio", "audio/flac")):
                conn.execute(
                    insert(artifacts).values(
                        id="artifact-" + kind,
                        job_id=task["id"],
                        attempt_id="fixture-attempt",
                        created_at=1,
                        metadata={
                            "kind": kind,
                            "mime": mime,
                            "object_key": "private-fixture",
                            "size_bytes": 100,
                            "sha256": "0" * 64,
                            "duration": 5,
                        },
                    )
                )
        _, headers = self.key()
        result = self.edit(
            [
                {
                    "op": "artifact.adopt",
                    "artifact_id": "artifact-video",
                    "shot_id": "shot-one",
                    "select": True,
                },
                {"op": "artifact.adopt", "artifact_id": "artifact-audio"},
                {
                    "op": "sound.set",
                    "chapter_id": "chapter-one",
                    "mode": "mixed",
                    "tracks": [],
                },
                {"op": "sound.generated", "shot_id": "shot-one"},
                {"op": "shot.trim", "shot_id": "shot-one", "start": 0, "end": 5},
            ],
            headers=headers,
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn("private-fixture", result.text)
        doc = self.client.get("/v1/projects/story-one").json()["project"]
        self.assertEqual(
            doc["entities"][2]["data"]["selectedAssetId"], "result-artifact-video"
        )
        track = doc["journey"]["soundTracks"]["chapter-one"][0]
        self.assertEqual(track["generatedFrom"]["audioArtifactId"], "artifact-audio")
        self.assertEqual(track["generatedFrom"]["jobId"], task["id"])
        self.login("supervan")
        self.client.post("/v1/projects", json={"project": project()}).raise_for_status()
        self.assertEqual(
            self.edit(
                [{"op": "artifact.adopt", "artifact_id": "artifact-video"}]
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
