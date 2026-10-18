"""Location deletion through atomic HTTP actions on disposable CPU-only state."""
import copy
import unittest

from studio_platform.project_validation import validate_project
from studio_platform.source_snapshot import source_snapshot
import test_platform_api as fixtures


class GuidedLocationDeleteTests(unittest.TestCase):
    login = fixtures.ApiTests.login

    def setUp(self):
        fixtures.ApiTests.setUp(self)
        self.login()
        self.document = fixtures.project()
        scene = self.document["entities"][1]
        scene["data"]["locationId"] = "station"

        def add(ident, kind, data=None, parent=None):
            self.document["entities"].append(dict(id=ident, type=kind, parentId=parent,
                title=ident, description="", version=1, order=0, status="ready", data=data or {}))

        add("shot-room", "shot", {"seconds": 5, "locationId": "room"}, "scene-one")
        add("shot-station", "shot", {"seconds": 5, "locationId": "station"}, "scene-one")
        add("station", "location", {"referenceAssetIds": ["shared-image", "station-image"]})
        add("room", "location", {"referenceAssetIds": ["shared-image", "room-image"]})
        for ident in ("shared-image", "station-image", "room-image", "unrelated-image"):
            add(ident, "image", {"fileId": ident+"-file"})
        add("unrelated-note", "note", {"source": "retain"})
        self.document["links"] = [{"id": "location-edge", "source": "station",
            "target": "shot-one", "role": "location"}]
        validate_project(self.document)
        response = self.client.post("/v1/projects", json={"project": self.document})
        self.assertEqual(response.status_code, 201, response.text)

    def saved(self):
        return self.client.get("/v1/projects/story-one").json()

    def edit(self, actions, key=None):
        return self.client.post("/v1/projects/story-one/actions",
            json={"expected_version": 1, "actions": actions},
            headers={"Idempotency-Key": key} if key else {})

    def entities(self, document):
        return {entity["id"]: entity for entity in document["entities"]}

    def test_deleting_scene_default_location_clears_all_matching_bindings_and_keeps_other_entities(self):
        before = self.entities(self.document)
        response = self.edit([{"op": "entity.delete", "entity_id": "station"}])
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()["project"]
        validate_project(after)
        entities = self.entities(after)
        self.assertNotIn("station", entities)
        for ident in ("scene-one", "shot-station"):
            self.assertEqual(entities[ident]["data"]["locationId"], "")
            self.assertEqual(entities[ident]["version"], before[ident]["version"]+1)
            self.assertEqual(entities[ident]["status"], "review")
        self.assertEqual(after["links"], [])
        for ident in set(before)-{"station", "scene-one", "shot-station"}:
            self.assertEqual(entities[ident], before[ident])
        self.assertNotEqual(source_snapshot(self.document, "shot-one"), source_snapshot(after, "shot-one"))

    def test_deleting_shot_override_restores_scene_inheritance_without_removing_its_images(self):
        before = self.entities(self.document)
        response = self.client.delete("/v1/projects/story-one/entities/room?expected_version=1")
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()["project"]
        validate_project(after)
        entities = self.entities(after)
        self.assertNotIn("room", entities)
        self.assertEqual(entities["shot-room"]["data"]["locationId"], "")
        self.assertEqual(entities["scene-one"]["data"]["locationId"], "station")
        for ident in set(before)-{"room", "shot-room"}:
            self.assertEqual(entities[ident], before[ident])
        self.assertNotEqual(source_snapshot(self.document, "shot-room"), source_snapshot(after, "shot-room"))

    def test_deleting_shared_location_image_prunes_every_gallery_but_preserves_other_images_and_bindings(self):
        before = self.entities(self.document)
        response = self.edit([{"op": "entity.delete", "entity_id": "shared-image"}], key="delete-shared-image")
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()["project"]
        validate_project(after)
        entities = self.entities(after)
        self.assertNotIn("shared-image", entities)
        for ident, remaining in (("station", "station-image"), ("room", "room-image")):
            self.assertEqual(entities[ident]["data"]["referenceAssetIds"], [remaining])
            self.assertEqual(entities[ident]["version"], before[ident]["version"]+1)
            self.assertEqual(entities[ident]["status"], "review")
        for ident in set(before)-{"shared-image", "station", "room"}:
            self.assertEqual(entities[ident], before[ident])
        # An exact retry returns the original edit receipt, without a second
        # version increment or replacing any surviving image reference.
        retry = self.edit([{"op": "entity.delete", "entity_id": "shared-image"}], key="delete-shared-image")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json(), response.json())
        self.assertEqual(self.saved()["version"], 2)

    def test_deleting_unrelated_entity_does_not_rewrite_location_data_or_entity_versions(self):
        before = self.entities(self.document)
        response = self.edit([{"op": "entity.delete", "entity_id": "unrelated-note"}])
        self.assertEqual(response.status_code, 200, response.text)
        after = response.json()["project"]
        validate_project(after)
        self.assertEqual(self.entities(after), {ident: entity for ident, entity in before.items()
            if ident != "unrelated-note"})
        self.assertEqual(source_snapshot(self.document, "shot-one"), source_snapshot(after, "shot-one"))

    def test_later_invalid_action_rolls_back_deleted_entities_and_pruned_bindings_atomically(self):
        before = copy.deepcopy(self.saved())
        response = self.edit([
            {"op": "entity.delete", "entity_id": "station"},
            {"op": "entity.delete", "entity_id": "shared-image"},
            {"op": "entity.create", "entity": {"id": "bad-scene", "type": "scene", "parentId": "missing"}},
        ], key="failed-atomic-deletion")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.saved(), before)
        # The rejected request must not consume its edit receipt/key.
        corrected = self.edit([{"op": "entity.delete", "entity_id": "station"}], key="failed-atomic-deletion")
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertEqual(corrected.json()["version"], before["version"]+1)


if __name__ == "__main__":
    unittest.main()
