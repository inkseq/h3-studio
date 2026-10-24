"""Source-bound editing and CAS regressions; no provider, network or GPU use."""

import copy
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from studio_platform.shot_segments import (
    segment_frames,
    source_binding,
    validate_segments_data,
    validate_segments_transition,
)
import test_platform_guided as guided_tests


def video(ident="clip"):
    return {
        "id": ident,
        "type": "video",
        "title": ident,
        "data": {"fileId": "local-" + ident},
    }


def shot(segments=None):
    entity = video()
    return {
        "id": "shot-one",
        "type": "shot",
        "data": {
            "seconds": 10,
            "selectedAssetId": entity["id"],
            "videoSegments": segments
            if segments is not None
            else [{"start": 1, "end": 3}],
            "videoSegmentsSource": source_binding(entity),
        },
    }


class SegmentStructureTests(unittest.TestCase):
    def test_order_repeats_overlap_and_inward_grid_are_preserved(self):
        value = shot(
            [
                {"start": 9, "end": 10},
                {"start": 1.01, "end": 3.99},
                {"start": 1.01, "end": 3.99},
                {"start": 2, "end": 3},
            ]
        )
        original = copy.deepcopy(value)
        frames = segment_frames(value, video(), 10)
        self.assertEqual(
            [(r["start_frame"], r["end_frame"], r["frames"]) for r in frames],
            [(216, 240, 24), (25, 95, 70), (25, 95, 70), (48, 72, 24)],
        )
        self.assertEqual(value, original)

    def test_invalid_ranges_and_structure_fail_closed(self):
        for segments in (
            None,
            {},
            [{"start": True, "end": 2}],
            [{"start": -1, "end": 1}],
            [{"start": 1, "end": 1}],
            [{"start": 0, "end": 0.01}],
            [{"start": 0, "end": float("inf")}],
            [{"start": 0, "end": float("nan")}],
            [{"start": 0, "end": 1, "extra": True}],
            [{"start": 0, "end": 1}] * 51,
        ):
            with self.subTest(segments=segments):
                value = shot()
                value["data"]["videoSegments"] = segments
                with self.assertRaises(ValueError):
                    validate_segments_data(value)
        value = shot()
        value["type"] = "video"
        with self.assertRaises(ValueError):
            validate_segments_data(value)

    def test_binding_all_fields_duration_and_speed_are_checked(self):
        for field in ("assetId", "fileId", "cloudAssetId", "cloudArtifactId"):
            value = shot()
            value["data"]["videoSegmentsSource"][field] = "changed"
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "source_changed"),
            ):
                segment_frames(value, video(), 10)
        with self.assertRaisesRegex(ValueError, "exceeds_source"):
            segment_frames(shot([{"start": 9, "end": 10.00001}]), video(), 10)
        value = shot()
        value["data"]["selectedVideoRange"] = {
            **source_binding(video("another")),
            "start": 0,
            "end": 10,
            "playback_rate": 2,
        }
        with self.assertRaisesRegex(ValueError, "speed_source_changed"):
            segment_frames(value, video(), 10)

    def test_omitting_ranges_is_not_clear_intent(self):
        before = {"entities": [shot(), video(), video("other")]}
        for mutation in ("omit", "rebind", "source-file"):
            after = copy.deepcopy(before)
            data = after["entities"][0]["data"]
            if mutation == "omit":
                data.pop("videoSegments")
                data.pop("videoSegmentsSource")
            elif mutation == "rebind":
                data.update(
                    selectedAssetId="other",
                    videoSegmentsSource=source_binding(video("other")),
                )
            else:
                after["entities"][1]["data"]["fileId"] = "changed"
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "clear_required"),
            ):
                validate_segments_transition(before, after)
        after = copy.deepcopy(before)
        after["entities"][0]["data"].update(selectedAssetId="other", videoSegments=[])
        after["entities"][0]["data"].pop("videoSegmentsSource")
        validate_segments_transition(before, after)


