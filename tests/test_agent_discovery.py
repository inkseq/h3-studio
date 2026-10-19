"""Anonymous discovery, authenticated contract and distributable skill invariants."""

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from studio_platform import agent_discovery
import test_platform_api as fixtures


class AgentDiscoveryTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login

    def test_public_docs_readable_without_frontend_or_credentials(self):
        for path in sorted(agent_discovery.PUBLIC_PATHS):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                head = self.client.head(path)
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
        html = self.client.get("/for-agents").text
        self.assertNotIn("<script", html)
        for link in (
            "/for-agents/guide.json",
            "/for-agents/guide.md",
            "/for-agents/guide.md?download=true",
            "/for-agents/SKILL.md",
            "/for-agents/skill.zip",
            "/llms.txt",
        ):
            self.assertIn('href="' + link + '"', html)
        self.assertIn(
            'rel="service-doc"', self.client.get("/for-agents").headers["Link"]
        )

    def test_only_exact_safe_methods_are_public_and_api_stays_private(self):
        for path in sorted(agent_discovery.PUBLIC_PATHS):
            self.assertEqual(self.client.post(path, json={}).status_code, 401)
        for path in (
            "/for-agents/private",
            "/for-agents/key",
            "/v1/projects",
            "/v1/capabilities",
            "/v1/generation-availability",
            "/v1/agent-guide",
            "/v1/guided-schema",
            "/v1/agent-skill.zip",
            "/openapi.json",
            "/v1/projects/nonexistent/activity",
            "/for-agents/reference/private.md",
            "/for-agents/reference/api.md/extra",
            "/for-agents/reference/%2e%2e/scripts/connect.py",
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(
            self.client.get(
                "/for-agents", headers={"Host": "attacker.example"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/for-agents", headers={"Authorization": "Bearer invalid"}
            ).status_code,
            401,
        )

    def test_discovery_only_links_authenticated_availability_without_live_observations(
        self,
    ):
        guide = self.client.get("/for-agents/guide.json").json()
        self.assertEqual(guide["name"], "Inkseq / 映序")
        self.assertEqual(
            guide["connection"]["origin_migration"]["target"], "https://inkseq.com"
        )
        self.assertEqual(
            guide["authentication"]["credential_environment"], "INKSEQ_API_KEY"
        )
        document = self.client.get("/for-agents/guide.md").text
        self.assertIn("--base-url https://inkseq.com", document)
        self.assertIn("scripts/inkseq.py", document)
        self.assertNotIn("SIXNINE_API_KEY", document)
        self.assertNotIn("sixnine.art", document)
        recovery = self.client.get(guide["connection"]["origin_migration"]["reference"])
        self.assertEqual(recovery.status_code, 200)
        self.assertIn(
            "migrate-origin --server https://www.sixnine.art --to-server https://inkseq.com",
            recovery.text,
        )
        self.assertIn("Keep the original encrypted credential", recovery.text)
        contract = guide["generation_availability"]
        self.assertEqual(
            contract["path"],
            guide["authenticated_resources"]["generation_availability"],
        )
        self.assertEqual(contract["method"], "GET")
        self.assertTrue(contract["authenticated"])
        self.assertEqual(contract["required_scope_any"], ["jobs:read", "jobs:write"])
        for private_observation in ("profiles", "observed_at", "expires_at"):
            self.assertNotIn(private_observation, contract)
        self.login()
        authenticated = self.client.get("/v1/agent-guide")
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(authenticated.json()["generation_availability"], contract)

    def test_public_content_does_not_change_after_private_edit(self):
        paths = (
            "/for-agents",
            "/llms.txt",
            "/for-agents/guide.json",
            "/for-agents/guide.md",
            "/for-agents/guide.md?download=true",
            "/for-agents/SKILL.md",
            "/for-agents/reference/api.md",
            "/for-agents/reference/recovery.md",
        )
        anonymous = {path: self.client.get(path).content for path in paths}
        self.login()
        self.client.post(
            "/v1/projects",
            json={
                "title": "PRIVATE PROJECT NEVER ADVERTISE",
                "logline": "PRIVATE CONTENT NEVER ADVERTISE",
            },
        ).raise_for_status()
        for path in paths:
            self.assertEqual(self.client.get(path).content, anonymous[path])
            self.assertNotIn(b"PRIVATE", anonymous[path])
        self.client.cookies.clear()
        for path in paths:
            self.assertEqual(self.client.get(path).content, anonymous[path])

    def test_resume_contract_and_burn_limits_match_existing_authenticated_routes(self):
        from studio_platform.caption_server import validate_caption_text

        public = self.client.get("/for-agents/guide.json").json()
        resume = public["quick_chat"]["job_changes"]["resume_admission"]
        routes = {
            (method, route.path)
            for route in self.app.routes
            for method in getattr(route, "methods", ())
        }
        for method, path in (
            (resume["method"], resume["path"]),
            ("POST", resume["preflight"]),
            ("GET", resume["observe"]),
            ("GET", resume["revision"]),
            ("GET", resume["observe_preflight"]),
        ):
            self.assertIn((method, path), routes)
        self.assertEqual(
            set(resume["preflight_body"]),
            {"revision_hash", "capabilities_version", "item_ids"},
        )
        self.assertEqual(
            set(resume["body"]), {"item_ids", "fresh_preflight_id", "confirmed"}
        )
        self.assertIs(resume["body"]["confirmed"], True)
        self.assertEqual(
            resume["preflight_body"]["item_ids"], resume["body"]["item_ids"]
        )
        burn = public["story_film"]["subtitle_burn"]
        line = "x" * burn["max_characters_per_line"]
        valid = "\n".join([line] * burn["max_lines_per_cue"])
        self.assertEqual(validate_caption_text(valid), valid)
        for invalid in (valid + "x", valid + "\nx"):
            with self.assertRaises(ValueError):
                validate_caption_text(invalid)
        self.login()
        authenticated = self.client.get("/v1/agent-guide").json()
        schema = self.client.get("/v1/quick-chat/schema").json()
        self.assertEqual(authenticated["examples_url"], "/for-agents/guide.json")
        self.assertEqual(schema["agent_contract"], public["quick_chat"])

    def test_zip_contains_only_reviewed_files_and_matches_public_skill(self):
        response = self.client.get("/for-agents/skill.zip")
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "inkseq/SKILL.md",
                    "inkseq/README.md",
                    "inkseq/scripts/inkseq.py",
                    "inkseq/scripts/sixnine.py",
                    "inkseq/scripts/connect.py",
                    "inkseq/reference/api.md",
                    "inkseq/reference/recovery.md",
                },
            )
            self.assertEqual(
                archive.read("inkseq/SKILL.md"),
                self.client.get("/for-agents/SKILL.md").content,
            )
            self.assertEqual(
                archive.read("inkseq/README.md"),
                self.client.get("/for-agents/guide.md").content,
            )
            for name in agent_discovery.SKILL_FILES:
                self.assertEqual(
                    archive.read("inkseq/" + name),
                    (agent_discovery.SKILL_ROOT / name).read_bytes(),
                )
            bundled_helper = archive.read("inkseq/scripts/connect.py")
            bundled_guide = archive.read("inkseq/README.md").decode("utf-8")
        manifest = self.client.get("/for-agents/connect-manifest.json").json()
        helper = self.client.get(manifest["helper"]).content
        self.assertEqual(manifest["sha256"], hashlib.sha256(helper).hexdigest())
        self.assertEqual(bundled_helper, helper)
        self.assertEqual(manifest["sha256"], hashlib.sha256(bundled_helper).hexdigest())
        self.assertIn("native macOS user Keychain", bundled_guide)
        self.assertIn("Fake OS tests do not prove native store access", bundled_guide)
        guide = self.client.get("/for-agents/guide.json").json()
        self.assertFalse(manifest["raw_api_key_response"])
        self.assertTrue(guide["connection"]["profile"]["owner_only"])

    def test_reference_http_zip_and_source_bytes_agree(self):
        with zipfile.ZipFile(
            io.BytesIO(self.client.get("/for-agents/skill.zip").content)
        ) as archive:
            for name in ("api.md", "recovery.md"):
                path = "reference/" + name
                response = self.client.get("/for-agents/" + path)
                self.assertEqual(
                    response.headers["content-type"], "text/markdown; charset=utf-8"
                )
                self.assertEqual(
                    response.content, (agent_discovery.SKILL_ROOT / path).read_bytes()
                )
                self.assertEqual(response.content, archive.read("inkseq/" + path))
        self.assertLessEqual(
            len(self.client.get("/for-agents/SKILL.md").content), 10 * 1024
        )

    def test_storage_manifest_is_additive_and_matches_full_public_connection_contract(
        self,
    ):
        manifest = self.client.get("/for-agents/connect-manifest.json").json()
        original_fields = {
            name: manifest[name]
            for name in (
                "version",
                "helper",
                "protocol",
                "missing_storage_behavior",
                "manual_pat",
                "exchange",
                "raw_api_key_response",
                "discovery_is_authorization",
            )
        }
        self.assertEqual(
            original_fields,
            {
                "version": 1,
                "helper": "/for-agents/connect.py",
                "protocol": "client-held-pat-v1",
                "missing_storage_behavior": "stop_before_exchange",
                "manual_pat": "advanced_fallback",
                "exchange": "/v1/agent-connect/exchange",
                "raw_api_key_response": False,
                "discovery_is_authorization": False,
            },
        )
        self.assertEqual(
            manifest["credential_storage"],
            ["windows-user-dpapi", "linux-secret-service", "macos-user-keychain"],
        )
        connection = self.client.get("/for-agents/guide.json").json()["connection"]
        for field in (
            "credential_storage",
            "credential_storage_requirements",
            "verification_checklist",
            "verification_limits",
        ):
            self.assertEqual(connection[field], manifest[field])
        self.assertEqual(
            set(manifest["credential_storage_requirements"]),
            set(manifest["credential_storage"]),
        )
        macos = manifest["credential_storage_requirements"]["macos-user-keychain"]
        for requirement in (
            "existing default file-based",
            "already unlocked",
            "Authentication UI is suppressed",
            "same Keychain",
            "fails closed",
            "never collects a Keychain password",
        ):
            self.assertIn(requirement, macos)
        self.assertTrue(manifest["verification_checklist"])
        self.assertIn("separate evidence", manifest["verification_limits"])
        self.assertEqual(connection["profile"]["id"], "creator-full")
        self.assertEqual(connection["profile"]["version"], 1)
        self.assertEqual(connection["profile"]["key_lifetime_days"], 90)
        self.assertEqual(
            connection["profile"]["scopes"],
            [
                "assets:read",
                "assets:write",
                "assistant:run",
                "jobs:read",
                "jobs:write",
                "projects:create",
                "projects:read",
                "projects:write",
            ],
        )
        self.assertEqual(connection["code_ttl_seconds"], 600)
        self.assertEqual(connection["recovery_ttl_seconds"], 300)
        self.assertNotIn("observed_at", manifest)
        self.assertNotIn("profiles", manifest)

    def test_markdown_read_and_download_include_current_contract_without_login(self):
        guide = self.client.get("/for-agents/guide.json").json()
        read = self.client.get(guide["public_resources"]["guide_markdown"])
        download = self.client.get(guide["public_resources"]["guide_download"])
        self.assertEqual(read.status_code, 200)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(read.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertNotIn("content-disposition", read.headers)
        self.assertEqual(
            download.headers["content-disposition"],
            'attachment; filename="inkseq-agent-guide.md"',
        )
        self.assertEqual(download.content, read.content)
        embedded = read.text.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        self.assertEqual(json.loads(embedded), guide)
        self.assertIn("same user-supplied origin", read.text)
        self.assertIn("A tool-specific reading failure is not evidence", read.text)
        self.assertIn("account/business API calls do require authentication", read.text)
        self.assertIn("does not install software", read.text)
        self.assertIn("native macOS user Keychain", read.text)
        self.assertIn("suppress authentication UI", read.text)
        self.assertIn("trusted system UI", read.text)
        self.assertIn("Fake OS tests do not prove native store access", read.text)
        self.assertNotIn("macOS storage is not implemented", read.text)
        self.assertNotIn(
            "macOS storage is not implemented",
            self.client.get("/for-agents/SKILL.md").text,
        )
        self.assertIn(
            "/for-agents/guide.md?download=true", self.client.get("/llms.txt").text
        )
        head = self.client.head(guide["public_resources"]["guide_download"])
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(
            head.headers["content-disposition"], download.headers["content-disposition"]
        )
        self.assertEqual(
            head.headers["content-length"], download.headers["content-length"]
        )

    def test_missing_or_oversize_skill_fails_closed_without_local_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(agent_discovery, "SKILL_ROOT", root):
                for path in (
                    "/for-agents/skill.zip",
                    "/for-agents/SKILL.md",
                    "/for-agents/reference/api.md",
                    "/for-agents/reference/recovery.md",
                ):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn(folder, response.text)
                (root / "SKILL.md").write_bytes(
                    b"x" * (agent_discovery.MAX_SKILL_BYTES + 1)
                )
                self.assertEqual(
                    self.client.get("/for-agents/SKILL.md").status_code, 503
                )

    def test_linked_file_rejected_without_read(self):
        # Do not require Windows symlink privileges to test the rejection.
        with (
            patch.object(Path, "is_symlink", return_value=True),
            patch.object(Path, "open") as opened,
        ):
            for path in (
                "/for-agents/SKILL.md",
                "/for-agents/reference/api.md",
                "/for-agents/reference/recovery.md",
            ):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 503)
        opened.assert_not_called()

    def test_linked_reference_directory_is_rejected_before_reading(self):
        original = Path.is_symlink

        def linked(path):
            return path == agent_discovery.SKILL_ROOT / 'reference' or original(path)

        with (
            patch.object(Path, 'is_symlink', linked),
            patch.object(Path, 'open') as opened,
        ):
            for path in (
                '/for-agents/reference/api.md',
                '/for-agents/reference/recovery.md',
            ):
                self.assertEqual(self.client.get(path).status_code, 503)
        opened.assert_not_called()

    def test_manifest_examples_produce_navigable_story_with_scoped_agent(self):
        guide = self.client.get("/for-agents/guide.json").json()
        self.assertFalse(guide["discovery_is_authorization"])
        self.assertEqual(
            guide["web_links"]["entity"], "/?project={project_id}&entity={entity_id}"
        )
        self.login()
        key_response = self.client.post(
            "/v1/api-keys",
            json={
                "name": "Documented onboarding fixture",
                "scopes": ["projects:read", "projects:create", "projects:write"],
                "all_projects": True,
                "project_ids": [],
                "expires_in_days": 1,
            },
        )
        key_response.raise_for_status()
        headers = {"Authorization": "Bearer " + key_response.json()["api_key"]}
        self.client.cookies.clear()
        create = guide["examples"]["create_story"]
        created = self.client.request(
            create["method"],
            create["path"],
            json=create["body"],
            headers={**headers, **create["headers"]},
        )
        self.assertEqual(created.status_code, 201, created.text)
        row = created.json()
        edit = guide["examples"]["create_chapter_scene_shot"]
        body = copy.deepcopy(edit["body"])
        body["expected_version"] = row["version"]
        result = self.client.request(
            edit["method"],
            edit["path"].format(project_id=row["id"]),
            json=body,
            headers={**headers, **edit["headers"]},
        )
        self.assertEqual(result.status_code, 200, result.text)
        saved = self.client.get("/v1/projects/" + row["id"], headers=headers).json()
        entities = {item["id"]: item for item in saved["project"]["entities"]}
        self.assertEqual(entities["shot-one"]["parentId"], "scene-one")
        self.assertEqual(entities["scene-one"]["parentId"], "chapter-one")
        self.assertEqual(saved["version"], row["version"] + 1)
        # The exact retry resolves the first receipt and cannot duplicate entities.
        retry = self.client.request(
            edit["method"],
            edit["path"].format(project_id=row["id"]),
            json=body,
            headers={**headers, **edit["headers"]},
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["version"], saved["version"])
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/projects/" + row["id"]).status_code, 404)

    def test_quick_examples_save_one_web_draft_preflight_and_never_submit_implicitly(
        self,
    ):
        guide = self.client.get("/for-agents/guide.json").json()
        self.assertEqual(guide["version"], 2)
        self.assertEqual(
            guide["web_links"]["quick"],
            "/freestyle?project={project_id}&entity={shot_id}",
        )
        self.login()
        issued = self.client.post(
            "/v1/api-keys",
            json={
                "name": "Quick agent fixture",
                "scopes": [
                    "projects:read",
                    "projects:create",
                    "projects:write",
                    "assets:read",
                    "assets:write",
                    "jobs:read",
                    "jobs:write",
                ],
                "all_projects": True,
                "project_ids": [],
                "expires_in_days": 1,
            },
        )
        issued.raise_for_status()
        headers = {"Authorization": "Bearer " + issued.json()["api_key"]}
        self.client.cookies.clear()
        example = guide["examples"]["create_quick_project"]
        created = self.client.request(
            example["method"],
            example["path"],
            json=example["body"],
            headers={**headers, **example["headers"]},
        )
        self.assertEqual(created.status_code, 201, created.text)
        row = created.json()
        shot_id = row["project"]["journey"]["reviewShotId"]
        self.assertEqual(row["project"]["journey"]["workspace"], "freestyle")
        self.assertEqual(
            len([e for e in row["project"]["entities"] if e["type"] == "shot"]), 1
        )
        retry = self.client.request(
            example["method"],
            example["path"],
            json=example["body"],
            headers={**headers, **example["headers"]},
        )
        self.assertEqual(retry.json()["id"], row["id"])
        example = guide["examples"]["configure_quick_text"]
        body = copy.deepcopy(example["body"])
        body["expected_version"] = row["version"]
        body["actions"][0]["shot_id"] = shot_id
        response = self.client.request(
            example["method"],
            example["path"].format(project_id=row["id"]),
            json=body,
            headers={**headers, **example["headers"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        current = response.json()
        same = self.client.request(
            example["method"],
            example["path"].format(project_id=row["id"]),
            json=body,
            headers={**headers, **example["headers"]},
        )
        self.assertEqual(same.json()["version"], current["version"])
        shot = next(e for e in current["project"]["entities"] if e["id"] == shot_id)
        self.assertEqual(shot["data"]["prompt"], body["actions"][0]["prompt"])
        self.assertEqual(shot["data"]["h3"]["controls"]["seed"], "42")
        example = guide["examples"]["read_quick_draft"]
        draft = self.client.get(
            example["path"].format(project_id=row["id"], shot_id=shot_id),
            headers=headers,
        )
        self.assertEqual(draft.status_code, 200, draft.text)
        draft = draft.json()
        self.assertEqual(draft["project_version"], current["version"])
        self.assertEqual(draft["draft"]["prompt"], shot["data"]["prompt"])
        self.assertEqual(
            draft["web_url"],
            guide["web_links"]["quick"].format(project_id=row["id"], shot_id=shot_id),
        )
        example = guide["examples"]["plan_quick_draft"]
        plan = self.client.post(
            example["path"].format(project_id=row["id"], shot_id=shot_id),
            json={"expected_version": draft["project_version"]},
            headers=headers,
        )
        self.assertEqual(plan.status_code, 201, plan.text)
        self.assertEqual(plan.json()["status"], "blocked")
        listed = self.client.get(
            "/v1/jobs", params={"client_project_id": row["id"]}, headers=headers
        ).json()
        self.assertEqual(listed["jobs"], [])
        self.login("supervan")
        self.assertEqual(
            self.client.get(
                guide["authenticated_resources"]["generation_draft"].format(
                    project_id=row["id"], shot_id=shot_id
                )
            ).status_code,
            404,
        )

    def test_authenticated_guide_reuses_public_quick_contract_without_public_private_data(
        self,
    ):
        public = self.client.get("/for-agents/guide.json").json()
        self.login()
        private = self.client.get("/v1/agent-guide").json()
        self.assertEqual(private["quick_creation"], public["quick_creation"])
        self.assertIn(
            "shot.configure_generation",
            self.client.get("/v1/guided-schema").json()["operation_schemas"],
        )
        self.assertIn("workspace=freestyle", self.client.get("/llms.txt").text)
        self.assertIn("快速草稿", self.client.get("/for-agents").text)


if __name__ == "__main__":
    unittest.main()
