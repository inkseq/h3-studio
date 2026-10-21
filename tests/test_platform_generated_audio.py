"""Generated audio bindings are editing intent, never artifact authorization."""

import copy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from studio_platform.api import create_app
from studio_platform.queue import TaskQueue
from studio_platform.repository import Scope, artifacts, jobs
from studio_platform.settings import Settings
from studio_platform.project_validation import validate_project
from studio_platform.render_plans import (
    compile_render,
    frame_sample,
    render_source_hash,
)
from studio_platform.render_backend import validate_render_request
from test_platform_render_plans import BODY, source, story


def generated_story():
    p = story()
    video = p["entities"][-1]
    video["data"].update(
        cloudAssetId=None, cloudArtifactId="artifact-video", sourceJobId="job-one"
    )
    audio = copy.deepcopy(video)
    audio.update(id="voice", type="audio", title="Generated voice")
    audio["data"].update(fileId="local-voice", cloudArtifactId="artifact-audio")
    p["entities"].append(audio)
    track = {
        "assetId": "voice",
        "fileId": "local-voice",
        "shotId": "shot-one",
        "offset": 0,
        "start": 0,
        "end": 2,
        "gain": 0.7,
        "role": "generated",
        "muted": False,
        "generatedFrom": {
            "jobId": "job-one",
            "videoEntityId": "clip",
            "videoArtifactId": "artifact-video",
            "audioArtifactId": "artifact-audio",
        },
    }
    p["journey"]["sound"]["mode"] = "mixed"
    p["journey"]["soundTracks"]["chapter-one"] = [track]
    return p, track


def trusted_source(entity, kind):
    result = source(entity, kind)
    result.update(
        source_job_id="job-one",
        artifact_id="artifact-" + ("video" if kind == "video" else "audio"),
    )
    result["object"]["content_type"] = "video/mp4" if kind == "video" else "audio/flac"
    return result


