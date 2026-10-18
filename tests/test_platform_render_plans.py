"""No model/provider calls: timeline ownership, immutable consent and CPU limits."""
import copy
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_platform_api as api_fixtures
from studio_platform.api import create_app
from studio_platform.render_plans import compile_render, render_source_hash, configuration_for
from studio_platform.project_validation import validate_project
from studio_platform.repository import NotFound
from studio_platform.settings import Settings


def story():
    p = api_fixtures.project()
    p["entities"][2]["data"].update(seconds=2, selectedAssetId="clip")
    p["entities"].append(dict(id="clip", type="video", title="Clip", version=1, order=0,
        parentId=None, description="", status="draft", data={"fileId": "local-clip", "cloudAssetId": "asset-one"}))
    p["journey"] = {"brief": {"aspect": "9:16"}, "sound": {"mode": "silent"}, "soundTracks": {}}
    return p


BODY = {"client_ref": {"project_id": "story-one", "chapter_id": "chapter-one"}, "resolution": "480P"}


def source(entity, kind):
    return {"kind": kind, "duration": 5, "simulation": True,
        "object": {"key": "owners/test/assets/test/file.mp4", "sha256": "a"*64,
                   "size_bytes": 1234, "content_type": "video/mp4"}}


class RenderPlanTests(unittest.TestCase):
    def selected_range(self, project, start=1, end=4):
        value = {"assetId": "clip", "fileId": "local-clip", "cloudAssetId": "asset-one",
                 "cloudArtifactId": None, "start": start, "end": end}
        project["entities"][2]["data"]["selectedVideoRange"] = value
        return value

    def test_nonzero_range_is_bound_and_snapped_inward_without_changing_shot(self):
        p = story()
        self.selected_range(p, 1.01, 3.99)
        compiled, timeline, blockers, warnings = compile_render(BODY, p, source)
        self.assertFalse(blockers)
        shot = compiled["request"]["render"]["shots"][0]
        self.assertEqual((compiled["request"]["render"]["version"], configuration_for(compiled)), (3, "cpu-render-v3"))
        self.assertEqual((shot["source_start_frame"], shot["frames"]), (25, 48))
        self.assertEqual(p["entities"][2]["data"]["seconds"], 2)
        self.assertAlmostEqual(timeline["shots"][0]["source_start"], 25/24)
        self.assertAlmostEqual(timeline["shots"][0]["source_end"], 73/24)
        self.assertTrue(any("向内对齐" in item for item in warnings))
        self.assertTrue(any("尾部" in item for item in warnings))

    def test_range_too_short_or_stale_is_saved_as_draft_but_cannot_render(self):
        for mutate in (lambda p, r: r.update(end=2), lambda p, r: r.update(fileId="replaced"),
                       lambda p, r: r.update(cloudAssetId="another-asset"),
                       lambda p, r: r.update(cloudArtifactId="another-artifact"),
                       lambda p, r: r.update(assetId="old-selected-entity"), lambda p, r: r.update(end=8)):
            p = story()
            value = self.selected_range(p)
            mutate(p, value)
            validate_project(p)  # Preserve the old editing intent for recovery.
            compiled, _, blockers, _ = compile_render(BODY, p, source)
            self.assertTrue(blockers)
            self.assertFalse(compiled["request"]["render"]["shots"])

    def test_range_zero_and_exact_fraction_do_not_drop_a_frame(self):
        p = story()
        p["entities"][2]["data"]["seconds"] = 1/24
        self.selected_range(p, 5/24, 6/24)
        compiled, _, blockers, _ = compile_render(BODY, p, source)
        self.assertFalse(blockers)
        self.assertEqual(compiled["request"]["render"]["shots"][0]["source_start_frame"], 5)
        self.assertEqual(compiled["request"]["render"]["shots"][0]["frames"], 1)
        p["entities"][2]["data"]["selectedVideoRange"] = None
        compiled, _, blockers, _ = compile_render(BODY, p, source)
        self.assertFalse(blockers)
        self.assertEqual(compiled["request"]["render"]["shots"][0]["source_start_frame"], 0)

    def test_range_invalid_structure_and_times_are_rejected_on_save(self):
        for mutate in (lambda r: r.update(start=True), lambda r: r.update(start=-1), lambda r: r.update(end=0),
                       lambda r: r.update(end=float("inf")), lambda r: r.update(fileId=3),
                       lambda r: r.update(url="https://untrusted.invalid"), lambda r: r.pop("cloudArtifactId")):
            p = story()
            value = self.selected_range(p)
            mutate(value)
            with self.assertRaises(ValueError):
                validate_project(p)

    def test_range_edit_invalidates_source_hash_and_legacy_version_is_explicit(self):
        p = story()
        before = render_source_hash(p, "chapter-one")
        self.selected_range(p)
        self.assertNotEqual(before, render_source_hash(p, "chapter-one"))
        self.assertEqual(configuration_for({"request": {"render": {"version": 1}}}), "cpu-render-v1")
        for version in (True, 0, 4, None):
            with self.assertRaises(ValueError):
                configuration_for({"request": {"render": {"version": version}}})

    def test_execution_policy_keeps_legacy_and_current_configurations_separate(self):
        from studio_platform.execution_policy import ExecutionPolicies
        from studio_platform.repository import Repository, Scope
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), auth_mode="local-test", render_enabled=True)
            repo = Repository(settings.database_url)
            repo.create_schema()
            try:
                policy = ExecutionPolicies(settings, repo)
                current = compile_render(BODY, story(), source)[0]
                legacy = copy.deepcopy(current)
                legacy["request"]["render"]["version"] = 1
                legacy["request"]["render"].pop("subtitles")
                for shot in legacy["request"]["render"]["shots"]:
                    shot.pop("source_start_frame")
                with patch.object(policy.control, "pool_status", return_value={"ready": 1, "busy": 0}):
                    for compiled, expected in ((current, "cpu-render-v3"), (legacy, "cpu-render-v1")):
                        admission = policy.evaluate(compiled, Scope("sixnine", "superdan", "story-one"), "a"*64)
                        self.assertEqual(admission.execution["configuration_id"], expected)
                        job = {"request": compiled, "execution_plan": admission.execution}
                        self.assertTrue(policy.submission_allowed(job))
                        wrong = copy.deepcopy(job)
                        wrong["execution_plan"]["configuration_id"] = "cpu-render-v1" if expected.endswith("v3") else "cpu-render-v2"
                        self.assertFalse(policy.submission_allowed(wrong))
            finally:
                repo.close()

    def test_exact_frames_and_output_aspect(self):
        p = story()
        p["entities"][2]["data"]["seconds"] = 2.01
        compiled, timeline, blockers, warnings = compile_render(BODY, p, source)
        self.assertFalse(blockers)
        self.assertEqual(compiled["output_spec"], dict(width=480, height=854, fps=24, frame_count=48, actual_duration=2))
        self.assertTrue(compiled["simulation"])
        self.assertEqual(timeline["shots"][0]["duration"], 2)
        self.assertTrue(any("24fps" in w for w in warnings))

    def test_short_video_blocks_without_silent_retiming(self):
        p = story()
        p["entities"][2]["data"]["seconds"] = 6
        compiled, _, blockers, _ = compile_render(BODY, p, source)
        self.assertTrue(any("只有5.000秒" in b for b in blockers))
        self.assertEqual(compiled["request"]["duration"], 6)
        self.assertFalse(compiled["request"]["render"]["shots"])

    def test_still_or_missing_selection_is_blocked(self):
        for typ in ("image", "audio"):
            p = story()
            p["entities"][-1]["type"] = typ
            _, _, blockers, _ = compile_render(BODY, p, source)
            self.assertTrue(blockers)

    def test_audio_clips_tail_and_checks_file_binding(self):
        p = story()
        audio = copy.deepcopy(p["entities"][-1])
        audio.update(id="music", type="audio", title="Music")
        audio["data"]["fileId"] = "local-audio"
        p["entities"].append(audio)
        p["journey"]["sound"]["mode"] = "music"
        track = {"assetId": "music", "fileId": "local-audio", "start": 1, "end": 4,
                 "gain": .5, "offset": 1, "shotId": "shot-one"}
        p["journey"]["soundTracks"]["chapter-one"] = [track]
        compiled, _, blockers, warnings = compile_render(BODY, p, source)
        self.assertFalse(blockers)
        self.assertTrue(compiled["request"]["generate_audio"])
        self.assertEqual(compiled["request"]["render"]["audio_tracks"][0]["source_end"], 2)
        self.assertTrue(any("尾部将裁掉" in w for w in warnings))
        track["fileId"] = "stale"
        self.assertTrue(compile_render(BODY, p, source)[2])

    def test_muted_draft_tracks_do_not_add_simulated_sources(self):
        p = story()
        p["journey"]["soundTracks"]["chapter-one"] = [{"assetId": "missing", "muted": False}]
        compiled, _, blockers, _ = compile_render(BODY, p, lambda *args: {**source(*args), "simulation": False})
        self.assertFalse(blockers)
        self.assertFalse(compiled["simulation"])
        self.assertEqual(len(compiled["sources"]), 1)

    def test_source_hash_catches_same_version_replacement(self):
        p = story()
        before = render_source_hash(p, "chapter-one")
        p["entities"][-1]["data"]["cloudAssetId"] = "replaced"
        self.assertNotEqual(before, render_source_hash(p, "chapter-one"))

    def test_order_and_no_arbitrary_object_keys(self):
        p = story()
        shot = copy.deepcopy(p["entities"][2])
        p["entities"][2]["order"] = 1
        shot.update(id="shot-earlier", order=0)
        p["entities"].append(shot)
        compiled, _, blockers, _ = compile_render(BODY, p, source)
        self.assertFalse(blockers)
        self.assertEqual(compiled["request"]["render"]["shots"][0]["shot_id"], "shot-earlier")
        with self.assertRaises(ValueError):
            compile_render({**BODY, "sources": {"url": "https://bad.invalid"}}, p, source)

    def test_invalid_types_and_oversized_chapter(self):
        p = story()
        for mode in ([], {}, None):
            p["journey"]["sound"]["mode"] = mode
            self.assertTrue(compile_render(BODY, p, source)[2])
        p = story()
        for index in range(50):
            s = copy.deepcopy(p["entities"][2])
            s["id"] = "extra-"+str(index)
            p["entities"].append(s)
        self.assertTrue(any("1–50" in b for b in compile_render(BODY, p, source)[2]))


class RenderApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(Settings(Path(self.tmp.name), auth_mode="local-test", render_enabled=True))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/auth/login", json={"username": "superdan"})
        self.project = story()
        self.client.post("/v1/projects", json={"project": self.project}).raise_for_status()
        path = Path(self.tmp.name)/"fixture.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=navy:s=256x256:r=24",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(path)], check=True, capture_output=True)
        with path.open("rb") as data:
            response = self.client.post("/v1/assets", data={"client_project_id": "story-one"}, files={"file": ("fixture.mp4", data, "video/mp4")})
        self.assertEqual(response.status_code, 201, response.text)
        self.project["entities"][-1]["data"]["cloudAssetId"] = response.json()["id"]
        self.save(1)
        self.capacity = patch("studio_platform.control.WorkerControl.pool_status", return_value={"ready": 1, "busy": 0})
        self.capacity.start()
        self.addCleanup(self.capacity.stop)

    def save(self, version):
        result = self.client.put("/v1/projects/story-one", json={"project": self.project, "expected_version": version})
        self.assertEqual(result.status_code, 200, result.text)

    def plan(self):
        result = self.client.post("/v1/render-plans", json=BODY)
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def test_local_source_real_decode_and_idempotent_consent(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "ready", plan)
        self.assertFalse(plan["simulation"])
        self.assertNotIn("object", str(plan))
        response = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "roughcut-first"})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(response.json()["recipe_id"], "chapter-roughcut-v1")
        self.project["entities"][2]["data"]["seconds"] = 1
        self.save(2)
        retry = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "roughcut-first"})
        self.assertEqual(retry.json()["id"], response.json()["id"])
        fresh = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "roughcut-new"})
        self.assertEqual(fresh.status_code, 409)

    def test_http_trim_precheck_and_edit_invalidate_old_consent(self):
        shot, video = self.project["entities"][2], self.project["entities"][-1]
        shot["data"].update(seconds=.5, selectedVideoRange={"assetId": video["id"], "fileId": video["data"]["fileId"],
            "cloudAssetId": video["data"]["cloudAssetId"], "cloudArtifactId": None, "start": .5, "end": 1.5})
        self.save(2)
        plan = self.plan()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual((plan["timeline"]["shots"][0]["source_start"], plan["timeline"]["shots"][0]["source_end"]), (.5, 1))
        shot["data"]["selectedVideoRange"]["start"] = .75
        self.save(3)
        response = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "old-trim"})
        self.assertEqual(response.status_code, 409)
        current = self.plan()
        self.assertEqual(self.client.post("/v1/jobs", json={"plan_id": current["plan_id"]},
            headers={"Idempotency-Key": "new-trim"}).status_code, 202)

    def test_other_owner_and_same_owner_other_project_source_denied(self):
        borrowed = copy.deepcopy(self.project)
        borrowed["id"] = "second-project"
        self.client.post("/v1/projects", json={"project": borrowed}).raise_for_status()
        response = self.client.post("/v1/render-plans", json={"client_ref": {"project_id": "second-project", "chapter_id": "chapter-one"}})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "blocked")
        self.client.post("/api/auth/login", json={"username": "supervan"})
        self.assertEqual(self.client.post("/v1/render-plans", json=BODY).status_code, 404)
        # Reference fencing rejects a new cross-owner attachment at save time,
        # before it could race that owner's permanent cleanup. The same-owner
        # other-project case above still reaches the original render precheck.
        denied = self.client.post("/v1/projects", json={"project": self.project})
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(self.client.get("/v1/projects/story-one").status_code, 404)


if __name__ == "__main__":
    unittest.main()