class SegmentGuidedTests(unittest.TestCase):
    setUp = guided_tests.GuidedTests.setUp
    login = guided_tests.GuidedTests.login
    setup_project = guided_tests.GuidedTests.setup_project
    edit = guided_tests.GuidedTests.edit
    key = guided_tests.GuidedTests.key

    def prepare(self):
        self.setup_project()
        response = self.edit(
            [
                {"op": "entity.create", "entity": video()},
                {"op": "entity.create", "entity": video("other")},
                {"op": "shot.select", "shot_id": "shot-one", "entity_id": "clip"},
            ]
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def action(segments=None):
        return {
            "op": "shot.segments",
            "shot_id": "shot-one",
            "segments": segments
            if segments is not None
            else [
                {"start": 1, "end": 3},
                {"start": 5, "end": 6},
                {"start": 9, "end": 10},
            ],
        }

    def source(self, _principal, project_id, entity, kind):
        self.assertEqual((project_id, kind), ("story-one", "video"))
        self.assertFalse(getattr(self, "write_lock", False))
        return {"kind": "video", "duration": 10}

    def segmented(self):
        before = self.prepare()
        with patch.object(
            self.app.state, "resolve_render_source", side_effect=self.source
        ):
            response = self.edit(
                [self.action()], before["version"], key="segments-original"
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_action_binds_before_lock_and_replays_without_source_read(self):
        before = self.prepare()
        transaction = self.app.state.repository.transaction

        @contextmanager
        def tracked_transaction():
            with transaction() as conn:
                self.write_lock = True
                try:
                    yield conn
                finally:
                    self.write_lock = False

        with (
            patch.object(self.app.state.repository, "transaction", tracked_transaction),
            patch.object(
                self.app.state, "resolve_render_source", side_effect=self.source
            ),
        ):
            response = self.edit(
                [self.action()], before["version"], key="segments-original"
            )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        data = result["project"]["entities"][2]["data"]
        self.assertEqual(data["videoSegments"], self.action()["segments"])
        self.assertEqual(data["videoSegmentsSource"], source_binding(video()))
        self.assertEqual(data["seconds"], 5)
        with patch.object(
            self.app.state,
            "resolve_render_source",
            side_effect=AssertionError("No replay source read"),
        ):
            repeated = self.edit(
                [self.action()], before["version"], key="segments-original"
            )
            conflict = self.edit(
                [self.action([])], before["version"], key="segments-original"
            )
            stale = self.edit([self.action()], before["version"], key="segments-stale")
        self.assertEqual(repeated.json(), result)
        self.assertEqual((conflict.status_code, stale.status_code), (409, 409))

    def test_switch_and_rebind_paths_require_explicit_clear_in_atomic_batch(self):
        result = self.segmented()
        cases = [
            {"op": "shot.select", "shot_id": "shot-one", "entity_id": "other"},
            {
                "op": "entity.update",
                "entity_id": "shot-one",
                "patch": {
                    "data": {
                        "selectedAssetId": "other",
                        "videoSegmentsSource": source_binding(video("other")),
                    }
                },
            },
            {
                "op": "entity.update",
                "entity_id": "clip",
                "patch": {"data": {"fileId": "replaced"}},
            },
            {
                "op": "artifact.adopt",
                "artifact_id": "new-result",
                "shot_id": "shot-one",
                "select": True,
            },
            {
                "op": "asset.attach",
                "asset_id": "new-upload",
                "shot_id": "shot-one",
                "select": True,
            },
            {
                "op": "storyboard.select",
                "artifact_id": "new-result",
                "shot_id": "shot-one",
            },
        ]
        replacement = {
            **video("replacement"),
            "data": {"cloudArtifactId": "new-result", "cloudAssetId": "new-upload"},
        }
        with (
            patch.object(self.app.state.guided, "resolve", return_value=replacement),
            patch.object(
                self.app.state.storyboard_versions, "artifact_recipe", return_value={}
            ),
            patch.object(
                self.app.state, "resolve_render_source", side_effect=self.source
            ),
        ):
            for action in cases:
                with self.subTest(op=action["op"]):
                    failed = self.edit([action], result["version"])
                    self.assertEqual(failed.status_code, 422, failed.text)
                    self.assertIn("video_segments_clear_required", failed.text)
            cleared = self.edit([self.action([]), cases[0]], result["version"])
        self.assertEqual(cleared.status_code, 200, cleared.text)
        data = cleared.json()["project"]["entities"][2]["data"]
        self.assertEqual(
            (data["videoSegments"], data["selectedAssetId"]), ([], "other")
        )
        self.assertNotIn("videoSegmentsSource", data)

    def test_clear_choose_then_segments_is_one_cas_and_failed_batch_keeps_original(
        self,
    ):
        result = self.segmented()
        choose = {"op": "shot.select", "shot_id": "shot-one", "entity_id": "other"}
        with patch.object(
            self.app.state, "resolve_render_source", side_effect=self.source
        ):
            failed = self.edit(
                [self.action([]), choose, self.action([{"start": 0, "end": 11}])],
                result["version"],
                key="rejected-replacement",
            )
            self.assertEqual(failed.status_code, 422, failed.text)
            same = self.edit(
                [{"op": "shot.select", "shot_id": "shot-one", "entity_id": "clip"}],
                result["version"],
            )
            self.assertEqual(same.status_code, 200, same.text)
            changed = self.edit(
                [self.action([]), choose, self.action([{"start": 2, "end": 3}])],
                same.json()["version"],
                key="confirmed-replacement",
            )
        self.assertEqual(changed.status_code, 200, changed.text)
        data = changed.json()["project"]["entities"][2]["data"]
        self.assertEqual(data["selectedAssetId"], "other")
        self.assertEqual(data["videoSegmentsSource"], source_binding(video("other")))
        self.assertEqual(data["videoSegments"], [{"start": 2, "end": 3}])

    def test_action_generic_and_full_save_persist_inward_grid(self):
        result = self.prepare()
        raw = [{"start": 0.01, "end": 0.99}]
        expected = [{"start": 1 / 24, "end": 23 / 24}]
        for mode in ("action", "generic", "full"):
            with patch.object(
                self.app.state, "resolve_render_source", side_effect=self.source
            ):
                if mode == "action":
                    response = self.edit([self.action(raw)], result["version"])
                elif mode == "generic":
                    response = self.edit(
                        [
                            {
                                "op": "entity.update",
                                "entity_id": "shot-one",
                                "patch": {"data": {"videoSegments": raw}},
                            }
                        ],
                        result["version"],
                    )
                else:
                    value = copy.deepcopy(result["project"])
                    value["entities"][2]["data"]["videoSegments"] = raw
                    response = self.client.put(
                        "/v1/projects/story-one",
                        json={"expected_version": result["version"], "project": value},
                    )
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(
                result["project"]["entities"][2]["data"]["videoSegments"], expected
            )
            readback = self.client.get("/v1/projects/story-one").json()
            self.assertEqual(
                readback["project"]["entities"][2]["data"]["videoSegments"], expected
            )

    def test_caption_confirmation_uses_segment_total_instead_of_generation_duration(
        self,
    ):
        result = self.segmented()
        with patch.object(
            self.app.state, "resolve_render_source", side_effect=self.source
        ):
            too_long = self.edit(
                [
                    {
                        "op": "captions.set",
                        "chapter_id": "chapter-one",
                        "cues": [
                            {"id": "cue", "start": 0, "end": 4.5, "text": "Overrun"}
                        ],
                    },
                    {
                        "op": "captions.confirm",
                        "chapter_id": "chapter-one",
                        "reviewed": True,
                    },
                ],
                result["version"],
            )
            self.assertEqual(too_long.status_code, 422, too_long.text)
            accepted = self.edit(
                [
                    {
                        "op": "captions.set",
                        "chapter_id": "chapter-one",
                        "cues": [
                            {"id": "cue", "start": 0, "end": 4, "text": "All segments"}
                        ],
                    },
                    {
                        "op": "captions.confirm",
                        "chapter_id": "chapter-one",
                        "reviewed": True,
                    },
                ],
                result["version"],
            )
        self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_full_save_rebind_omission_and_forged_duration_are_refused(self):
        result = self.segmented()
        for mode in ("omit", "rebind", "overrun"):
            value = copy.deepcopy(result["project"])
            data = value["entities"][2]["data"]
            if mode == "omit":
                data.pop("videoSegments")
                data.pop("videoSegmentsSource")
            elif mode == "rebind":
                data.update(
                    selectedAssetId="other",
                    videoSegmentsSource=source_binding(video("other")),
                )
            else:
                data["videoSegments"] = [{"start": 0, "end": 11}]
                data["seconds"] = 100
                value["entities"][3]["data"]["duration"] = 100
            with (
                self.subTest(mode=mode),
                patch.object(
                    self.app.state, "resolve_render_source", side_effect=self.source
                ),
            ):
                response = self.client.put(
                    "/v1/projects/story-one",
                    json={"expected_version": result["version"], "project": value},
                )
            self.assertEqual(response.status_code, 422, response.text)
        actual = self.client.get("/v1/projects/story-one").json()
        self.assertEqual(actual["version"], result["version"])
        self.assertEqual(actual["project"], result["project"])

    def test_malformed_full_save_refuses_before_media_reads(self):
        result = self.segmented()
        for mode in ("list", "entity", "id", "duplicate", "data", "ranges", "binding"):
            value = copy.deepcopy(result["project"])
            if mode == "list":
                value["entities"] = None
            elif mode == "entity":
                value["entities"].append(None)
            elif mode == "id":
                value["entities"][0].pop("id")
            elif mode == "duplicate":
                value["entities"].append(copy.deepcopy(value["entities"][0]))
            elif mode == "data":
                value["entities"][3]["data"] = None
            elif mode == "ranges":
                value["entities"][2]["data"]["videoSegments"] = "invalid"
            else:
                value["entities"][2]["data"]["videoSegmentsSource"]["fileId"] = "wrong"
            with (
                self.subTest(mode=mode),
                patch.object(
                    self.app.state,
                    "resolve_render_source",
                    side_effect=AssertionError("Malformed edits must not read media"),
                ),
            ):
                response = self.client.put(
                    "/v1/projects/story-one",
                    json={"expected_version": result["version"], "project": value},
                )
                self.assertEqual(response.status_code, 422, response.text)
        actual = self.client.get("/v1/projects/story-one").json()
        self.assertEqual(actual["version"], result["version"])
        self.assertEqual(actual["project"], result["project"])

    def test_generic_patch_and_action_cannot_bypass_trusted_duration(self):
        result = self.prepare()
        invalid = [{"start": 9, "end": 10.00001}]
        for action in (
            self.action(invalid),
            {
                "op": "entity.update",
                "entity_id": "shot-one",
                "patch": {
                    "data": {
                        "videoSegments": invalid,
                        "videoSegmentsSource": source_binding(video()),
                        "seconds": 100,
                    }
                },
            },
        ):
            with patch.object(
                self.app.state, "resolve_render_source", side_effect=self.source
            ):
                response = self.edit([action], result["version"])
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("video_segment_exceeds_source", response.text)
        with patch.object(
            self.app.state, "resolve_render_source", return_value={"duration": None}
        ):
            unknown = self.edit([self.action()], result["version"])
        self.assertEqual(unknown.status_code, 422, unknown.text)

    def test_cross_owner_and_missing_scope_do_not_resolve_sources(self):
        result = self.prepare()
        _, headers = self.key(scopes=["projects:read", "projects:write"])
        with patch.object(
            self.app.state,
            "resolve_render_source",
            side_effect=AssertionError("No unauthorized source read"),
        ):
            denied = self.edit([self.action()], result["version"], headers=headers)
            self.assertEqual(denied.status_code, 403, denied.text)
            self.login("supervan")
            denied = self.edit([self.action()], result["version"])
            self.assertEqual(denied.status_code, 404, denied.text)

    def test_malformed_full_save_stays_422_before_source_resolution(self):
        result = self.segmented()
        for mode in ("data-null", "entities-null", "entities-missing", "entity-scalar"):
            value = copy.deepcopy(result["project"])
            if mode == "data-null":
                value["entities"][2]["data"] = None
            elif mode == "entities-null":
                value["entities"] = None
            elif mode == "entities-missing":
                value.pop("entities")
            else:
                value["entities"][2] = "not-an-entity"
            with (
                self.subTest(mode=mode),
                patch.object(
                    self.app.state,
                    "resolve_render_source",
                    side_effect=AssertionError(
                        "Malformed input must not resolve media"
                    ),
                ),
            ):
                response = self.client.put(
                    "/v1/projects/story-one",
                    json={"expected_version": result["version"], "project": value},
                )
            self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