class GeneratedAudioTests(unittest.TestCase):
    def test_known_excluded_missing_generated_audio_is_not_resolved_or_hashed(self):
        from studio_platform.caption_server import caption_signature
        from studio_platform.render_plans import ordered_chapter

        p, track = generated_story()
        included = copy.deepcopy(p["entities"][2])
        included.update(id="included", order=1)
        p["entities"].append(included)
        p["journey"]["filmCuts"] = {"chapter-one": {"shotIds": ["included"]}}
        voice = next(entity for entity in p["entities"] if entity["id"] == "voice")
        voice["data"]["missingFile"] = True
        music = copy.deepcopy(voice)
        music.update(id="music", title="Independent music")
        music["data"] = {"fileId": "local-music"}
        p["entities"].append(music)
        p["journey"]["soundTracks"]["chapter-one"].append(
            {
                "assetId": "music",
                "fileId": "local-music",
                "shotId": "",
                "start": 0,
                "end": 2,
                "offset": 0,
                "gain": 0.5,
            }
        )
        calls = []

        def resolver(entity, kind):
            calls.append(entity["id"])
            self.assertNotEqual(entity["id"], "voice")
            return trusted_source(entity, kind)

        compiled, _, blockers, _ = compile_render(BODY, p, resolver)
        self.assertFalse(blockers)
        self.assertEqual(set(calls), {"clip", "music"})
        self.assertEqual(set(compiled["sources"]), {"clip", "music"})
        self.assertEqual(
            [
                row["assetId"]
                for row in caption_signature(
                    p, "chapter-one", ordered_chapter(p, "chapter-one")[2]
                )["audio"]
            ],
            ["music"],
        )
        before = render_source_hash(p, "chapter-one", burn_subtitles=True)
        track["generatedFrom"] = {"unread": "changed"}
        voice["data"]["fileId"] = "different-excluded-audio"
        self.assertEqual(
            render_source_hash(p, "chapter-one", burn_subtitles=True), before
        )
        foreign_chapter = {**copy.deepcopy(p["entities"][0]), "id": "foreign-chapter"}
        foreign_scene = {
            **copy.deepcopy(p["entities"][1]),
            "id": "foreign-scene",
            "parentId": "foreign-chapter",
        }
        p["entities"].extend(
            [
                foreign_chapter,
                foreign_scene,
                {
                    **copy.deepcopy(included),
                    "id": "foreign",
                    "parentId": "foreign-scene",
                },
            ]
        )
        for anchor in ("unknown", "foreign"):
            with self.subTest(anchor=anchor):
                track["shotId"] = anchor
                track.pop("generatedFrom", None)
                self.assertTrue(compile_render(BODY, p, trusted_source)[2])

    def test_same_job_flac_follows_live_cut_instead_of_projected_audio_fields(self):
        p, track = generated_story()
        shot = p["entities"][2]
        shot["data"].update(
            seconds=3,
            selectedVideoRange={
                "assetId": "clip",
                "fileId": "local-clip",
                "cloudAssetId": None,
                "cloudArtifactId": "artifact-video",
                "start": 2,
                "end": 5,
            },
        )
        track.update(start=-999, end=999)  # UI projection is not execution authority.
        compiled, timeline, blockers, _ = compile_render(BODY, p, trusted_source)
        self.assertFalse(blockers)
        normalized = compiled["request"]["render"]["audio_tracks"]
        self.assertEqual(
            normalized,
            [
                {
                    "source_id": "voice",
                    "timeline_start": 0,
                    "source_start": 2,
                    "source_end": 5,
                    "gain": 0.7,
                }
            ],
        )
        self.assertEqual(
            timeline["audio_tracks"][0]["generated_from"], track["generatedFrom"]
        )
        self.assertNotIn("generatedFrom", str(compiled["sources"]))
        validate_render_request(compiled)

    def test_fractional_shots_use_absolute_frame_boundaries_without_accumulation(self):
        p, track = generated_story()
        p["entities"][2]["data"]["seconds"] = 0.126  # three frames
        tracks = p["journey"]["soundTracks"]["chapter-one"]
        for index in range(1, 29):
            shot = copy.deepcopy(p["entities"][2])
            shot.update(id=f"shot-{index}", order=index)
            shot["data"]["seconds"] = 1 / 24 if index % 2 else 2 / 24
            p["entities"].append(shot)
            tracks.append({**copy.deepcopy(track), "shotId": shot["id"]})
        compiled, _, blockers, _ = compile_render(BODY, p, trusted_source)
        self.assertFalse(blockers)
        validate_render_request(compiled)
        cursor = 0
        for shot, audio in zip(
            compiled["request"]["render"]["shots"],
            compiled["request"]["render"]["audio_tracks"],
        ):
            self.assertEqual(
                round(audio["timeline_start"] * 32000), frame_sample(cursor)
            )
            self.assertLessEqual(
                abs(audio["timeline_start"] * 32000 - cursor * 32000 / 24), 0.500001
            )
            cursor += shot["frames"]
        self.assertEqual(frame_sample(14400), 19_200_000)

    def test_missing_wrong_or_uploaded_pair_cannot_impersonate_same_job(self):
        mutations = (
            lambda s, k: s.update(source_job_id="other-job") if k == "audio" else None,
            lambda s, k: s.update(source_job_id=None, artifact_id=None),
            lambda s, k: (
                s.update(artifact_id="another-artifact") if k == "audio" else None
            ),
            lambda s, k: (
                s["object"].update(content_type="audio/wav") if k == "audio" else None
            ),
        )
        for mutate in mutations:
            p, _ = generated_story()

            def resolver(e, k):
                value = trusted_source(e, k)
                mutate(value, k)
                return value

            self.assertTrue(compile_render(BODY, p, resolver)[2])
        p, _ = generated_story()
        p["entities"] = [e for e in p["entities"] if e["type"] != "audio"]
        validate_project(p)  # Recoverable draft, not a renderable track.
        self.assertTrue(compile_render(BODY, p, trusted_source)[2])

    def test_source_swap_delete_or_review_blocks_until_mute_and_undo_recovers(self):
        def swap(p, t):
            replacement = copy.deepcopy(p["entities"][3])
            replacement["id"] = "new-clip"
            p["entities"].append(replacement)
            p["entities"][2]["data"]["selectedAssetId"] = "new-clip"

        def delete(p, t):
            p["entities"] = [e for e in p["entities"] if e["id"] != "clip"]
            p["entities"][2]["data"]["selectedAssetId"] = None

        for mutate in (
            swap,
            delete,
            lambda p, t: t.update(needsReview=True),
            lambda p, t: t["generatedFrom"].update(videoArtifactId="replaced"),
        ):
            p, track = generated_story()
            original = copy.deepcopy(p)
            mutate(p, track)
            validate_project(p)
            self.assertTrue(compile_render(BODY, p, trusted_source)[2])
            self.assertFalse(compile_render(BODY, original, trusted_source)[2])
        p, track = generated_story()
        track.update(needsReview=True, muted=True)
        # Muted stale binding is ignored when another independent track exists.
        p["journey"]["soundTracks"]["chapter-one"].append(
            {
                "assetId": "voice",
                "fileId": "local-voice",
                "start": 0,
                "end": 2,
                "gain": 0.2,
            }
        )
        self.assertFalse(compile_render(BODY, p, trusted_source)[2])

    def test_short_source_duplicate_track_offset_and_limit_are_blocked(self):
        p, track = generated_story()

        def short(e, k):
            value = trusted_source(e, k)
            if k == "audio":
                value["duration"] = 2 - 1 / 32000
            return value

        self.assertTrue(any("短于" in b for b in compile_render(BODY, p, short)[2]))
        for mutate in (
            lambda t, p: t.update(offset=0.1),
            lambda t, p: p["journey"]["soundTracks"]["chapter-one"].append(
                copy.deepcopy(t)
            ),
            lambda t, p: p["journey"]["soundTracks"]["chapter-one"].__imul__(33),
        ):
            p, track = generated_story()
            mutate(track, p)
            self.assertTrue(compile_render(BODY, p, trusted_source)[2])

    def test_silent_ignores_missing_audio_and_no_audio_extraction(self):
        p, track = generated_story()
        track["assetId"] = "missing-flac"
        p["journey"]["sound"]["mode"] = "silent"
        compiled, _, blockers, _ = compile_render(BODY, p, trusted_source)
        self.assertFalse(blockers)
        self.assertFalse(compiled["request"]["generate_audio"])
        self.assertEqual(set(compiled["sources"]), {"clip"})

    def test_strict_binding_structure_and_source_hash(self):
        for mutate in (
            lambda t: t["generatedFrom"].update(jobId=[]),
            lambda t: t["generatedFrom"].update(url="https://invalid.test"),
            lambda t: t["generatedFrom"].pop("audioArtifactId"),
            lambda t: t.update(shotId="../escape"),
            lambda t: t.update(needsReview="false"),
        ):
            p, track = generated_story()
            mutate(track)
            with self.assertRaises(ValueError):
                validate_project(p)
        p, track = generated_story()
        before = render_source_hash(p, "chapter-one")
        track["generatedFrom"]["audioArtifactId"] = "new-audio"
        self.assertNotEqual(before, render_source_hash(p, "chapter-one"))

    def test_explicit_detach_preserves_independent_track_semantics(self):
        p, track = generated_story()
        track.pop("generatedFrom")
        track.update(start=1, end=3, offset=0.5)
        compiled, _, blockers, warnings = compile_render(BODY, p, trusted_source)
        self.assertFalse(blockers)
        self.assertEqual(
            compiled["request"]["render"]["audio_tracks"][0],
            {
                "source_id": "voice",
                "timeline_start": 0.5,
                "source_start": 1,
                "source_end": 2.5,
                "gain": 0.7,
            },
        )
        self.assertTrue(warnings)


