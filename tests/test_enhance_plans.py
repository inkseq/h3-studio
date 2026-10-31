"""Enhance plans and credits (#795); no provider, GPU or network."""

import unittest

from studio_platform.enhance_plans import RECIPE, compile_enhance, credits

PROJECT = {
    "id": "project-1",
    "entities": [
        {"id": "take-1", "type": "video", "data": {"cloudArtifactId": "artifact-1"}},
        {"id": "upload-1", "type": "video", "data": {"cloudAssetId": "asset-1"}},
        {"id": "audio-1", "type": "audio", "data": {"cloudArtifactId": "a"}},
        {"id": "broken-1", "type": "video", "data": "artifact-1"},
    ],
}


def source(**changes):
    return {
        "kind": "video",
        "duration": 124 / 24,
        "source_job_id": "job-1",
        "artifact_id": "artifact-1",
        "object": {"key": "k", "size_bytes": 20 * 1024**2, "sha256": "a" * 64},
        "simulation": False,
        "recipe_id": "h3-default",
        "width": 1376,
        "height": 768,
        "shot": {"shot_id": "shot-1", "shot_version": 3},
        **changes,
    }


def body(tier="2k", entity="take-1"):
    return {
        "client_ref": {"project_id": "project-1", "entity_id": entity},
        "tier": tier,
    }


class EnhancePlanTests(unittest.TestCase):
    def test_owner_prices_per_clip_then_per_started_extra_second(self):
        self.assertEqual(credits("1080p", 124 / 24), 100)
        self.assertEqual(credits("2k", 124 / 24), 150)
        self.assertEqual(credits("1080p", 5.25), 100)
        self.assertEqual(credits("1080p", 5.26), 110)
        self.assertEqual(credits("2k", 10.0), 150 + 30 * 5)

    def test_compiles_only_a_validated_generated_take(self):
        compiled, display = compile_enhance(body(), PROJECT, lambda e, k: source())
        self.assertEqual(compiled["recipe_id"], RECIPE)
        self.assertEqual(
            compiled["client_ref"],
            {
                "project_id": "project-1",
                "entity_id": "take-1",
                "shot_id": "shot-1",
                "shot_version": 3,
            },
        )
        loose, _ = compile_enhance(body(), PROJECT, lambda e, k: source(shot={}))
        self.assertEqual(set(loose["client_ref"]), {"project_id", "entity_id"})
        self.assertEqual(
            compiled["output_spec"],
            {
                "width": 2560,
                "height": 1440,
                "fps": 24,
                "frame_count": 121,
                "actual_duration": 121 / 24,
            },
        )
        # The workflow keeps 8k+1 frames: a 124-frame take comes back as 121.
        for seconds, frames in ((121 / 24, 121), (241 / 24, 241), (15, 353)):
            spec = compile_enhance(
                body(), PROJECT, lambda e, k: source(duration=seconds)
            )[0]["output_spec"]
            self.assertEqual(spec["frame_count"], frames)
        worker = compiled["request"]["enhance"]
        self.assertEqual(
            (worker["res"], worker["steps"], worker["sigma"]), ("2k", 3, 0.8)
        )
        self.assertEqual(worker["source_artifact_id"], "artifact-1")
        self.assertNotIn("prompt", worker)
        prompted, _ = compile_enhance(body(), PROJECT, lambda e, k: source(prompt="p"))
        self.assertEqual(prompted["request"]["enhance"]["prompt"], "p")
        self.assertEqual(display["credits"], 150)
        refused = [
            ({"tier": "2k"}, lambda e, k: source()),
            (body(tier="4k"), lambda e, k: source()),
            (body(entity="upload-1"), lambda e, k: source()),
            (body(entity="audio-1"), lambda e, k: source()),
            (body(entity="missing"), lambda e, k: source()),
            (body(entity="broken-1"), lambda e, k: source()),
            (body(), lambda e, k: source(duration=16)),
            # Only generated takes: no re-enhance, no chapter render, no 2K input.
            (body(), lambda e, k: source(recipe_id=RECIPE)),
            (body(), lambda e, k: source(recipe_id="chapter-roughcut-v1")),
            (body(), lambda e, k: source(recipe_id=None)),
            (body(), lambda e, k: source(width=2560, height=1440)),
            (body(), lambda e, k: source(width=None, height=None)),
            (body(), lambda e, k: source(shot={"shot_id": 7})),
            (body(), lambda e, k: source(shot={"other": "x"})),
            (body(), lambda e, k: source(artifact_id=None)),
            (body(), lambda e, k: source(object={"key": "k", "size_bytes": 0})),
        ]
        for value, resolver in refused:
            with self.subTest(value=value), self.assertRaises(ValueError):
                compile_enhance(value, PROJECT, resolver)


if __name__ == "__main__":
    unittest.main()
