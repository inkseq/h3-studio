"""Offline single-shot contract tests; synthetic receipts, no GPU/provider calls."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image

from sqlalchemy import insert, select, func

from studio_platform.assets import asset_table
from studio_platform.repository import jobs, plans
from studio_platform.generation_draft import plan_body, read_draft
from studio_platform.capabilities import capabilities
import test_platform_guided as guided_fixtures
import test_platform_api as fixtures


class GenerationDraftTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login
    setup_project = fixtures.ApiTests.setup_project
    key = guided_fixtures.GuidedTests.key
    edit = guided_fixtures.GuidedTests.edit

    def source(self, ident, kind, *, owner="superdan", project="story-one", status="ready", duration=10):
        metadata = {"kind": kind, "model_ready": True, "width": 512, "height": 512,
            "duration": 56/24, "source_duration": duration, "fps": 24, "frame_count": 56,
            "sample_rate": 32000, "channels": 1, "has_audio": kind != "image"}
        value = {"id": ident, "asset_id": ident, "project_id": project, "status": status, "kind": kind,
            "file_name": ident + {"image": ".png", "video": ".mp4", "audio": ".wav"}[kind],
            "mime": {"image": "image/png", "video": "video/mp4", "audio": "audio/wav"}[kind],
            "metadata": metadata, "model": {"key": "synthetic/model", "sha256": "0"*64, "size_bytes": 1},
            "original": {"key": "synthetic/original", "sha256": "0"*64, "size_bytes": 1}}
        with self.app.state.repository.transaction() as conn:
            conn.execute(insert(asset_table).values(id=ident, tenant=self.settings.tenant_id, owner=owner,
                project_id=project, status=status, created=1, record=json.dumps(value)))
        return value

    def configure(self, **fields):
        version = self.client.get("/v1/projects/story-one").json()["version"]
        return self.edit([{"op": "shot.configure_generation", "shot_id": "shot-one", **fields}], version=version)

    def draft(self):
        return self.client.get("/v1/projects/story-one/shots/shot-one/generation-draft")

    def preflight(self, **fields):
        version = self.client.get("/v1/projects/story-one").json()["version"]
        return self.client.post("/v1/projects/story-one/shots/shot-one/generation-plans",
            json={"expected_version": version, **fields})

    def test_quick_template_retry_deterministic_ids_and_owner_isolation(self):
        self.login()
        _, headers = self.key()
        headers["Idempotency-Key"] = "quick-create"
        body = {"title": "Quick", "workspace": "freestyle"}
        first = self.client.post("/v1/projects", json=body, headers=headers)
        repeat = self.client.post("/v1/projects", json=body, headers=headers)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json(), repeat.json())
        record = first.json()
        shot_id = record["project"]["journey"]["reviewShotId"]
        self.assertEqual(len(record["project"]["entities"]), 3)
        draft = self.client.get(f'/v1/projects/{record["id"]}/shots/{shot_id}/generation-draft').json()
        self.assertEqual(draft["draft"]["prompt"], "")
        self.assertTrue(draft["web_url"].startswith("/freestyle?project="))
        self.assertEqual(self.client.post("/v1/projects", json={**body, "title": "Changed"}, headers=headers).status_code, 409)
        self.assertEqual(self.client.post("/v1/projects", json={**body, "workspace": "unknown"}).status_code, 422)
        self.login("supervan")
        self.assertEqual(self.client.get(f'/v1/projects/{record["id"]}/shots/{shot_id}/generation-draft').status_code, 404)

    def test_configure_partial_patch_preserves_controls_materials_and_candidates(self):
        self.setup_project()
        self.source("image-a", "image")
        self.source("image-b", "image")
        self.configure(prompt="  Saved prompt  ", controls={"duration": 5, "seed": "123", "encoder_device": "cpu"},
            inputs={"first_frame": {"asset_id": "image-a"}, "last_frame": {"asset_id": "image-b"}}).raise_for_status()
        before = self.client.get("/v1/projects/story-one").json()["project"]
        updated = self.configure(controls={"steps": 20}).json()["project"]
        self.assertEqual(before["links"], updated["links"])
        self.assertEqual(self.draft().json()["draft"]["controls"]["seed"], "123")
        result = self.preflight()
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["effective_request"]["prompt"], "Saved prompt")
        self.assertEqual(result.json()["effective_request"]["inputs"]["first_frame"], "image-a")
        self.assertEqual(result.json()["status"], "blocked")
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)

    def test_mode_change_keeps_incompatible_slots_until_explicit_clear(self):
        self.setup_project()
        self.source("image-a", "image")
        self.source("image-b", "image")
        self.configure(prompt="Actor", inputs={"first_frame": {"asset_id": "image-a"}}).raise_for_status()
        self.configure(recipe_id="h3-base-ref2va-v1", inputs={"images": [{"asset_id": "image-b"}]}).raise_for_status()
        self.assertEqual(self.draft().json()["draft"]["inputs"]["first_frame"]["asset_id"], "image-a")
        self.assertEqual(self.preflight().status_code, 422)
        self.configure(inputs={"first_frame": None}).raise_for_status()
        result = self.preflight()
        self.assertEqual(result.status_code, 201, result.text)
        project = self.client.get("/v1/projects/story-one").json()["project"]
        self.assertEqual(len([e for e in project["entities"] if e["type"] == "image"]), 2)
        self.configure(inputs={"images": []}).raise_for_status()
        self.assertEqual(self.preflight().status_code, 422)

    def test_reference_and_guide_ranges_video_audio_switch_roundtrip_and_shared_derivative(self):
        self.setup_project()
        self.source("video-a", "video")
        self.source("audio-a", "audio")
        self.source("video-derived", "video", duration=3)
        self.configure(recipe_id="h3-base-ref2va-v1", prompt="Follow motion", inputs={
            "videos": [{"asset_id": "video-a", "include_audio": False, "source_range": {"start": 1, "end": 4}}],
            "audios": [{"asset_id": "audio-a"}],
            "guides": [{"media_id": "video-a", "time_seconds": 0, "use_audio": False,
                        "source_range": {"start": 1, "end": 4}}]}).raise_for_status()
        before = self.draft().json()["draft"]
        links = self.client.get("/v1/projects/story-one").json()["project"]["links"]
        self.configure(inputs={"videos": [{"asset_id": "video-a"}],
            "guides": [{"media_id": "video-a", "time_seconds": 0}]}).raise_for_status()
        self.assertEqual(self.draft().json()["draft"], before)
        self.assertEqual(self.client.get("/v1/projects/story-one").json()["project"]["links"], links)
        with patch.object(self.app.state.assets, "derive", return_value={"asset_id": "video-derived", "status": "ready"}) as derive:
            result = self.preflight()
        self.assertEqual(result.status_code, 201, result.text)
        derive.assert_called_once_with("superdan", "video-a", 1, 4)
        effective = result.json()["effective_request"]
        self.assertFalse(effective["video_audio"]["video-derived"])
        self.assertEqual(effective["guides"][0]["media_id"], "video-derived")
        self.configure(inputs={"videos": [{"asset_id": "video-a", "purpose": "reference"}]}).raise_for_status()
        changed = self.draft().json()["draft"]["inputs"]["videos"][0]
        self.assertEqual(changed["purpose"], "reference")
        self.assertEqual(changed["source_range"], {"start": 1, "end": 4})
        self.assertFalse(changed["include_audio"])
        self.configure(inputs={"videos": [{"asset_id": "video-a", "source_range": None}]}).raise_for_status()
        self.assertNotIn("source_range", self.draft().json()["draft"]["inputs"]["videos"][0])

    def test_guides_reuse_image_without_duplicate_entities_or_candidates(self):
        self.setup_project()
        self.source("image-a", "image")
        result = self.configure(recipe_id="h3-base-ref2va-v1", prompt="At one second", inputs={
            "images": [{"asset_id": "image-a"}], "guides": [{"media_id": "image-a", "time_seconds": 1}]}).json()
        self.assertEqual(len([e for e in result["project"]["entities"] if e["type"] == "image"]), 1)
        self.assertNotIn("candidateIds", result["project"]["entities"][2]["data"])
        preflight = self.preflight()
        self.assertEqual(preflight.status_code, 201, preflight.text)
        self.assertEqual(preflight.json()["effective_request"]["guides"][0]["media_id"], "image-a")

    def test_edit_idempotency_version_conflict_and_failed_batch_are_atomic(self):
        self.setup_project()
        action = {"op": "shot.configure_generation", "shot_id": "shot-one", "prompt": "First"}
        first = self.edit([action], key="draft-once")
        self.assertEqual(first.json(), self.edit([action], key="draft-once").json())
        self.assertEqual(self.edit([{**action, "prompt": "Changed"}], key="draft-once").status_code, 409)
        self.assertEqual(self.edit([action]).status_code, 409)
        response = self.edit([{**action, "prompt": "Rollback"}, {**action, "shot_id": "missing"}], version=2)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.draft().json()["draft"]["prompt"], "First")

    def test_unknown_fields_bad_types_and_ranges_never_change_document(self):
        self.setup_project()
        self.source("image-a", "image")
        self.source("video-a", "video")
        cases = [
            {"extra": True}, {"recipe_id": []}, {"controls": {"mystery": 1}}, {"controls": {"steps": True}},
            {"controls": {"guides": []}}, {"controls": {"audio_tile_size": 1}}, {"prompt": None},
            {"inputs": {"unknown": []}}, {"inputs": {"first_frame": {"asset_id": "video-a"}}},
            {"inputs": {"images": [{"asset_id": "image-a", "include_audio": True}]}},
            {"inputs": {"images": [{"asset_id": "image-a", "purpose": "arbitrary"}]}},
            {"inputs": {"videos": [{"asset_id": "video-a", "source_range": {"start": 9, "end": 12}}]}},
            {"inputs": {"guides": [{"media_id": "image-a", "time_seconds": 0, "use_audio": True}]}},
            {"inputs": {"guides": [{"media_id": "image-a", "time_seconds": True}]}},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                result = self.configure(**fields)
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(self.draft().json()["project_version"], 1)

    def test_other_owner_project_unready_and_scoped_keys_are_rejected(self):
        self.setup_project()
        self.source("foreign-owner", "image", owner="supervan")
        self.source("foreign-project", "image", project="other")
        self.source("unready", "image", status="validating")
        self.source("own", "image")
        for ident, status in (("foreign-owner", 404), ("foreign-project", 404), ("unready", 409)):
            result = self.configure(inputs={"first_frame": {"asset_id": ident}})
            self.assertEqual(result.status_code, status, result.text)
        _, headers = self.key(scopes=["projects:read", "projects:write", "jobs:write"])
        result = self.edit([{"op": "shot.configure_generation", "shot_id": "shot-one",
            "inputs": {"first_frame": {"asset_id": "own"}}}], headers=headers)
        self.assertEqual(result.status_code, 404)
        self.configure(prompt="Actor", inputs={"first_frame": {"asset_id": "own"}}).raise_for_status()
        result = self.client.post("/v1/projects/story-one/shots/shot-one/generation-plans",
            json={"expected_version": 2}, headers=headers)
        self.assertEqual(result.status_code, 404)

    def test_selected_range_requires_asset_write_before_derivation(self):
        self.setup_project()
        self.source("video-a", "video")
        self.configure(recipe_id="h3-base-ref2va-v1", prompt="Motion", inputs={
            "videos": [{"asset_id": "video-a", "source_range": {"start": 0, "end": 3}}]}).raise_for_status()
        _, headers = self.key(scopes=["projects:read", "assets:read", "jobs:write"])
        with patch.object(self.app.state.assets, "derive") as derive:
            result = self.client.post("/v1/projects/story-one/shots/shot-one/generation-plans",
                json={"expected_version": 2}, headers=headers)
        self.assertEqual(result.status_code, 404)
        derive.assert_not_called()

    def test_preflight_rejects_stale_and_override_body_without_making_plan(self):
        self.setup_project()
        self.configure(prompt="Saved").raise_for_status()
        for value in ({"expected_version": 1}, {"expected_version": 2, "prompt": "Override"},
                      {"expected_version": True}, {"expected_version": 2, "capabilities_version": "stale"}):
            result = self.client.post("/v1/projects/story-one/shots/shot-one/generation-plans", json=value)
            self.assertIn(result.status_code, (409, 422))
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(plans)), 0)

    def test_concurrent_edit_during_derivation_rejects_old_snapshot(self):
        self.setup_project()
        self.source("video-a", "video")
        self.configure(recipe_id="h3-base-ref2va-v1", prompt="Motion", inputs={
            "videos": [{"asset_id": "video-a", "source_range": {"start": 0, "end": 3}}]}).raise_for_status()
        def derive(*args):
            self.configure(prompt="Changed while trimming").raise_for_status()
            return {"status": "ready", "asset_id": "unneeded-derived"}
        with patch.object(self.app.state.assets, "derive", side_effect=derive):
            result = self.preflight()
        self.assertEqual(result.status_code, 409, result.text)
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(plans)), 0)

    def test_empty_prompt_stays_empty_and_does_not_use_description(self):
        self.setup_project()
        self.configure(prompt="").raise_for_status()
        self.assertEqual(self.draft().json()["draft"]["prompt"], "")
        self.assertEqual(self.preflight().status_code, 422)

    def test_saved_range_file_replacement_is_reported_not_silently_ignored(self):
        self.setup_project()
        self.source("video-a", "video")
        self.configure(recipe_id="h3-base-ref2va-v1", prompt="Motion", inputs={
            "videos": [{"asset_id": "video-a", "source_range": {"start": 0, "end": 3}}]}).raise_for_status()
        entity = next(e for e in self.client.get("/v1/projects/story-one").json()["project"]["entities"] if e["type"] == "video")
        self.edit([{"op": "entity.update", "entity_id": entity["id"], "patch": {"data": {"fileId": "changed"}}}], version=2).raise_for_status()
        self.assertTrue(self.draft().json()["issues"])
        with patch.object(self.app.state.assets, "derive") as derive:
            self.assertEqual(self.preflight().status_code, 422)
        derive.assert_not_called()

    def test_missing_browser_file_and_malformed_legacy_range_are_actionable(self):
        self.setup_project()
        self.source("video-a", "video")
        self.configure(recipe_id="h3-base-ref2va-v1", prompt="Motion", inputs={"videos": [{"asset_id": "video-a"}]}).raise_for_status()
        record = self.client.get("/v1/projects/story-one").json()
        project = record["project"]
        shot = next(e for e in project["entities"] if e["id"] == "shot-one")
        shot["data"]["referenceRanges"] = {project["links"][0]["id"]: True}
        self.client.put("/v1/projects/story-one", json={"project": project, "expected_version": 2}).raise_for_status()
        self.assertTrue(self.draft().json()["issues"])
        self.assertEqual(self.preflight().status_code, 422)
        project = self.client.get("/v1/projects/story-one").json()["project"]
        shot = next(e for e in project["entities"] if e["id"] == "shot-one")
        shot["data"]["referenceRanges"] = {}
        next(e for e in project["entities"] if e["type"] == "video")["data"]["missingFile"] = True
        self.client.put("/v1/projects/story-one", json={"project": project, "expected_version": 3}).raise_for_status()
        self.assertTrue(self.draft().json()["issues"])
        self.assertEqual(self.preflight().status_code, 422)

    def test_preset_applies_only_to_unset_fields_of_saved_plan(self):
        self.setup_project()
        self.configure(prompt="Actor", controls={"encoder_device": "default"}).raise_for_status()
        recipe_caps = capabilities(self.settings)
        recipe_caps["recipes"][0]["deployment_preset"] = {"applies_to": "unset_controls_only",
            "controls": {"encoder_device": "cpu", "video_decode": "tiled"}}
        with patch("studio_platform.api.capabilities", return_value=recipe_caps):
            result = self.preflight()
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["effective_request"]["encoder_device"], "default")
        self.assertEqual(result.json()["effective_request"]["video_decode"], "tiled")
        self.assertEqual(self.draft().json()["draft"]["controls"], {"encoder_device": "default"})

    def test_browser_cleared_numeric_control_uses_existing_default_semantics(self):
        self.setup_project()
        self.edit([{"op": "entity.update", "entity_id": "shot-one", "patch": {"data": {
            "prompt": "Actor", "h3": {"controls": {"duration": None, "steps": None}}}}}]).raise_for_status()
        result = self.preflight()
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["effective_request"]["duration"], 5)
        self.assertEqual(result.json()["effective_request"]["steps"], 20)

    def test_api_inherits_uploaded_story_images_only_for_saved_ref_input_mode(self):
        self.setup_project()
        self.source("portrait", "image")
        self.source("station", "image")
        record = self.client.get("/v1/projects/story-one").json()
        document = record["project"]
        document["entities"].extend([
            {"id": "portrait-image", "type": "image", "title": "Portrait", "description": "", "parentId": None,
             "version": 1, "status": "draft", "order": 0, "data": {"fileId": "portrait-file", "cloudAssetId": "portrait"}},
            {"id": "station-image", "type": "image", "title": "Station", "description": "", "parentId": None,
             "version": 1, "status": "draft", "order": 1, "data": {"fileId": "station-file", "cloudAssetId": "station"}},
            {"id": "actor", "type": "character", "title": "Traveller", "description": "", "parentId": None,
             "version": 1, "status": "draft", "order": 2, "data": {"looks": [
                 {"id": "raincoat", "name": "Raincoat", "version": 1, "gallery": {"front": "portrait-image"}}]}},
            {"id": "place", "type": "location", "title": "Station", "description": "", "parentId": None,
             "version": 1, "status": "draft", "order": 3, "data": {"referenceAssetIds": ["station-image"]}}])
        document["entities"][1]["data"].update(cast=[{"characterId": "actor", "lookId": "raincoat"}], locationId="place")
        document["entities"][2]["data"].update(prompt="Enter the station", h3={"inputMode": "ref"})
        self.client.put("/v1/projects/story-one", json={"project": document,
            "expected_version": record["version"]}).raise_for_status()
        draft = self.draft().json()
        self.assertEqual(draft["issues"], [])
        self.assertEqual(draft["draft"]["recipe_id"], "h3-base-ref2va-v1")
        self.assertEqual([entry["asset_id"] for entry in draft["draft"]["inputs"]["images"]], ["portrait", "station"])
        result = self.preflight()
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["effective_request"]["inputs"]["images"], ["portrait", "station"])
        _, headers = self.key(scopes=["projects:read", "jobs:write"])
        denied = self.client.post("/v1/projects/story-one/shots/shot-one/generation-plans",
            json={"expected_version": draft["project_version"]}, headers=headers)
        self.assertEqual(denied.status_code, 404)  # Shared images still require assets:read.
        self.configure(recipe_id="h3-base-fl2va-v1").raise_for_status()
        self.assertEqual(self.draft().json()["draft"]["inputs"]["images"], [])
        result = self.preflight()
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["effective_request"]["inputs"]["images"], [])

    def test_upload_contract_matches_small_image_rejection_and_source_limits(self):
        from studio_platform.media import EXTENSIONS
        self.setup_project()
        limits = self.client.get("/v1/capabilities").json()["upload_constraints"]
        self.assertEqual(limits["max_bytes"], self.settings.max_upload_bytes)
        self.assertEqual(limits["allowed_extensions"], EXTENSIONS)
        for kind in ("image", "video"):
            self.assertEqual({k: limits[kind][k] for k in ("min_side", "max_side", "min_aspect_ratio", "max_aspect_ratio")},
                {"min_side": 256, "max_side": 5760, "min_aspect_ratio": .4, "max_aspect_ratio": 2.5})
        for kind in ("video", "audio"):
            self.assertEqual(limits[kind]["min_source_duration_seconds"], .1)
            self.assertEqual(limits[kind]["max_source_duration_seconds"], 3600)
        self.assertEqual(limits["scope"], "upload_inspection_only")
        picture = io.BytesIO()
        Image.new("RGB", (320, 180), "navy").save(picture, format="PNG")
        response = self.client.post("/v1/assets", data={"client_project_id": "story-one"},
            files={"file": ("small.png", picture.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("256", response.text)
        self.assertFalse(min(320, 180) >= limits["image"]["min_side"])


if __name__ == "__main__":
    unittest.main()
