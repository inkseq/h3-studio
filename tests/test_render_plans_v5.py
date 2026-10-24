"""V5 authoring-to-worker contract; no provider, storage or process calls."""

import copy
import unittest

from test_platform_render_plans import BODY, source, story
from studio_platform.caption_server import caption_signature
from studio_platform.film_runs import frozen_story
from studio_platform.project_validation import validate_project
from studio_platform.render_backend import validate_render_request
from studio_platform.render_plans import (
    compile_film_export,
    compile_render,
    configuration_for,
    render_source_hash,
    validate_render_source,
)
from studio_platform.render_timing import RATES, shot_frames
from studio_platform.repository import request_hash
from studio_platform.shot_segments import source_binding


def fixture():
    project = story()
    seed = project["entities"][2]
    project["entities"] = project["entities"][:2]
    sources = {}
    tracks = []
    for index, (name, seconds, rate) in enumerate(
        (("c", 3, 0.75), ("a", 2, 2), ("d", 2, 0.5), ("b", 2, 1))
    ):
        shot = copy.deepcopy(seed)
        video_id, audio_id, job = name + "-video", name + "-audio", "job-" + name
        shot.update(id=name, title=name, order=index)
        shot["data"].update(
            seconds=seconds,
            selectedAssetId=video_id,
            selectedVideoRange={
                "assetId": video_id,
                "fileId": "file-" + video_id,
                "cloudAssetId": None,
                "cloudArtifactId": "artifact-" + video_id,
                "start": 0,
                "end": seconds,
                "playback_rate": rate,
            },
        )
        project["entities"].append(shot)
        for kind, ident in (("video", video_id), ("audio", audio_id)):
            entity = {
                "id": ident,
                "type": kind,
                "title": ident,
                "version": 1,
                "order": 0,
                "parentId": None,
                "description": "",
                "status": "ready",
                "data": {
                    "fileId": "file-" + ident,
                    "cloudArtifactId": "artifact-" + ident,
                },
            }
            project["entities"].append(entity)
            sources["artifact-" + ident] = {
                "kind": kind,
                "duration": seconds,
                "source_job_id": job,
                "artifact_id": "artifact-" + ident,
                "simulation": True,
                "object": {
                    "key": "owners/test/" + ident,
                    "sha256": "a" * 64,
                    "size_bytes": 1000,
                    "content_type": "video/mp4" if kind == "video" else "audio/flac",
                },
            }
        tracks.append(
            {
                "id": "sound-" + name,
                "assetId": audio_id,
                "fileId": "file-" + audio_id,
                "shotId": name,
                "offset": 0,
                "start": 0,
                "end": seconds,
                "gain": 1,
                "muted": False,
                "generatedFrom": {
                    "jobId": job,
                    "videoEntityId": video_id,
                    "videoArtifactId": "artifact-" + video_id,
                    "audioArtifactId": "artifact-" + audio_id,
                },
            }
        )
    tracks[2].update(detached=True, shotId="", offset=5)
    music = {
        "id": "music",
        "type": "audio",
        "title": "music",
        "version": 1,
        "order": 0,
        "parentId": None,
        "description": "",
        "status": "ready",
        "data": {"fileId": "music-file", "cloudArtifactId": "music-artifact"},
    }
    project["entities"].append(music)
    sources["music-artifact"] = {
        "kind": "audio",
        "duration": 9,
        "source_job_id": None,
        "artifact_id": "music-artifact",
        "simulation": True,
        "object": {
            "key": "owners/test/music.flac",
            "sha256": "b" * 64,
            "size_bytes": 1000,
            "content_type": "audio/flac",
        },
    }
    tracks.append(
        {
            "id": "music-track",
            "assetId": "music",
            "fileId": "music-file",
            "offset": 0,
            "start": 0,
            "end": 9,
            "gain": 0.4,
            "fade_in_s": 1,
            "fade_out_s": 2,
        }
    )
    project["journey"].update(
        sound={"mode": "mixed"},
        soundTracks={"chapter-one": tracks},
        filmCuts={
            "chapter-one": {
                "shotIds": ["d", "c", "a", "c", "unknown"],
                "clip_sound_crossfade_s": 0.5,
            }
        },
    )
    calls = []

    def resolve(entity, kind):
        calls.append((entity["id"], kind))
        value = copy.deepcopy(sources[entity["data"]["cloudArtifactId"]])
        if value["kind"] != kind:
            raise ValueError("wrong media kind")
        return value

    return project, sources, calls, resolve


