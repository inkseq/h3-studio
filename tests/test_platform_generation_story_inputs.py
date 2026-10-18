"""Story image inheritance uses the selected H3 mode; no IO or provider calls."""
import copy
from pathlib import Path
import tempfile
import unittest

from studio_platform.capabilities import capabilities
from studio_platform.generation_draft import plan_body, read_draft, selected_recipe
from studio_platform.settings import Settings
from test_platform_api import project


FL = "h3-base-fl2va-v1"
REF = "h3-base-ref2va-v1"


class StoryInputTests(unittest.TestCase):
    def setUp(self):
        self.project = project()
        self.scene, self.shot = self.project["entities"][1:3]
        self.shot["data"].update(prompt="A traveller enters a rainy station.", h3={})
        self.caps = capabilities(Settings(Path(tempfile.gettempdir()) / "unused-story-input-no-io"))

    def entity(self, ident, kind, data):
        entity = {"id": ident, "type": kind, "parentId": None, "title": ident,
                  "description": "", "version": 1, "status": "draft", "order": 0, "data": data}
        self.project["entities"].append(entity)
        return entity

    def image(self, ident):
        return self.entity(ident, "image", {"cloudAssetId": "receipt-" + ident, "fileId": "file-" + ident})

    def location(self, ident, *images):
        return self.entity(ident, "location", {"referenceAssetIds": list(images)})

    def link(self, ident, role):
        self.project["links"].append({"id": "link-" + ident + "-" + role,
            "source": ident, "target": self.shot["id"], "role": role})

    def plan(self):
        return plan_body(self.project, self.shot["id"], self.caps,
                         lambda *_: self.fail("Images must not start derivation"))

    def test_recipe_id_wins_over_mode_and_unknown_explicit_choice_never_falls_back(self):
        recipes = self.caps["recipes"]
        for saved, expected in (({}, FL), ({"inputMode": "ref"}, REF),
            ({"recipeId": FL, "inputMode": "ref"}, FL),
            ({"recipeId": REF, "inputMode": "fl"}, REF)):
            with self.subTest(saved=saved):
                self.assertEqual(selected_recipe(recipes, saved)["id"], expected)
                self.shot["data"]["h3"] = saved
                self.assertEqual(read_draft(self.project, self.shot["id"])[0]["recipe_id"], expected)
        for saved in ({"recipeId": "removed-recipe", "inputMode": "ref"}, {"inputMode": "unknown"}):
            with self.subTest(saved=saved):
                self.assertIsNone(selected_recipe(recipes, saved))
                self.shot["data"]["h3"] = saved
                with self.assertRaisesRegex(ValueError, "配方已不可用"):
                    self.plan()

    def test_ref_mode_inherits_character_with_shot_look_override_and_scene_location(self):
        for ident in ("rain-look", "formal-look", "station-image"):
            self.image(ident)
        self.entity("actor", "character", {"looks": [
            {"id": "rain", "gallery": {"front": "rain-look"}},
            {"id": "formal", "gallery": {"front": "formal-look"}}]})
        self.location("station", "station-image")
        self.scene["data"].update(cast=[{"characterId": "actor", "lookId": "rain"}], locationId="station")
        self.shot["data"].update(cast=[{"characterId": "actor", "lookId": "formal"}], h3={"inputMode": "ref"})
        body = self.plan()
        self.assertEqual(body["recipe_id"], REF)
        self.assertEqual(body["inputs"]["images"], [
            {"asset_id": "receipt-formal-look", "purpose": "identity"},
            {"asset_id": "receipt-station-image", "purpose": "reference"}])

    def test_fl_retains_story_bindings_but_sends_only_explicit_frames(self):
        self.image("frame")
        self.entity("actor", "character", {"looks": []})
        self.scene["data"].update(cast=[{"characterId": "actor", "lookId": "not-ready"}], locationId="missing-location")
        self.shot["data"]["h3"] = {"recipeId": FL, "inputMode": "ref"}
        self.link("actor", "identity")
        self.link("frame", "firstFrame")
        before = copy.deepcopy(self.project)
        body = self.plan()
        self.assertEqual(body["inputs"]["images"], [])
        self.assertEqual(body["inputs"]["first_frame"], {"asset_id": "receipt-frame"})
        self.assertEqual(self.project, before)
        self.link("frame", "reference")
        with self.assertRaisesRegex(ValueError, "不能混用全能参考"):
            self.plan()

    def test_shot_location_overrides_scene_and_location_edges_add_with_dedup(self):
        for ident in ("scene-image", "shot-image", "linked-image"):
            self.image(ident)
        self.location("scene-location", "scene-image")
        self.location("shot-location", "shot-image")
        self.location("linked-location", "shot-image", "linked-image")
        self.scene["data"]["locationId"] = "scene-location"
        self.shot["data"].update(locationId="shot-location", h3={"recipeId": REF})
        self.link("linked-location", "location")
        self.link("shot-location", "reference")
        body = self.plan()
        self.assertEqual(body["inputs"]["images"], [
            {"asset_id": "receipt-shot-image", "purpose": "reference"},
            {"asset_id": "receipt-linked-image", "purpose": "reference"}])
        self.assertNotIn("receipt-scene-image", str(body["inputs"]))

    def test_empty_shot_override_inherits_scene_and_direct_input_keeps_first_purpose(self):
        self.image("place-image")
        self.location("place", "place-image", "place-image")
        self.scene["data"]["locationId"] = "place"
        self.shot["data"].update(locationId="", h3={"inputMode": "ref"})
        self.link("place", "reference")
        self.link("place-image", "identity")
        self.assertEqual(self.plan()["inputs"]["images"], [
            {"asset_id": "receipt-place-image", "purpose": "identity"}])

    def test_ref_reports_missing_or_unsynced_location_images_and_fl_does_not_use_them(self):
        picture = self.image("place-image")
        picture["data"].pop("cloudAssetId")
        self.location("place", "place-image")
        self.scene["data"]["locationId"] = "place"
        self.shot["data"]["h3"] = {"inputMode": "ref"}
        with self.assertRaisesRegex(ValueError, "尚未同步"):
            self.plan()
        self.scene["data"]["locationId"] = "missing-place"
        with self.assertRaisesRegex(ValueError, "地点不存在"):
            self.plan()
        self.shot["data"]["h3"]["inputMode"] = "fl"
        self.assertEqual(self.plan()["inputs"]["images"], [])

    def test_malformed_legacy_location_fields_are_actionable_without_crashing(self):
        self.shot["data"]["h3"] = {"inputMode": "ref"}
        self.scene["data"]["locationId"] = ["wrong-format"]
        with self.assertRaisesRegex(ValueError, "地点绑定格式无效"):
            self.plan()
        self.scene["data"]["locationId"] = "place"
        location = self.location("place")
        location["data"]["referenceAssetIds"] = "not-a-list"
        with self.assertRaisesRegex(ValueError, "地点参考图格式无效"):
            self.plan()

    def test_location_reference_cannot_silently_become_audio_or_video_input(self):
        self.entity("sound", "audio", {"fileId": "sound-file", "cloudAssetId": "sound-receipt"})
        self.location("place", "sound")
        self.scene["data"]["locationId"] = "place"
        self.shot["data"]["h3"] = {"inputMode": "ref"}
        with self.assertRaisesRegex(ValueError, "地点参考请选择已上传的图片"):
            self.plan()

    def test_plan_defaults_follow_advertised_recipe_order(self):
        self.image("reference")
        self.location("place", "reference")
        self.scene["data"]["locationId"] = "place"
        self.caps["recipes"].reverse()
        self.assertEqual(self.plan()["recipe_id"], REF)


if __name__ == "__main__":
    unittest.main()
