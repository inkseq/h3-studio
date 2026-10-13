"""Owned frame assets: synthetic CPU bytes, existing journals and no inference."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import secrets
import unittest
from unittest import mock

from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update, func

from studio_platform import media
from studio_platform.artifact_frames import extract, selection
from studio_platform.assets import AssetService, AssetConflict, asset_table
from studio_platform.repository import artifacts, attempts, budget_reservations, jobs, plans
from studio_platform.api import create_app
from studio_platform.storage import S3ObjectStore, StorageWriteUncertain, make_object_key
from studio_platform.storage_asset_journal import receipts
import test_platform_api as api_fixtures
import test_platform_storage as storage_fixtures
import test_platform_storage_multipart as multipart_fixtures


class ArtifactFrameTests(unittest.TestCase):
    setUp = api_fixtures.ApiTests.setUp
    login = api_fixtures.ApiTests.login
    setup_project = api_fixtures.ApiTests.setup_project
    make_plan = api_fixtures.ApiTests.make_plan

    @classmethod
    def setUpClass(cls):
        cls.media_temp = tempfile.TemporaryDirectory()
        cls.source_path = Path(cls.media_temp.name) / "synthetic.mp4"
        media.run_media_process(["ffmpeg", "-v", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "color=red:s=256x256:r=24:d=1",
            "-f", "lavfi", "-i", "color=blue:s=256x256:r=24:d=1",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", cls.source_path], timeout=30)
        cls.video_bytes = cls.source_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def fixture(self, *, project_id="story-one", artifact_id="artifact-video", status="succeeded", metadata=None):
        self.setup_project(project_id)
        plan = self.make_plan(project_id=project_id)
        job = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
            headers={"Idempotency-Key": "fixture-"+artifact_id}).json()
        key = make_object_key("superdan", artifact_id, "verified.mp4")
        self.app.state.storage.put(key, io.BytesIO(self.video_bytes), content_type="video/mp4",
            max_bytes=self.settings.max_upload_bytes, expected_sha256=hashlib.sha256(self.video_bytes).hexdigest())
        info = {"kind": "video", "mime": "video/mp4", "validated": True,
            "object_key": key, "size_bytes": len(self.video_bytes), "sha256": hashlib.sha256(self.video_bytes).hexdigest(),
            "duration_s": 2, "frame_count": 48, "fps": 24, **(metadata or {})}
        with self.app.state.repository.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status=status))
            conn.execute(insert(attempts).values(id="attempt-"+artifact_id, job_id=job["id"], number=1,
                status=status, fence=1, worker_id="synthetic", created_at=1, updated_at=1))
            conn.execute(insert(artifacts).values(id=artifact_id, job_id=job["id"], attempt_id="attempt-"+artifact_id,
                metadata=info, created_at=1))
        return job, info

    def save(self, body=None, *, key="frame-one", project_id="story-one", artifact_id="artifact-video"):
        return self.client.post(f"/v1/projects/{project_id}/artifacts/{artifact_id}/frames",
            json=body or {"position": "last"}, headers={"Idempotency-Key": key})

    def counts(self):
        with self.app.state.repository.engine.connect() as conn:
            return tuple(conn.execute(select(func.count()).select_from(table)).scalar_one()
                         for table in (plans, jobs, attempts, budget_reservations, asset_table, receipts))

    def color(self, asset):
        content = self.client.get(asset["content_url"])
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(hashlib.sha256(content.content).hexdigest(), asset["metadata"]["sha256"])
        with Image.open(io.BytesIO(content.content)) as image:
            self.assertEqual(image.size, (256, 256))
            return image.convert("RGB").getpixel((128, 128))

    def test_real_first_and_true_last_frame_preserve_original_and_execution_ledgers(self):
        job, original = self.fixture()
        before = self.counts()
        first = self.save({"position": "time", "time_seconds": 0}, key="first")
        last = self.save(key="last")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(last.status_code, 201, last.text)
        red, blue = self.color(first.json()), self.color(last.json())
        self.assertGreater(red[0], 240)
        self.assertGreater(blue[2], 240)
        self.assertEqual(last.json()["selection"]["frame_index"], 47)
        self.assertEqual(last.json()["selection"]["source_sha256"], original["sha256"])
        self.assertEqual(last.json()["selection"]["job_id"], job["id"])
        self.assertEqual(last.json()["metadata"]["artifact_frame"], last.json()["selection"])
        self.assertEqual(before[:4], self.counts()[:4])
        self.assertEqual(self.client.get("/v1/artifacts/artifact-video/content").content, self.video_bytes)
        self.assertEqual(self.app.state.assets.usage("superdan")["active_uploads"], 0)
        for forbidden in ("object_key", "owners/", str(self.settings.data_dir)):
            self.assertNotIn(forbidden, last.text)

    def test_replay_same_identity_changed_body_conflicts_without_new_asset(self):
        self.fixture()
        first = self.save()
        before = self.counts()
        with mock.patch("studio_platform.artifact_frames.extract", side_effect=AssertionError("Replay must not decode")):
            replay = self.save()
        self.assertEqual(first.json(), replay.json())
        conflict = self.save({"position": "time", "time_seconds": 0})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["detail"], "frame_idempotency_conflict")
        self.assertEqual(self.counts(), before)

    def test_owner_and_same_owner_cross_project_rejection_before_asset_reservation(self):
        self.fixture()
        self.client.post("/v1/projects", json={"project": api_fixtures.project("another-story")}).raise_for_status()
        before = self.counts()
        self.assertEqual(self.save(project_id="another-story").status_code, 404)
        self.login("supervan")
        self.client.post("/v1/projects", json={"project": api_fixtures.project()}).raise_for_status()
        self.assertEqual(self.save().status_code, 404)
        self.assertEqual(before, self.counts())

    def test_non_success_or_unvalidated_video_rejected(self):
        self.fixture(status="collecting")
        before = self.counts()
        self.assertEqual(self.save().status_code, 409)
        with self.app.state.repository.engine.begin() as conn:
            conn.execute(update(jobs).values(status="succeeded"))
            row = conn.execute(select(artifacts.c.metadata)).scalar_one()
            conn.execute(update(artifacts).values(metadata={**row, "validated": False}))
        self.assertEqual(self.save().status_code, 422)
        self.assertEqual(before, self.counts())

    def test_machine_scopes_and_project_allowlist_are_required(self):
        self.fixture()
        before = self.counts()
        token = secrets.token_urlsafe(32)
        self.client.cookies.clear()
        headers = {"Authorization": "Bearer "+token, "Idempotency-Key": "machine-frame"}
        for projects, scopes in ((["other"], ["assets:write", "jobs:read"]),
                (["story-one"], ["assets:write"]), (["story-one"], ["jobs:read"])):
            with self.subTest(projects=projects, scopes=scopes):
                self.app.state.auth.register_client("frame-agent", token, "superdan", projects, scopes)
                result = self.client.post("/v1/projects/story-one/artifacts/artifact-video/frames",
                    json={"position": "last"}, headers=headers)
                self.assertEqual(result.status_code, 404, result.text)
        self.assertEqual(before, self.counts())
        self.app.state.auth.register_client("frame-agent", token, "superdan", ["story-one"],
            ["assets:write", "assets:read", "jobs:read"])
        result = self.client.post("/v1/projects/story-one/artifacts/artifact-video/frames",
            json={"position": "last"}, headers=headers)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(self.client.get(result.json()["content_url"], headers=headers).status_code, 200)

    def test_r2_adapter_frame_roundtrip_cleans_only_owned_scratch(self):
        selected = storage_fixtures.config()
        fake = multipart_fixtures.FakeMultipartS3(selected)
        store = S3ObjectStore(selected, client=fake)
        self.app = create_app(self.settings, repository=self.app.state.repository, storage=store)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        _, source = self.fixture()
        before = dict(fake.objects)
        frame = self.save()
        self.assertEqual(frame.status_code, 201, frame.text)
        download = self.client.get(frame.json()["content_url"], follow_redirects=False)
        self.assertEqual(download.status_code, 307)
        service = self.app.state.assets
        receipt = service.journal.get("superdan", frame.json()["id"])
        with store.open(receipt["asset"]["original"]["key"]) as stream:
            body = stream.read()
        self.assertEqual(hashlib.sha256(body).hexdigest(), frame.json()["metadata"]["sha256"])
        with Image.open(io.BytesIO(body)) as image:
            self.assertGreater(image.convert("RGB").getpixel((128, 128))[2], 240)
        self.assertEqual(receipt["scratch_cleanup"]["phase"], "complete")
        self.assertFalse(service._stage(receipt).exists())
        self.assertEqual(fake.objects[source["object_key"]], before[source["object_key"]])
        self.assertEqual(len(fake.objects), 3)
        self.assertEqual(service.usage("superdan")["active_uploads"], 0)
        self.assertEqual(service.resume("superdan", frame.json()["id"])["id"], frame.json()["id"])
        self.assertNotIn("fake-secret", frame.text)
        self.assertFalse(any(op == "delete" for op, _ in fake.calls))

    def test_bad_selection_and_missing_key_do_not_process(self):
        self.fixture()
        invalid = ({}, {"position": []}, {"position": {}}, {"position": "other"}, {"position": "last", "time_seconds": 0},
            {"position": "time", "time_seconds": True}, {"position": "time", "time_seconds": -1},
            {"position": "time", "time_seconds": "0"}, {"position": "time", "time_seconds": 600},
            {"position": "time", "time_seconds": 0, "url": "https://private.invalid/secret"})
        before = self.counts()
        for body in invalid:
            with self.subTest(body=body):
                response = self.client.post("/v1/projects/story-one/artifacts/artifact-video/frames", json=body,
                    headers={"Idempotency-Key": "invalid"})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.post("/v1/projects/story-one/artifacts/artifact-video/frames",
            json={"position": "last"}).status_code, 422)
        self.assertEqual(before, self.counts())

    def test_time_at_duration_fails_and_releases_processing_with_recoverable_receipt(self):
        self.fixture()
        response = self.save({"position": "time", "time_seconds": 2})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("超出视频范围", response.text)
        self.assertEqual(self.app.state.assets.usage("superdan")["active_uploads"], 0)
        listed = self.app.state.assets.list("superdan", "story-one")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "failed")

    def test_lost_put_response_recovers_same_asset_without_rewrite_or_new_decode(self):
        self.fixture()
        original_put = self.app.state.storage.put
        calls = []
        def uncertain_put(key, source, **options):
            calls.append(key)
            result = original_put(key, source, **options)
            if len(calls) == 1:
                raise StorageWriteUncertain(key)
            return result
        with mock.patch.object(self.app.state.storage, "put", side_effect=uncertain_put):
            first = self.save()
            self.assertEqual(first.status_code, 503, first.text)
            known = self.app.state.assets.list("superdan", "story-one")
            self.assertEqual(len(known), 1)
            self.assertEqual(known[0]["status"], "storage_unknown")
            with mock.patch("studio_platform.artifact_frames.extract", side_effect=AssertionError("Already decoded")):
                recovered = self.save()
        self.assertEqual(recovered.status_code, 201, recovered.text)
        self.assertEqual(recovered.json()["id"], known[0]["id"])
        self.assertEqual(len(calls), 2)  # Original once; model PNG once.
        self.assertEqual(self.counts()[-2:], (1, 1))

    def test_live_operation_lock_never_taken_over(self):
        self.fixture()
        known = self.save().json()
        with self.app.state.assets._operation(known["id"]):
            response = self.save()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.counts()[-2:], (1, 1))

    def test_unknown_frame_cannot_resume_against_a_changed_storage_binding(self):
        _, original = self.fixture()
        store = self.app.state.storage
        real_put = store.put
        def uncertain_put(key, source, **options):
            real_put(key, source, **options)
            raise StorageWriteUncertain(key)
        with mock.patch.object(store, "put", side_effect=uncertain_put):
            self.assertEqual(self.save().status_code, 503)
        old = self.app.state.assets
        frame = old.list("superdan", "story-one")[0]
        receipt = old.journal.get("superdan", frame["id"])
        usage = old.usage("superdan")
        selected = storage_fixtures.config(bucket="different-bucket")
        fake = multipart_fixtures.FakeMultipartS3(selected)
        changed = AssetService(self.app.state.repository.engine, S3ObjectStore(selected, client=fake),
            self.settings.data_dir, tenant=self.settings.tenant_id, max_bytes=self.settings.max_upload_bytes)
        with self.assertRaisesRegex(AssetConflict, "存储身份"):
            changed.artifact_frame("superdan", "story-one", self.client.get("/api/auth/me").json()["actor_id"], "artifact-video",
                receipt["asset"]["selection"]["job_id"], original,
                {"position": "last"}, "frame-one")
        self.assertEqual(old.journal.get("superdan", frame["id"]), receipt)
        self.assertEqual(old.usage("superdan"), usage)
        self.assertEqual(fake.calls, [])

    def test_decoder_failure_keeps_safe_diagnostic_no_private_paths(self):
        self.fixture()
        with mock.patch.object(media, "ffmpeg", side_effect=media.MediaError("媒体处理失败、超时或达到资源限制；原素材保持不变")):
            response = self.save()
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(str(self.settings.data_dir), response.text)
        self.assertIn("媒体处理失败", response.text)
        self.assertEqual(self.app.state.assets.usage("superdan")["active_uploads"], 0)

    def test_source_hash_failure_never_accepts_different_bytes(self):
        self.fixture(metadata={"sha256": "0"*64})
        response = self.save()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("ready", response.text)
        self.assertEqual(self.app.state.assets.list("superdan", "story-one")[0]["status"], "failed")

    def test_exact_decoder_limits_and_source_protocol_restrictions(self):
        output = Path(self.media_temp.name)
        with mock.patch.object(media, "ffmpeg", side_effect=media.MediaError("stopped")) as process:
            with self.assertRaises(media.MediaError):
                extract(self.source_path, output, {"position": "last"}, max_output_bytes=1024)
        args = process.call_args.args[0]
        self.assertIn("select=eq(n\\,47)", args)
        self.assertIn("-xerror", args)
        self.assertEqual(args[args.index("-fs")+1], "1024")
        self.assertEqual(process.call_args.kwargs["timeout"], 180)
        self.assertEqual(media.PROCESSING_ADMISSION._used, 0)
        for body in ({"position": "time", "time_seconds": float("nan")}, {"position": "time", "time_seconds": float("inf")}):
            with self.assertRaises(media.MediaError):
                selection(body)


if __name__ == "__main__":
    unittest.main()