class PlannerV5(unittest.TestCase):
    @staticmethod
    def segments(project, ranges, shot_id="c"):
        shot = next(item for item in project["entities"] if item["id"] == shot_id)
        entity = next(
            item
            for item in project["entities"]
            if item["id"] == shot["data"]["selectedAssetId"]
        )
        shot["data"].update(
            videoSegments=[{"start": start, "end": end} for start, end in ranges],
            videoSegmentsSource=source_binding(entity),
        )
        return shot

    def test_segments_keep_user_order_speed_pairing_and_once_only_manual_audio(self):
        project, _, calls, resolve = fixture()
        shot = self.segments(project, [(2, 3), (0, 1), (2, 3)])
        before = copy.deepcopy(project)
        compiled, _, blockers, _ = compile_render(BODY, project, resolve)
        self.assertEqual(blockers, [])
        render = compiled["request"]["render"]
        self.assertEqual(
            [
                (s["shot_id"], s["source_start_frame"], s["frames"])
                for s in render["shots"]
            ],
            [("c", 48, 32), ("c", 0, 32), ("c", 48, 32), ("a", 0, 24), ("d", 0, 96)],
        )
        self.assertEqual(shot_frames(shot), (72, 96, 0.75))
        tracks = render["audio_tracks"]
        self.assertEqual(
            [
                (
                    t["source_start_sample"],
                    t["source_end_sample"],
                    t["timeline_start_sample"],
                    t["output_samples"],
                )
                for t in tracks[:3]
            ],
            [
                (64000, 96000, 0, 42667),
                (0, 32000, 42667, 42666),
                (64000, 96000, 85333, 42667),
            ],
        )
        self.assertEqual(
            [t["source_id"] for t in tracks],
            ["c-audio"] * 3 + ["a-audio", "d-audio", "music"],
        )
        self.assertEqual(tracks[4]["timeline_start_sample"], 160000)
        self.assertEqual(tracks[5]["output_samples"], 288000)
        self.assertEqual(calls.count(("c-video", "video")), 1)
        self.assertEqual(calls.count(("c-audio", "audio")), 1)
        self.assertEqual(compiled["output_spec"]["frame_count"], 216)
        self.assertEqual(project, before)
        validate_render_request(compiled)

    def test_segment_frame_rounding_is_per_clip_and_overrides_single_trim(self):
        project, _, _, resolve = fixture()
        shot = self.segments(project, [(0, 1 / 24), (2, 2 + 1 / 24)])
        shot["data"]["selectedVideoRange"]["playback_rate"] = 2
        project["journey"]["sound"] = {"mode": "silent"}
        project["journey"]["filmCuts"]["chapter-one"]["shotIds"] = ["c"]
        compiled, _, blockers, _ = compile_render(BODY, project, resolve)
        self.assertEqual(blockers, [])
        self.assertEqual(shot_frames(shot), (2, 2, 2))
        self.assertEqual(compiled["output_spec"]["frame_count"], 2)
        self.assertEqual(shot["data"]["seconds"], 3)

    def test_expanded_video_and_audio_limits_refuse_without_truncating_valid_plan(self):
        for count, expected in ((50, "最多50个片段"), (33, "最多32条声音轨道")):
            project, _, _, resolve = fixture()
            self.segments(project, [(0, 1 / 24)] * count)
            compiled, _, blockers, _ = compile_render(BODY, project, resolve)
            self.assertTrue(any(expected in message for message in blockers), blockers)
            self.assertLessEqual(len(compiled["request"]["render"]["shots"]), 50)
            self.assertLessEqual(len(compiled["request"]["render"]["audio_tracks"]), 32)

    def test_segments_invalidate_equal_length_caption_and_source_and_refuse_stale_binding(
        self,
    ):
        project, _, _, resolve = fixture()
        shot = self.segments(project, [(0, 1), (2, 3)])
        shots = [shot]
        before = caption_signature(project, "chapter-one", shots)
        digest = render_source_hash(project, "chapter-one")
        shot["data"]["videoSegments"].reverse()
        after = caption_signature(project, "chapter-one", shots)
        self.assertEqual(before["basis"][0]["end"], after["basis"][0]["end"])
        self.assertNotEqual(before, after)
        self.assertNotEqual(digest, render_source_hash(project, "chapter-one"))
        shot["data"]["videoSegmentsSource"]["fileId"] = "replaced-file"
        self.assertTrue(compile_render(BODY, project, resolve)[2])

    def test_no_segments_keep_exact_v5_request_and_empty_edits_do_not_enable_v5(self):
        project, _, _, resolve = fixture()
        baseline = compile_render(BODY, project, resolve)[0]
        for shot in project["entities"]:
            if shot["type"] == "shot":
                shot["data"]["videoSegments"] = []
        actual = compile_render(BODY, project, resolve)[0]
        self.assertEqual(actual["request"], baseline["request"])
        self.assertEqual(actual["sources"], baseline["sources"])
        project = story()
        project["entities"][2]["data"]["videoSegments"] = []
        self.assertEqual(
            compile_render(BODY, project, source)[0]["request"]["render"]["version"], 3
        )

    def test_legacy_request_hashes_are_exactly_the_6726_baseline(self):
        project = story()
        compiled = compile_render(BODY, project, source)[0]
        self.assertEqual(
            request_hash(compiled),
            "ca258e4adc255b57eb2d8b76690e8ad147afdc4c549ca2f9d560efc102a930c2",
        )
        project["entities"][2]["data"]["selectedVideoRange"] = {
            "assetId": "clip",
            "fileId": "local-clip",
            "cloudAssetId": "asset-one",
            "cloudArtifactId": None,
            "start": 1,
            "end": 4,
        }
        compiled = compile_render(BODY, project, source)[0]
        self.assertEqual(
            request_hash(compiled),
            "f1be6b7c7e3b27210db62e87ce23f0201559b26c3ac0267e5a332cf49e09ea98",
        )
        joined = compile_film_export(
            "story-one",
            "run-one",
            [
                {
                    "chapter_id": "chapter-one",
                    "output_spec": compiled["output_spec"],
                    "video": compiled["sources"]["clip"],
                    "audio": None,
                }
            ],
        )
        self.assertEqual(
            request_hash(joined),
            "70a876d5693622d171a91a56907741ef2ae3cb4b92d0e75a280b88f6c9c7a4d6",
        )

    def test_combined_fixture_normalizes_exact_frames_samples_and_selection(self):
        project, _, calls, resolve = fixture()
        validate_project(project)
        compiled, display, blockers, _ = compile_render(BODY, project, resolve)
        self.assertEqual(blockers, [])
        render = compiled["request"]["render"]
        self.assertEqual(
            (render["version"], render["stage"], configuration_for(compiled)),
            (5, "chapter", "cpu-render-v5"),
        )
        self.assertEqual(
            [(s["shot_id"], s["source_frames"], s["frames"]) for s in render["shots"]],
            [("c", 72, 96), ("a", 48, 24), ("d", 48, 96)],
        )
        self.assertEqual(compiled["output_spec"]["frame_count"], 216)
        self.assertEqual(display["duration"], 9)
        self.assertNotIn(("b-video", "video"), calls)
        self.assertNotIn(("b-audio", "audio"), calls)
        c, a, d, music = render["audio_tracks"]
        self.assertEqual((c["fade_out_samples"], a["fade_in_samples"]), (8000, 8000))
        self.assertEqual(
            (d["timeline_start_sample"], d["output_samples"], d["playback_rate"]),
            (160000, 64000, 1),
        )
        self.assertEqual(
            (music["fade_in_samples"], music["fade_out_samples"]), (32000, 64000)
        )
        validate_render_request(compiled)
        self.assertTrue(validate_render_source(project, compiled))

    def test_every_rate_exact_rational_and_legacy_duration(self):
        for rate in RATES:
            project, _, _, _ = fixture()
            shot = next(s for s in project["entities"] if s["id"] == "c")
            shot["data"]["selectedVideoRange"]["playback_rate"] = rate
            self.assertEqual(shot_frames(shot), (72, int(72 / rate + 0.5), rate))
        shot["data"]["selectedVideoRange"].pop("playback_rate")
        shot["data"]["seconds"] = 2
        self.assertEqual(shot_frames(shot), (48, 48, 1))

    def test_slow_sound_default_and_override_do_not_read_muted_source(self):
        project, _, calls, resolve = fixture()
        project["journey"]["filmCuts"]["chapter-one"]["shotIds"] = ["d"]
        track = project["journey"]["soundTracks"]["chapter-one"][2]
        track.update(detached=False, shotId="d", offset=0)
        project["journey"]["soundTracks"]["chapter-one"] = [track]
        compiled, display, blockers, _ = compile_render(BODY, project, resolve)
        self.assertEqual(blockers, [])
        self.assertEqual(compiled["request"]["render"]["audio_tracks"], [])
        self.assertTrue(display["audio_tracks"][0]["effective_muted"])
        self.assertNotIn(("d-audio", "audio"), calls)
        track["slow_rate_audio_enabled"] = True
        compiled, _, blockers, _ = compile_render(BODY, project, resolve)
        self.assertEqual(blockers, [])
        self.assertEqual(
            compiled["request"]["render"]["audio_tracks"][0]["playback_rate"], 0.5
        )

    def test_detached_proves_original_job_without_including_original_video_bytes(self):
        project, sources, calls, resolve = fixture()
        original = next(e for e in project["entities"] if e["id"] == "d-video")
        original["id"] = "new-d-video"
        original["data"].update(fileId="new-file", cloudArtifactId="new-artifact")
        sources["new-artifact"] = {
            **copy.deepcopy(sources["artifact-d-video"]),
            "artifact_id": "new-artifact",
            "source_job_id": "new-job",
        }
        shot = next(e for e in project["entities"] if e["id"] == "d")
        shot["data"]["selectedAssetId"] = "new-d-video"
        shot["data"]["selectedVideoRange"].update(
            assetId="new-d-video", fileId="new-file", cloudArtifactId="new-artifact"
        )
        compiled, _, blockers, _ = compile_render(BODY, project, resolve)
        self.assertEqual(blockers, [])
        self.assertIn(("d-video", "video"), calls)
        self.assertNotIn("d-video", compiled["sources"])
        sources["artifact-d-video"]["source_job_id"] = "foreign-job"
        self.assertTrue(compile_render(BODY, project, resolve)[2])

    def test_edits_invalidate_source_and_caption_consent_and_reject_bad_envelopes(self):
        project, _, _, resolve = fixture()
        shots = [e for e in project["entities"] if e["id"] in {"c", "a", "d"}]
        before, caption = (
            render_source_hash(project, "chapter-one"),
            caption_signature(project, "chapter-one", shots),
        )
        project["journey"]["filmCuts"]["chapter-one"]["clip_sound_crossfade_s"] = 1
        self.assertNotEqual(render_source_hash(project, "chapter-one"), before)
        self.assertNotEqual(caption_signature(project, "chapter-one", shots), caption)
        project["journey"]["soundTracks"]["chapter-one"][0].update(
            fade_in_s=3, fade_out_s=2
        )
        self.assertTrue(compile_render(BODY, project, resolve)[2])

    def test_legacy_caption_context_does_not_reinterpret_ignored_future_track_fields(
        self,
    ):
        project, _, _, _ = fixture()
        shots = [e for e in project["entities"] if e["id"] in {"c", "a", "d"}]
        baseline = caption_signature(project, "chapter-one", shots, render_version=3)
        project["journey"]["soundTracks"]["chapter-one"][0].update(
            fade_in_s=1, gain=0.3, slow_rate_audio_enabled=True
        )
        self.assertEqual(
            caption_signature(project, "chapter-one", shots, render_version=3), baseline
        )
        self.assertNotEqual(
            caption_signature(project, "chapter-one", shots, render_version=5), baseline
        )

    def test_strict_new_fields_reject_bool_nan_and_detached_tail(self):
        for value in (True, 0, 3, float("nan")):
            project, _, _, _ = fixture()
            next(e for e in project["entities"] if e["id"] == "c")["data"][
                "selectedVideoRange"
            ]["playback_rate"] = value
            with self.subTest(rate=value), self.assertRaises(ValueError):
                validate_project(project)
        project, _, _, resolve = fixture()
        project["journey"]["soundTracks"]["chapter-one"][2]["offset"] = 8
        self.assertTrue(compile_render(BODY, project, resolve)[2])

    def test_frozen_film_keeps_crossfade_and_v5_concat_uses_integer_spans(self):
        project, _, _, resolve = fixture()
        frozen = frozen_story(
            project,
            {},
            chapters=[
                {
                    "chapter_id": "chapter-one",
                    "shot_ids": ["c", "a", "d"],
                    "clip_sound_crossfade_s": 1,
                }
            ],
        )
        self.assertEqual(
            frozen["journey"]["filmCuts"]["chapter-one"]["clip_sound_crossfade_s"], 1
        )
        compiled = compile_render(BODY, project, resolve)[0]
        video = copy.deepcopy(compiled["sources"]["c-video"])
        audio = copy.deepcopy(compiled["sources"]["c-audio"])
        result = compile_film_export(
            "story-one",
            "run-one",
            [
                {
                    "chapter_id": "chapter-one",
                    "output_spec": compiled["output_spec"],
                    "video": video,
                    "audio": audio,
                    "render_version": 5,
                }
            ],
        )
        self.assertEqual(
            (
                result["request"]["render"]["version"],
                result["request"]["render"]["stage"],
            ),
            (5, "film_concat"),
        )
        self.assertEqual(
            result["request"]["render"]["audio_tracks"][0]["output_samples"], 288000
        )
        self.assertEqual(configuration_for(result), "cpu-render-v5")


if __name__ == "__main__":
    unittest.main()
