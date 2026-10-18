"""Offline API acceptance tests: temporary DATA, real FFmpeg media, no GPU/network.

Run from h3-studio: .venv/Scripts/python.exe -m unittest -v test_server.py
The server is imported under a private module name while DATA is redirected to
a TemporaryDirectory. TestClient is used without lifespan except in the explicit
restart test, where the worker is replaced. Existing user data is never opened.
"""

import gc
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient
from PIL import Image


class ServerOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("Real media validation requires FFmpeg and FFprobe")
        cls.temp = tempfile.TemporaryDirectory(prefix="h3-studio-offline-tests-")
        cls.root = Path(cls.temp.name)
        cls.data = cls.root / "service-data"
        cls.media = cls.root / "fixtures"
        cls.media.mkdir()
        module_spec = importlib.util.spec_from_file_location(
            "_h3_studio_offline_test_server", (Path(__file__).resolve().parents[1] / "server.py"))
        cls.server = importlib.util.module_from_spec(module_spec)
        with mock.patch.dict(os.environ, {"H3_STUDIO_DATA": str(cls.data),
                                          "H3_COMFY_URL": "http://127.0.0.1:8189"}):
            module_spec.loader.exec_module(cls.server)
        cls.execute_original = cls.server.execute_job
        # Windows asyncio builds an internal loopback socketpair. Blocking every
        # socket.connect would break TestClient itself; block actual HTTP network
        # transports while retaining its in-process ASGI transport instead.
        cls.network_guard = mock.patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Offline tests forbid HTTP network requests"))
        cls.network_guard.start()
        Image.new("RGB", (320, 320), "orange").save(cls.media / "valid.png")
        Image.new("RGB", (64, 64), "red").save(cls.media / "small.png")
        cls.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "5", str(cls.media / "valid.wav"))
        cls.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000", "-t", "1.5", str(cls.media / "short.wav"))
        cls.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000", "-t", "15.1", str(cls.media / "long.wav"))
        cls.ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=320x320:r=30", "-f", "lavfi", "-i",
                   "sine=frequency=220:sample_rate=44100", "-t", "2.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-movflags", "+faststart", str(cls.media / "sound.mp4"))
        cls.ffmpeg("-f", "lavfi", "-i", "color=c=green:s=320x320:r=30", "-t", "2.5", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "-an", str(cls.media / "silent.mp4"))
        cls.ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=1344x768:r=24", "-f", "lavfi", "-i",
                   "sine=frequency=220:sample_rate=32000", "-frames:v", "124", "-t", str(124 / 24),
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
                   str(cls.media / "output.mp4"))
        cls.ffmpeg("-f", "lavfi", "-i", "color=c=green:s=1344x768:r=24", "-frames:v", "124",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(cls.media / "output-silent.mp4"))
        cls.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000", "-t", "5.3", "-ac", "2",
                   "-c:a", "flac", str(cls.media / "output.flac"))

    @classmethod
    def tearDownClass(cls):
        cls.server.STOP.set()
        cls.network_guard.stop()
        # sqlite.Connection.__exit__ commits but does not close; the current
        # server's module-level schema initialization also retains global c.
        # Only close this private test module's connection, never the live app.
        schema_connection = getattr(cls.server, "c", None)
        if schema_connection is not None:
            schema_connection.close()
        gc.collect()
        cls.temp.cleanup()

    @classmethod
    def ffmpeg(cls, *args):
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args],
                       capture_output=True, check=True, timeout=30)

    def setUp(self):
        self.server.STOP.clear()
        self.server.BLOCKED = None
        with self.server.db() as connection:
            connection.execute("DELETE FROM jobs")
            connection.execute("DELETE FROM uploads")
        for folder in ("uploads", "outputs", "workflows"):
            for path in (self.data / folder).iterdir():
                if path.is_file():
                    path.unlink()
        self.capability_patch = mock.patch.object(self.server, "capability", return_value={
            "backends": [{"id": "comfy-local", "available": True, "reason": "offline fixture"}]
        })
        self.capability_mock = self.capability_patch.start()
        self.execute_patch = mock.patch.object(self.server, "execute_job", side_effect=AssertionError("Test must not dispatch GPU work"))
        self.execute_mock = self.execute_patch.start()
        self.client = TestClient(self.server.app)
        login = self.client.post("/api/auth/login", json={"username": "superdan"})
        self.assertEqual(login.status_code, 200, login.text)

    def tearDown(self):
        self.server.STOP.set()
        self.client.close()
        self.execute_patch.stop()
        self.capability_patch.stop()

    def upload(self, filename="valid.png"):
        path = self.media / filename
        with path.open("rb") as stream:
            response = self.client.post("/api/uploads", files={"file": (filename, stream, "application/octet-stream")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def payload(self, mode="fl", inputs=None):
        return {"backend": "comfy-local", "model": self.server.MODEL, "mode": mode,
                "prompt": "A quiet cinematic scene.", "duration": 5, "resolution": "768P",
                "aspect_ratio": "16:9", "generate_audio": True, "seed": 42,
                "inputs": inputs or {}}

    def post_job(self, payload=None, **kwargs):
        return self.client.post("/api/jobs", json=payload or self.payload(), **kwargs)

    def test_openapi_documents_generation_body_example_and_optional_idempotency_header(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        operation = response.json()["paths"]["/api/jobs"]["post"]
        self.assertTrue(operation["requestBody"]["required"])
        content = operation["requestBody"]["content"]["application/json"]
        schema = content["schema"]
        self.assertEqual(set(schema["required"]), {"backend", "model", "mode", "prompt", "duration", "resolution", "aspect_ratio"})
        fields = schema["properties"]
        self.assertEqual(fields["backend"]["enum"], ["comfy-local"])
        self.assertEqual(fields["model"]["enum"], [self.server.MODEL])
        self.assertEqual(fields["mode"]["enum"], ["ref", "fl"])
        self.assertEqual((fields["duration"]["minimum"], fields["duration"]["maximum"]), (4, 15))
        self.assertEqual((fields["steps"]["minimum"], fields["steps"]["maximum"], fields["steps"]["default"]), (1, 100, 20))
        self.assertEqual(fields["resolution"]["enum"], ["480P", "576P", "768P", "custom"])
        self.assertEqual(fields["seed"]["anyOf"][0]["maximum"], 2**64 - 1)
        inputs = fields["inputs"]["properties"]
        self.assertEqual([inputs[key]["maxItems"] for key in ("images", "videos", "audios")], [9, 3, 3])
        self.assertEqual(set(inputs), {"images", "videos", "audios", "first_frame", "last_frame"})
        header = next(p for p in operation["parameters"] if p["name"] == "Idempotency-Key")
        self.assertEqual(header["in"], "header")
        self.assertFalse(header["required"])
        self.assertEqual((header["schema"]["minLength"], header["schema"]["maxLength"]), (8, 128))
        example = content["examples"]["text_only"]["value"]
        self.assertEqual(example["mode"], "fl")
        self.assertFalse(any(example["inputs"].values()))
        submitted = self.post_job(example)
        self.assertEqual(submitted.status_code, 202, submitted.text)
        self.assertEqual(submitted.json()["status"], "queued")
        self.execute_mock.assert_not_called()

    def offline_execution(self, *, generate_audio=True, video="output.mp4", audio="output.flac",
                          cancel_result=None, cancel_on_prompt=False, stop_on_prompt=False, audio_key="audio"):
        """Exercise the real adapter against an in-memory HTTP transport only."""
        payload = self.payload()
        payload["generate_audio"] = generate_audio
        job = self.post_job(payload).json()
        job["status"] = "running"
        self.server.put_job(job)
        prompt_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        from comfy_workflow import build_workflow
        graph = build_workflow({**job["request"], "_job_id": job["id"]}, {}, {})
        video_node = next(nid for nid, node in graph.items() if node["class_type"] == "SaveVideo")
        video_name = job["id"] + "_00001_.mp4"
        audio_name = job["id"] + "_audio_00001.flac"
        sources = {video_name: video, audio_name: audio}
        video_entry = {"filename": video_name, "subfolder": "h3-studio", "type": "output"}
        # Loader previews precede save results in the real live history.
        outputs = {"preview-input": {"images": [{"filename": "reference.normalized.mp4", "subfolder": "", "type": "input"}]},
                   video_node: {"images": [video_entry]}}
        if audio:
            audio_node = next(nid for nid, node in graph.items() if node["class_type"] == "SaveAudioAdvanced")
            entries = [{"filename": audio_name, "subfolder": "h3-studio", "type": "output"}]
            outputs[audio_node] = {audio_key: entries}
            if audio_key == "both":
                outputs[audio_node] = {"audio": entries, "audios": entries}
        history = {prompt_id: {"status": {"completed": True}, "outputs": outputs}}
        requests = []

        def handler(request):
            requests.append((request.method, request.url.path))
            if request.url.path == "/queue":
                return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
            if request.method == "POST" and request.url.path == "/prompt":
                if cancel_on_prompt:
                    self.client.post(f"/api/jobs/{job['id']}/cancel")
                if stop_on_prompt:
                    self.server.STOP.set()
                return httpx.Response(200, json={"prompt_id": prompt_id})
            if request.url.path == f"/api/jobs/{prompt_id}/cancel":
                return httpx.Response(200, json={"cancelled": cancel_result})
            if request.url.path == f"/history/{prompt_id}":
                return httpx.Response(200, json=history)
            if request.url.path == "/view":
                filename = parse_qs(request.url.query.decode())["filename"][0]
                self.assertIn(filename, sources, "Input previews or other jobs must never be downloaded as outputs")
                return httpx.Response(200, content=(self.media / sources[filename]).read_bytes())
            raise AssertionError(f"Unexpected offline request: {request.method} {request.url}")

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        return job, prompt_id, requests, client

    def run_offline_execution(self, job, client):
        with client:
            self.__class__.execute_original(job, client)
        self.server.put_job(job)

    def test_import_and_uploads_stay_in_temporary_data(self):
        self.assertEqual(self.server.DATA, self.data)
        self.assertEqual(self.server.DB.parent, self.data)
        uploaded = self.upload()
        self.assertEqual(uploaded["kind"], "image")
        self.assertEqual((uploaded["width"], uploaded["height"]), (320, 320))
        self.assertNotIn("path", uploaded)
        self.assertNotIn("model_path", uploaded)
        private = self.server.upload_info(uploaded["id"])
        self.assertTrue(Path(private["model_path"]).is_relative_to(self.data))
        self.assertEqual(self.client.get(uploaded["preview_url"]).status_code, 200)
        self.execute_mock.assert_not_called()

    def test_real_audio_and_video_are_decoded_and_normalized(self):
        audio = self.upload("valid.wav")
        self.assertAlmostEqual(audio["source_duration"], 5, places=2)
        private = self.server.upload_info(audio["id"])
        stream = self.server.probe(private["model_path"])["streams"][0]
        self.assertEqual(int(stream["sample_rate"]), 32000)
        self.assertEqual(stream["channels"], 2)
        video = self.upload("sound.mp4")
        self.assertTrue(video["has_audio"])
        self.assertEqual(video["fps"], 24)
        self.assertEqual(video["frame_count"] % 17, 5)
        self.assertGreaterEqual(video["duration"], video["source_duration"])
        private = self.server.upload_info(video["id"])
        decoded = self.server.probe(private["model_path"])
        frames = next(x for x in decoded["streams"] if x["codec_type"] == "video")
        self.assertEqual(int(frames["nb_frames"]), video["frame_count"])
        self.assertEqual(frames["avg_frame_rate"], "24/1")
        self.assertTrue(any(x["codec_type"] == "audio" for x in decoded["streams"]))
        self.assertFalse(self.upload("silent.mp4")["has_audio"])

    def test_corrupt_empty_unsupported_and_small_uploads_leave_no_assets(self):
        cases = [("bad.png", b"not an image"), ("empty.wav", b""),
                 ("bad.mp4", b"not video"), ("bad.exe", b"MZ"),
                 ("wrong.wav", (self.media / "valid.png").read_bytes()),
                 ("small.png", (self.media / "small.png").read_bytes())]
        for name, content in cases:
            with self.subTest(name=name):
                response = self.client.post("/api/uploads", files={"file": (name, content)})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(list((self.data / "uploads").iterdir()), [])
        with self.server.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 0)

    def test_real_media_duration_limits_and_size_limit(self):
        for name in ("short.wav", "long.wav"):
            with (self.media / name).open("rb") as stream:
                response = self.client.post("/api/uploads", files={"file": (name, stream)})
            self.assertEqual(response.status_code, 422, response.text)
        response = self.client.post("/api/uploads", files={"file": ("oversize.wav", b"0" * (15 * 1024 * 1024 + 1))})
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(list((self.data / "uploads").iterdir()), [])

    def test_multimodal_and_first_last_frame_jobs_validate_without_dispatch(self):
        image = self.upload()
        video = self.upload("sound.mp4")
        audio = self.upload("valid.wav")
        response = self.post_job(self.payload("ref", {"images": [image["id"]], "videos": [video["id"]], "audios": [audio["id"]]}))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(response.json()["request"]["steps"], 20)
        image2 = self.upload()
        response = self.post_job(self.payload("fl", {"first_frame": image["id"], "last_frame": image2["id"]}))
        self.assertEqual(response.status_code, 202, response.text)
        self.execute_mock.assert_not_called()

    def test_job_parameter_mode_type_and_id_rejections(self):
        image = self.upload()
        audio = self.upload("valid.wav")
        cases = [{"mode": "other"}, {"duration": 3}, {"duration": 5.5}, {"duration": True},
                 {"resolution": "2K"}, {"steps": 101}, {"seed": -1}, {"seed": True},
                 {"generate_audio": "false"}, {"prompt": " "}, {"model": "wrong"},
                 {"inputs": {"first_frame": []}}, {"inputs": {"images": "bad"}},
                 {"inputs": {"first_frame": {"a": 1}}}, {"inputs": {"first_frame": "invalid"}},
                 {"mode": "ref", "inputs": {"images": [123]}},
                 {"mode": "ref", "inputs": {"images": ["0" * 32]}},
                 {"mode": "ref", "inputs": {"images": [audio["id"]]}},
                 {"mode": "ref", "inputs": {"first_frame": image["id"]}},
                 {"mode": "fl", "inputs": {"images": [image["id"]]}},
                 {"mode": "ref", "inputs": {"images": [image["id"], image["id"]]}},
                 {"mode": "ref", "inputs": {}}]
        for patch in cases:
            with self.subTest(patch=patch):
                payload = self.payload()
                payload.update(patch)
                response = self.post_job(payload)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])

    def test_counts_and_totals_are_server_enforced(self):
        image = self.upload()
        video = self.upload("sound.mp4")
        audio = self.upload("valid.wav")
        for key, value in (("images", [image["id"]] * 10), ("videos", [video["id"]] * 4), ("audios", [audio["id"]] * 4)):
            response = self.post_job(self.payload("ref", {key: value}))
            self.assertEqual(response.status_code, 422, response.text)
        mixed = {"images": [image["id"]] * 9, "videos": [video["id"]] * 3, "audios": [audio["id"]]}
        self.assertEqual(self.post_job(self.payload("ref", mixed)).status_code, 422)
        # Distinct, decoded assets: total source video 7.5s and audio 15s are
        # valid separately even though their combined duration exceeds 15s.
        video_ids = [video["id"], *(self.upload("sound.mp4")["id"] for _ in range(2))]
        audio_ids = [audio["id"], *(self.upload("valid.wav")["id"] for _ in range(2))]
        response = self.post_job(self.payload("ref", {"videos": video_ids, "audios": audio_ids}))
        self.assertEqual(response.status_code, 202, response.text)
        # A decoded metadata total is also checked without sending anything to Comfy.
        with self.server.db() as connection:
            record = self.server.upload_info(audio_ids[0])
            record.update(duration=6, source_duration=6)
            connection.execute("UPDATE uploads SET metadata=? WHERE id=?", (json.dumps(record), audio_ids[0]))
        response = self.post_job(self.payload("ref", {"audios": audio_ids}))
        self.assertEqual(response.status_code, 422, response.text)

    def test_reference_video_longer_than_output_is_rejected(self):
        video = self.upload("sound.mp4")
        record = self.server.upload_info(video["id"])
        record.update(frame_count=141, duration=141 / 24, source_duration=5.5)
        with self.server.db() as connection:
            connection.execute("UPDATE uploads SET metadata=? WHERE id=?", (json.dumps(record), video["id"]))
        response = self.post_job(self.payload("ref", {"videos": [video["id"]]}))
        self.assertEqual(response.status_code, 422, response.text)

    def test_idempotent_replay_conflict_and_invalid_key(self):
        payload = self.payload()
        headers = {"Idempotency-Key": "offline-replay-123"}
        first = self.post_job(payload, headers=headers)
        second = self.post_job(payload, headers=headers)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(self.client.get("/api/jobs").json()["jobs"]), 1)
        payload["prompt"] = "A different scene."
        self.assertEqual(self.post_job(payload, headers=headers).status_code, 409)
        self.assertEqual(self.post_job(headers={"Idempotency-Key": "short"}).status_code, 422)

    def test_existing_idempotent_job_replays_during_gpu_disconnect(self):
        payload = self.payload()
        headers = {"Idempotency-Key": "offline-disconnect-123"}
        first = self.post_job(payload, headers=headers)
        self.assertEqual(first.status_code, 202, first.text)
        self.capability_mock.return_value["backends"][0]["available"] = False
        replay = self.post_job(payload, headers=headers)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(first.json()["id"], replay.json()["id"])

    def test_cancel_requested_survives_stale_running_progress_write(self):
        stale = self.post_job().json()
        stale["status"] = "running"
        self.server.put_job(stale)
        response = self.client.post(f"/api/jobs/{stale['id']}/cancel")
        self.assertEqual(response.json()["status"], "cancel_requested")
        stale["elapsed_seconds"] = 10
        self.server.put_job(stale)
        saved = self.server.get_job(stale["id"])
        self.assertEqual(saved["status"], "cancel_requested")
        self.assertEqual(saved["elapsed_seconds"], 10)

    def test_cancel_requested_wins_a_late_completed_record_write(self):
        stale = self.post_job().json()
        stale["status"] = "running"
        self.server.put_job(stale)
        self.client.post(f"/api/jobs/{stale['id']}/cancel")
        stale.update(status="succeeded", output_url="/stale.mp4", audio_output_url="/stale.flac")
        self.server.put_job(stale)
        saved = self.server.get_job(stale["id"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertIsNone(saved["output_url"])
        self.assertIsNone(saved["audio_output_url"])

    def test_queue_capacity_cancel_and_unavailable_backend(self):
        queued = []
        for _ in range(8):
            response = self.post_job()
            self.assertEqual(response.status_code, 202, response.text)
            queued.append(response.json())
        self.assertEqual(self.post_job().status_code, 429)
        cancelled = self.client.post(f"/api/jobs/{queued[0]['id']}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.post_job().status_code, 202)
        job = queued[1]
        job["status"] = "running"
        self.server.put_job(job)
        cancelled = self.client.post(f"/api/jobs/{job['id']}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancel_requested")
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}").json()["status"], "cancel_requested")
        self.capability_mock.return_value["backends"][0]["available"] = False
        self.assertEqual(self.post_job().status_code, 503)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/output").status_code, 404)

    def test_host_origin_json_and_missing_records(self):
        self.assertEqual(self.client.get("/api/capabilities", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/capabilities", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/jobs", content="{not json").status_code, 422)
        self.assertEqual(self.client.get("/api/jobs/nonexistent").status_code, 404)
        self.assertEqual(self.client.get("/api/uploads/nonexistent/content").status_code, 422)

    def test_restart_marks_running_failed_without_resubmitting(self):
        queued = self.post_job().json()
        running = self.post_job().json()
        running["status"] = "running"
        self.server.put_job(running)
        with mock.patch.object(self.server, "worker", return_value=None):
            with TestClient(self.server.app) as client:
                login = client.post("/api/auth/login", json={"username": "superdan"})
                self.assertEqual(login.status_code, 200, login.text)
                restored = client.get(f"/api/jobs/{running['id']}").json()
                self.assertEqual(restored["status"], "failed")
                self.assertEqual(client.get(f"/api/jobs/{queued['id']}").json()["status"], "queued")
        self.execute_mock.assert_not_called()

    def test_worker_failure_does_not_replay_or_block_next_idle_job(self):
        first = self.post_job().json()
        second = self.post_job().json()
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.json.return_value = {"queue_running": [], "queue_pending": []}
        calls = []
        def fake_execute(job, _client):
            calls.append(job["id"])
            if len(calls) == 1:
                raise RuntimeError("Offline simulated execution failure")
            job.update(status="succeeded", progress=1)
            self.server.STOP.set()
        with mock.patch.object(self.server.httpx, "Client", return_value=client):
            with mock.patch.object(self.server, "execute_job", side_effect=fake_execute):
                self.server.worker()
        self.assertEqual(calls, [first["id"], second["id"]])
        self.assertEqual(self.server.get_job(first["id"])["status"], "failed")
        self.assertEqual(self.server.get_job(second["id"])["status"], "succeeded")
        self.assertIsNone(self.server.BLOCKED)

    def test_real_generated_outputs_require_audio_and_decode_before_success(self):
        job, _, requests, client = self.offline_execution()
        self.run_offline_execution(job, client)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(self.client.get(job["output_url"]).status_code, 200)
        self.assertEqual(self.client.get(job["audio_output_url"]).status_code, 200)
        audio = next(s for s in job["output_metadata"]["streams"] if s["codec_type"] == "audio")
        self.assertEqual((int(audio["sample_rate"]), audio["channels"]), (32000, 2))
        video = next(s for s in job["output_metadata"]["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"], int(video["nb_frames"])), (1344, 768, 120))
        self.assertAlmostEqual(float(video["duration"]), 5, places=3)
        self.assertAlmostEqual(float(job["audio_output_metadata"]["format"]["duration"]), 5, places=2)
        self.assertNotIn(("POST", "/interrupt"), requests)
        silent, _, _, client = self.offline_execution(generate_audio=False, video="output-silent.mp4", audio=None)
        self.run_offline_execution(silent, client)
        self.assertEqual(silent["status"], "succeeded")
        self.assertFalse(any(s["codec_type"] == "audio" for s in silent["output_metadata"]["streams"]))
        self.assertNotIn("audio_output_url", silent)

    def test_audio_history_accepts_native_singular_and_deduplicates_plural(self):
        for key in ("audios", "both"):
            with self.subTest(key=key):
                job, _, requests, client = self.offline_execution(audio_key=key)
                self.run_offline_execution(job, client)
                self.assertEqual(job["status"], "succeeded")
                self.assertEqual(requests.count(("GET", "/view")), 2)
                self.assertIn("audio_output_url", job)

    def test_real_live_history_preview_cannot_be_selected_as_a_result(self):
        # Actual first-run output shape captured from c602...history.json:
        # LoadVideo "images" is type=input, SaveVideo "images" is type=output,
        # and SaveAudioAdvanced uses singular "audio".
        jid = "c6028b074e794c0385a91200f44877cb"
        graph = {"6": {"class_type": "LoadVideo", "inputs": {}},
                 "17": {"class_type": "SaveAudioAdvanced", "inputs": {"filename_prefix": "h3-studio/" + jid + "_audio"}},
                 "19": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "h3-studio/" + jid}}}
        video = {"filename": jid + "_00001_.mp4", "subfolder": "h3-studio", "type": "output"}
        audio = {"filename": jid + "_audio_00001.flac", "subfolder": "h3-studio", "type": "output"}
        record = {"outputs": {
            "6": {"images": [{"filename": "a21602c2a8f2430dbcfa3952a7bfe056.normalized.mp4", "subfolder": "", "type": "input"}], "animated": [True]},
            "17": {"audio": [audio]}, "19": {"images": [video], "animated": [True]}}}
        self.assertEqual(self.server.saved_job_outputs(graph, record, jid, "SaveVideo", ("images",), ".mp4"), [video])
        self.assertEqual(self.server.saved_job_outputs(graph, record, jid, "SaveAudioAdvanced", ("audio",), ".flac"), [audio])
        for field, invalid in (("filename", "otherjob_00001_.mp4"), ("type", "input"), ("subfolder", "")):
            with self.subTest(field=field):
                bad = json.loads(json.dumps(record))
                bad["outputs"]["19"]["images"][0][field] = invalid
                self.assertEqual(self.server.saved_job_outputs(graph, bad, jid, "SaveVideo", ("images",), ".mp4"), [])

    def test_output_dimensions_frame_rate_and_short_duration_are_rejected(self):
        metadata = {"streams": [{"codec_type": "video", "width": 1344, "height": 768,
                                 "avg_frame_rate": "24/1", "duration": "5"}]}
        self.server.validate_output_video(metadata, self.payload())
        for field, invalid in (("width", 512), ("height", 288), ("avg_frame_rate", "30/1"),
                               ("duration", "3.072"), ("duration", "5.084"), ("duration", "nan")):
            with self.subTest(field=field, invalid=invalid):
                bad = json.loads(json.dumps(metadata))
                bad["streams"][0][field] = invalid
                with self.assertRaisesRegex(RuntimeError, "尺寸、24fps或时长"):
                    self.server.validate_output_video(bad, self.payload())
        job, _, _, client = self.offline_execution(video="sound.mp4")
        with client, self.assertRaisesRegex(RuntimeError, "尺寸、24fps或时长"):
            self.__class__.execute_original(job, client)
        self.assertNotEqual(job["status"], "succeeded")

    def test_generated_audio_missing_invalid_or_undecodable_never_succeeds(self):
        for video, audio, error in (("output-silent.mp4", "output.flac", "MP4"),
                                    ("output.mp4", None, "FLAC")):
            with self.subTest(video=video, audio=audio):
                job, _, _, client = self.offline_execution(video=video, audio=audio)
                with client, self.assertRaisesRegex(RuntimeError, error):
                    self.__class__.execute_original(job, client)
                self.assertNotEqual(job["status"], "succeeded")
                self.assertIsNone(job["output_url"])
        job, _, _, client = self.offline_execution()
        real_ffmpeg = self.server.run_ffmpeg
        def decode_failure(args):
            if "null" in args:
                raise RuntimeError("Offline simulated invalid decoded frame")
            return real_ffmpeg(args)
        with client, mock.patch.object(self.server, "run_ffmpeg", side_effect=decode_failure):
            with self.assertRaisesRegex(RuntimeError, "invalid decoded frame"):
                self.__class__.execute_original(job, client)
        self.assertNotEqual(job["status"], "succeeded")

    def test_audio_metadata_requires_stereo_32khz_and_requested_duration(self):
        valid = {"streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "32000", "channels": 2}],
                 "format": {"duration": "5"}}
        self.server.validate_output_audio(valid, 5, "FLAC", flac=True)
        for field, value in (("sample_rate", "44100"), ("channels", 1), ("codec_name", "aac"),
                             ("duration", "2.5"), ("duration", "nan")):
            with self.subTest(field=field, value=value):
                changed = json.loads(json.dumps(valid))
                changed["streams"][0][field] = value
                with self.assertRaisesRegex(RuntimeError, "32kHz"):
                    self.server.validate_output_audio(changed, 5, "FLAC", flac=True)

    def test_cancellation_is_targeted_and_false_is_an_idempotent_noop(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                job, prompt_id, requests, client = self.offline_execution(cancel_on_prompt=True, cancel_result=cancelled)
                self.run_offline_execution(job, client)
                self.assertEqual(job["status"], "cancelled")
                self.assertIs(job["comfy_cancel_dispatched"], cancelled)
                self.assertIn(("POST", f"/api/jobs/{prompt_id}/cancel"), requests)
                self.assertNotIn(("POST", "/interrupt"), requests)
                self.assertFalse(any(path.startswith("/history/") for _, path in requests))
        job, _, _, client = self.offline_execution(cancel_on_prompt=True, cancel_result="true")
        with client, self.assertRaisesRegex(RuntimeError, "无效状态"):
            self.__class__.execute_original(job, client)
        self.assertNotEqual(job["status"], "cancelled")

    def test_shutdown_and_timeout_cancel_only_the_submitted_prompt(self):
        for shutdown in (True, False):
            with self.subTest(shutdown=shutdown):
                self.server.STOP.clear()
                job, prompt_id, requests, client = self.offline_execution(stop_on_prompt=shutdown, cancel_result=True)
                with client, mock.patch.object(self.server.time, "monotonic", side_effect=[0, 3601]):
                    with self.assertRaisesRegex(RuntimeError, "按任务ID"):
                        self.__class__.execute_original(job, client)
                self.assertIn(("POST", f"/api/jobs/{prompt_id}/cancel"), requests)
                self.assertNotIn(("POST", "/interrupt"), requests)
                self.assertTrue(job["comfy_cancel_dispatched"])

    def test_worker_never_starts_next_job_until_queue_is_verified_idle(self):
        for query_fails in (False, True):
            with self.subTest(query_fails=query_fails):
                self.server.STOP.clear()
                self.server.BLOCKED = None
                with self.server.db() as connection:
                    connection.execute("DELETE FROM jobs")
                first, second = self.post_job().json(), self.post_job().json()
                client = mock.MagicMock()
                client.__enter__.return_value = client
                client.get.return_value.json.return_value = {"queue_running": [[0, "another-task"]], "queue_pending": []}
                if query_fails:
                    client.get.side_effect = RuntimeError("Offline queue read failure")
                calls = []
                def fake_execute(job, _client):
                    calls.append(job["id"])
                    job.update(status="cancelled")
                def wait_until_blocked(_timeout):
                    if self.server.BLOCKED:
                        self.server.STOP.set()
                with mock.patch.object(self.server.httpx, "Client", return_value=client), \
                     mock.patch.object(self.server, "execute_job", side_effect=fake_execute), \
                     mock.patch.object(self.server.STOP, "wait", side_effect=wait_until_blocked):
                    self.server.worker()
                self.assertEqual(calls, [first["id"]])
                self.assertEqual(self.server.get_job(second["id"])["status"], "queued")
                self.assertIsNotNone(self.server.BLOCKED)


if __name__ == "__main__":
    unittest.main()
