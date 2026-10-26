"""Disposable ledger and CAS coverage; no live media or generation calls."""

import copy
import unittest

from sqlalchemy import select, update

from studio_platform.repository import artifacts, Scope
from studio_platform.render_plans import (
    chapter_tracks,
    compile_render,
    render_source_hash,
)
from studio_platform.project_validation import validate_project
from test_platform_generated_audio import (
    generated_story,
    trusted_source,
)
import test_platform_generated_audio as fixtures
from test_platform_render_plans import BODY


class ClipSoundApiTests(unittest.TestCase):
    setUp = fixtures.GeneratedAudioApiTests.setUp
    pair = fixtures.GeneratedAudioApiTests.pair
    bind = fixtures.GeneratedAudioApiTests.bind

    def prepare(self, *, audio=True, preference=None, track=None):
        job, pair = self.pair(audio=audio)
        self.bind(job, pair)
        self.project["entities"][2]["data"]["selectedAssetId"] = ""
        if preference is not None:
            self.project["entities"][2]["data"]["clipSound"] = preference
        if track is None:
            self.project[
                "entities"
            ].pop()  # FLAC must be adopted from its real receipt.
        else:
            self.project["entities"][-1]["data"]["mime"] = "audio/flac"
            self.track.update(track)
        self.project["journey"]["soundTracks"]["chapter-one"] = (
            [] if track is None else [self.track]
        )
        result = self.client.post("/v1/projects", json={"project": self.project})
        self.assertEqual(result.status_code, 201, result.text)
        self.version = 1
        return job, pair

    def edit(self, actions, *, version=None, key=None):
        response = self.client.post(
            "/v1/projects/story-one/actions",
            json={"expected_version": version or self.version, "actions": actions},
            headers={"Idempotency-Key": key} if key else {},
        )
        if response.status_code == 200:
            self.version = response.json()["version"]
        return response

    def select(self, **kwargs):
        return self.edit(
            [{"op": "shot.select", "shot_id": "shot-one", "entity_id": "clip"}],
            **kwargs,
        )

    def preference(self, value):
        return self.edit(
            [
                {
                    "op": "entity.update",
                    "entity_id": "shot-one",
                    "patch": {"data": {"clipSound": value}},
                }
            ]
        )

    def saved(self):
        return self.client.get("/v1/projects/story-one").json()["project"]

    def test_selection_defaults_on_adopts_real_flac_once_and_cas_is_atomic(self):
        job, pair = self.prepare()
        result = self.select(key="select-once")
        self.assertEqual(result.status_code, 200, result.text)
        project = self.saved()
        self.assertEqual(project["entities"][2]["data"]["clipSound"], "on")
        tracks = project["journey"]["soundTracks"]["chapter-one"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["generatedFrom"]["jobId"], job)
        self.assertEqual(tracks[0]["generatedFrom"]["audioArtifactId"], pair["audio"])
        self.assertNotIn("object_key", str(project))
        replay = self.select(version=1, key="select-once")
        self.assertEqual(replay.json(), result.json())
        self.assertEqual(self.select(version=1).status_code, 409)
        self.assertEqual(self.saved(), project)

    def test_explicit_off_persists_through_selection_then_on_binds(self):
        self.prepare(preference="off")
        self.assertEqual(self.select().status_code, 200)
        self.assertEqual(self.saved()["journey"]["soundTracks"]["chapter-one"], [])
        result = self.preference("on")
        self.assertEqual(result.status_code, 200, result.text)
        tracks = self.saved()["journey"]["soundTracks"]["chapter-one"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(self.preference("off").status_code, 200)
        self.assertEqual(self.saved()["journey"]["soundTracks"]["chapter-one"], tracks)
        self.assertTrue(chapter_tracks(self.saved(), "chapter-one")[0]["muted"])
        self.assertEqual(self.preference("on").status_code, 200)
        self.assertEqual(self.saved()["journey"]["soundTracks"]["chapter-one"], tracks)

    def test_old_selected_project_unrelated_actions_and_full_save_do_not_backfill(self):
        self.prepare()
        project = self.saved()
        project["entities"][2]["data"]["selectedAssetId"] = "clip"
        result = self.client.put(
            "/v1/projects/story-one", json={"project": project, "expected_version": 1}
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.version = 2
        result = self.edit(
            [
                {
                    "op": "entity.update",
                    "entity_id": "shot-one",
                    "patch": {"title": "New title"},
                }
            ]
        )
        self.assertEqual(result.status_code, 200, result.text)
        saved = self.saved()
        self.assertNotIn("clipSound", saved["entities"][2]["data"])
        self.assertEqual(saved["journey"]["soundTracks"]["chapter-one"], [])

    def test_no_flac_does_not_invent_an_audio_entity(self):
        self.prepare(audio=False)
        result = self.select()
        self.assertEqual(result.status_code, 200, result.text)
        saved = self.saved()
        self.assertEqual(saved["entities"][2]["data"]["clipSound"], "on")
        self.assertFalse(any(e["type"] == "audio" for e in saved["entities"]))

    def test_unvalidated_flac_is_not_implicitly_adopted(self):
        _, pair = self.prepare()
        with self.repo.transaction() as conn:
            row = (
                conn.execute(select(artifacts).where(artifacts.c.id == pair["audio"]))
                .mappings()
                .one()
            )
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == row["id"])
                .values(metadata={**row["metadata"], "validated": False})
            )
        self.assertEqual(self.select().status_code, 200)
        self.assertEqual(self.saved()["journey"]["soundTracks"]["chapter-one"], [])

    def test_existing_mute_review_detach_and_manual_tracks_survive(self):
        self.prepare(
            track={"muted": True, "needsReview": True, "detached": True, "shotId": ""}
        )
        before = copy.deepcopy(self.saved()["journey"]["soundTracks"])
        self.assertEqual(self.select().status_code, 200)
        self.assertEqual(self.preference("off").status_code, 200)
        self.assertEqual(self.preference("on").status_code, 200)
        self.assertEqual(self.saved()["journey"]["soundTracks"], before)

    def test_invalid_preference_refuses_whole_action_batch(self):
        self.prepare()
        before = self.saved()
        for value in (False, None, [], "auto", 1):
            result = self.preference(value)
            self.assertEqual(result.status_code, 422, result.text)
            self.assertEqual(self.saved(), before)

    def test_changed_take_preserves_existing_binding_for_explicit_confirmation(self):
        self.prepare(track={"needsReview": True})
        before = copy.deepcopy(self.saved()["journey"]["soundTracks"])
        other_job, other_pair = self.pair()
        result = self.edit(
            [
                {
                    "op": "artifact.adopt",
                    "artifact_id": other_pair["video"],
                    "shot_id": "shot-one",
                    "select": True,
                }
            ]
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.saved()["journey"]["soundTracks"], before)
        self.assertEqual(self.saved()["entities"][2]["data"]["clipSound"], "on")
        self.assertNotEqual(
            before["chapter-one"][0]["generatedFrom"]["jobId"], other_job
        )

    def test_two_shots_can_bind_the_same_take_without_duplicating_media(self):
        self.prepare()
        result = self.edit(
            [
                {"op": "shot.select", "shot_id": "shot-one", "entity_id": "clip"},
                {
                    "op": "entity.create",
                    "entity": {
                        "id": "second-shot",
                        "type": "shot",
                        "parentId": "scene-one",
                        "title": "Second shot",
                        "data": {"seconds": 2, "selectedAssetId": "clip"},
                    },
                },
            ]
        )
        self.assertEqual(result.status_code, 200, result.text)
        project = self.saved()
        self.assertEqual(len(project["journey"]["soundTracks"]["chapter-one"]), 2)
        self.assertEqual(sum(e["type"] == "audio" for e in project["entities"]), 1)

    def test_foreign_artifact_cannot_be_selected_and_implicitly_adopted(self):
        self.prepare()
        _, foreign = self.pair(Scope("sixnine", "other-owner", "story-one"))
        result = self.edit(
            [
                {
                    "op": "artifact.adopt",
                    "artifact_id": foreign["video"],
                    "shot_id": "shot-one",
                    "select": True,
                }
            ]
        )
        self.assertEqual(result.status_code, 404, result.text)
        self.assertEqual(self.saved()["journey"]["soundTracks"]["chapter-one"], [])