class GeneratedAudioApiTests(unittest.TestCase):
    """Trusted collector ledger fixtures; no mocked ownership/resolver checks."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = create_app(
            Settings(Path(tmp.name), auth_mode="local-test", render_enabled=True)
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post(
            "/api/auth/login", json={"username": "superdan"}
        ).raise_for_status()
        self.repo = self.app.state.repository
        self.queue = TaskQueue(self.repo)
        self.scope = Scope("sixnine", "superdan", "story-one")
        self.project, self.track = generated_story()
        self.version = 0
        self.capacity = patch(
            "studio_platform.control.WorkerControl.pool_status",
            return_value={"ready": 1, "busy": 0},
        )
        self.capacity.start()
        self.addCleanup(self.capacity.stop)

    def pair(self, scope=None, *, audio=True):
        scope = scope or self.scope
        plan = self.repo.create_plan(
            scope,
            {"recipe_id": "synthetic-h3", "simulation": True},
            {"pool": "audio-fixture", "backend": "mock", "expected_runtime_s": 1},
            expires_at=self.repo.clock() + 1000,
        )
        job = self.repo.create_job(scope, plan["id"], uuid.uuid4().hex)
        lease = self.queue.claim("fixture-worker", "audio-fixture").lease
        self.queue.begin_submission(lease)
        self.queue.record_submitted(lease, "fixture-" + job["id"])
        self.queue.begin_collection(lease)
        specs = [
            {
                "kind": kind,
                "object_key": f"owners/{scope.owner_id}/assets/fixture/{kind}."
                + ("mp4" if kind == "video" else "flac"),
                "size_bytes": 1234,
                "sha256": "a" * 64,
                "validated": True,
                "duration_s": 5,
                "content_type": "video/mp4" if kind == "video" else "audio/flac",
            }
            for kind in (["video", "audio"] if audio else ["video"])
        ]
        self.queue.complete(lease, specs, actual_cost_microusd=0)
        with self.repo.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(artifacts).where(artifacts.c.job_id == job["id"])
                )
                .mappings()
                .all()
            )
        return job["id"], {row["metadata"]["kind"]: row["id"] for row in rows}

    def bind(self, job_id, pair):
        self.project["entities"][3]["data"].update(
            cloudArtifactId=pair["video"], sourceJobId=job_id
        )
        self.project["entities"][4]["data"].update(
            cloudArtifactId=pair.get("audio", "missing-audio"), sourceJobId=job_id
        )
        self.track["generatedFrom"].update(
            jobId=job_id,
            videoArtifactId=pair["video"],
            audioArtifactId=pair.get("audio", "missing-audio"),
        )

    def plan(self):
        if not self.version:
            result = self.client.post("/v1/projects", json={"project": self.project})
        else:
            result = self.client.put(
                "/v1/projects/story-one",
                json={"project": self.project, "expected_version": self.version},
            )
        self.assertIn(result.status_code, (200, 201), result.text)
        self.version += 1
        result = self.client.post("/v1/render-plans", json=BODY)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertNotIn("object_key", result.text)
        return result.json()

    def test_same_job_approved_artifacts_then_missing_or_wrong_job_audio(self):
        job_id, pair = self.pair()
        self.bind(job_id, pair)
        self.assertEqual(self.plan()["status"], "ready")
        other_job, other_pair = self.pair()
        # Browser claims same job in both entity and binding. Actual ledger wins.
        self.project["entities"][4]["data"]["cloudArtifactId"] = other_pair["audio"]
        self.track["generatedFrom"]["audioArtifactId"] = other_pair["audio"]
        self.assertEqual(self.plan()["status"], "blocked")
        self.bind(job_id, {"video": pair["video"]})
        self.assertEqual(self.plan()["status"], "blocked")
        self.bind(job_id, pair)
        self.assertEqual(self.plan()["status"], "ready")

    def test_foreign_owner_project_and_tenant_pair_are_not_renderable(self):
        for scope in (
            Scope("sixnine", "supervan", "story-one"),
            Scope("sixnine", "superdan", "other-project"),
            Scope("foreign-tenant", "superdan", "story-one"),
        ):
            self.bind(*self.pair(scope))
            if (
                scope.tenant_id != self.scope.tenant_id
                or scope.owner_id != self.scope.owner_id
            ):
                # New foreign identities cannot be saved across the cleanup
                # owner fence; same-owner project reuse still reaches render.
                response = (
                    self.client.post("/v1/projects", json={"project": self.project})
                    if not self.version
                    else self.client.put(
                        "/v1/projects/story-one",
                        json={
                            "project": self.project,
                            "expected_version": self.version,
                        },
                    )
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("foreign_content_reference", response.text)
                continue
            self.assertEqual(self.plan()["status"], "blocked")

    def test_unverified_wrong_kind_or_unsucceeded_job_are_rejected(self):
        job_id, pair = self.pair()
        self.bind(job_id, pair)
        with self.repo.engine.connect() as connection:
            original = dict(
                connection.execute(
                    select(artifacts.c.metadata).where(artifacts.c.id == pair["audio"])
                ).scalar_one()
            )
        for change in (
            {"validated": False},
            {"kind": "video"},
            {"content_type": "audio/wav"},
        ):
            with self.repo.transaction() as connection:
                connection.execute(
                    update(artifacts)
                    .where(artifacts.c.id == pair["audio"])
                    .values(metadata={**original, **change})
                )
            self.assertEqual(self.plan()["status"], "blocked")
        with self.repo.transaction() as connection:
            connection.execute(
                update(artifacts)
                .where(artifacts.c.id == pair["audio"])
                .values(metadata=original)
            )
            connection.execute(
                update(jobs).where(jobs.c.id == job_id).values(status="collecting")
            )
        self.assertEqual(self.plan()["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