class ClipSoundRenderTests(unittest.TestCase):
    def test_off_is_projection_on_restores_original_and_hash_changes(self):
        project, track = generated_story()
        original = copy.deepcopy(track)
        before = render_source_hash(project, "chapter-one")
        project["entities"][2]["data"]["clipSound"] = "off"
        compiled, _, blockers, _ = compile_render(BODY, project, trusted_source)
        self.assertFalse(blockers)
        self.assertEqual(compiled["request"]["render"]["audio_tracks"], [])
        self.assertNotEqual(render_source_hash(project, "chapter-one"), before)
        self.assertEqual(track, original)
        project["entities"][2]["data"]["clipSound"] = "on"
        self.assertEqual(chapter_tracks(project, "chapter-one")[0], original)
        track["muted"] = True
        self.assertTrue(chapter_tracks(project, "chapter-one")[0]["muted"])

    def test_manual_and_detached_audio_ignore_shot_preference(self):
        project, track = generated_story()
        project["entities"][2]["data"]["clipSound"] = "off"
        track["detached"] = True
        self.assertFalse(chapter_tracks(project, "chapter-one")[0]["muted"])
        track.pop("generatedFrom")
        self.assertFalse(chapter_tracks(project, "chapter-one")[0]["muted"])

    def test_non_shot_preference_is_invalid(self):
        project, _ = generated_story()
        project["entities"][0]["data"]["clipSound"] = "on"
        with self.assertRaisesRegex(ValueError, "clip_sound_invalid"):
            validate_project(project)
