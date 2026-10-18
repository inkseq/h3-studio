"""Numbered CPU frames and synthetic tail audio; no model/provider execution."""
import array
import copy
from dataclasses import replace
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from studio_platform.api import create_app
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec, worker_spec_payload
from studio_platform.execution_policy import ExecutionPolicies, validate_policy
from studio_platform.inference.outputs import NATIVE_DELIVERY, delivery_spec, native_delivery_spec
from studio_platform.inference.protocol import BackendError, Outcome
from studio_platform.media import probe
from studio_platform.repository import Conflict, request_hash
from studio_platform.settings import Settings
from studio_platform.storage import StorageWriteUncertain
from studio_platform.worker import WorkerRunner, _shape
from test_platform_api import generation_request, project
from test_platform_execution_policy import policy
from test_platform_repository import LedgerCase


def fixture(root):
    """124 individually numbered frames; audible signal only AFTER five seconds."""
    audio, video = root / "native.wav", root / "native.mp4"
    samples = array.array("h")
    for i in range(round(124 / 24 * 32000)):
        value = round(14000 * math.sin(i * 2 * math.pi * 440 / 32000)) if i >= 160000 else 0
        samples.extend((value, value))
    with wave.open(str(audio), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(32000)
        out.writeframes(samples.tobytes())
    frames = bytearray()
    for number in range(124):
        image = Image.new("RGB", (256, 256), "navy")
        draw = ImageDraw.Draw(image)
        draw.text((24, 90), f"CPU FRAME {number:03d}\nNOT H3", fill="white")
        # Compression-tolerant binary identification proves every frame/order.
        for bit in range(7):
            draw.rectangle((8+bit*32, 8, 31+bit*32, 39), fill="white" if number & (1 << bit) else "black")
        frames.extend(image.tobytes())
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", "256x256", "-r", "24", "-i", "pipe:0", "-i", str(audio), "-c:v", "libx264",
        "-threads", "1", "-crf", "0", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(video)], input=frames, check=True, capture_output=True, timeout=30)
    return {"video": video, "audio": audio}


def frame_numbers(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True, timeout=30)
    size = 256 * 256 * 3
    assert len(result.stdout) % size == 0
    return [sum(1 << bit for bit in range(7) if result.stdout[offset+(24*256+20+bit*32)*3] > 128)
            for offset in range(0, len(result.stdout), size)]


def audio_samples(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0",
        "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"], check=True, capture_output=True, timeout=30)
    samples = array.array("h")
    samples.frombytes(result.stdout)
    return samples


class CPUFixtureBackend:
    enabled, kind, slot_key = True, "wangp-worker", "synthetic-native-delivery"

    def __init__(self, paths):
        self.paths, self.starts = paths, 0

    def prepare(self, *args):
        return None

    def submit(self, prepared, tag):
        self.starts += 1
        return "cpu-" + tag

    def poll(self, tag, task_id):
        return Outcome("succeeded", task_id, 0)

    def fetch(self, *args):
        return self.paths


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU media utilities required")
class NativeDeliveryTests(LedgerCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory(prefix="native-delivery-fixture-")
        cls.paths = fixture(Path(cls.media.name))

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.body = generation_request(self.scope.project_id, controls={"duration": 5, "resolution": "custom",
            "width": 256, "height": 256, "seed": "123"})
        self.compiled, self.fingerprint = compile_request(self.body, lambda _: None, backend="wangp-worker")
        self.control = WorkerControl(self.repo)

    def spec(self, native=True, **overrides):
        spec = WorkerSpec("native-slot" if native else "legacy-slot", "synthetic-pool", "test-only",
            "native-instance" if native else "legacy-instance", ("test-gpu",), ("h3-base-fl2va-v1",),
            "MiniMax-H3-Base-BF16", "native-config" if native else "legacy-config", "wangp-worker",
            "a"*64, output_delivery=NATIVE_DELIVERY if native else "")
        return replace(spec, **overrides)

    def application(self, native=True):
        spec = self.spec(native)
        self.control.register(spec)
        self.control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        value = policy(self.now)
        value.update(backend="wangp-worker", engine_manifest_digest="a"*64,
                     configuration_id=spec.configuration_id, recipe_ids=["h3-base-fl2va-v1"])
        value["envelope"]["controls"] = {"sampler_name": ["euler"], "scheduler": ["auto"],
            "video_decode": ["tiled"], "audio_decode": ["normal"], "encoder_device": ["default"]}
        if native:
            value["output_delivery"] = NATIVE_DELIVERY
        self.policy_value = value
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_text(json.dumps(value), encoding="utf-8")
        self.policy_path.chmod(0o600)
        for name, owner in ((f"test-tenant:{self.scope.tenant_id}", None),
                            (f"test-owner:{self.scope.tenant_id}:superdan", "superdan")):
            self.repo.configure_budget(name, tenant_id=self.scope.tenant_id, owner_id=owner, limit_microusd=10_000_000)
        self.settings = Settings(self.root, tenant_id=self.scope.tenant_id, auth_mode="local-test",
            database_url=self.url, generation_enabled=True, execution_backend="wangp-worker",
            execution_policy_file=self.policy_path)
        self.app = create_app(self.settings, repository=self.repo)
        self.backend = CPUFixtureBackend(self.paths)
        self.runner = WorkerRunner(self.repo, self.app.state.storage, self.root / "worker", backend=self.backend,
            control=self.control, submission_guard=ExecutionPolicies(self.settings, self.repo).submission_allowed)
        return spec

    def confirm(self, client):
        client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
        client.post("/v1/projects", json={"project": project(self.scope.project_id)}).raise_for_status()
        response = client.post("/v1/generation-plans", json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        plan = response.json()
        self.assertEqual(plan["status"], "ready", plan)
        response = client.post("/v1/jobs", json={"plan_id": plan["plan_id"]},
                               headers={"Idempotency-Key": "cpu-native-once"})
        self.assertEqual(response.status_code, 202, response.text)
        return plan, response.json()["id"]

    def download(self, client, job_id):
        response = client.get("/v1/jobs/"+job_id)
        response.raise_for_status()
        job = response.json()
        self.assertEqual(job["status"], "succeeded", job)
        listed = client.get("/v1/jobs")
        listed.raise_for_status()
        self.assertEqual(next(item for item in listed.json()["jobs"] if item["id"] == job_id), job)
        files = {}
        for artifact in job["artifacts"]:
            response = client.get(artifact["content_url"])
            self.assertEqual(response.status_code, 200)
            path = self.root / (artifact["kind"] + (".mp4" if artifact["kind"] == "video" else ".flac"))
            path.write_bytes(response.content)
            files[artifact["kind"]] = path
        return job, files

    def test_native_delivers_every_numbered_frame_and_audio_tail_through_owned_api(self):
        spec = self.application()
        with TestClient(self.app) as client:
            plan, job_id = self.confirm(client)
            frozen = copy.deepcopy(self.repo.get_job(self.scope, job_id))
            expected = native_delivery_spec(frozen["request"])
            self.assertEqual(plan["execution"]["delivery_spec"], expected)
            self.assertEqual(expected["frame_count"], 124)
            self.assertEqual(expected["duration_s"], 124/24)
            self.assertEqual(_shape(frozen)[2], 5)  # Legacy shape was not globally changed.
            self.assertEqual(self.runner.run_once(spec.worker_id, spec.pool)["state"], "succeeded")
            job, files = self.download(client, job_id)
            self.assertEqual(job["delivery_spec"], expected)
            self.assertEqual(frame_numbers(files["video"]), list(range(124)))
            samples = audio_samples(files["audio"])
            self.assertEqual(len(samples)//2, round(124/24*32000))
            self.assertGreater(max(abs(x) for x in samples[320000:]), 10000)
            self.assertGreater(max(abs(x) for x in audio_samples(files["video"])[320000:]), 9000)
            saved = self.repo.get_job(self.scope, job_id)
            for field in ("request", "request_hash", "execution_plan"):
                self.assertEqual(saved[field], frozen[field])
            self.assertEqual(self.backend.starts, 1)
            self.assertEqual(saved["attempt_no"], 1)
            client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
            self.assertEqual(client.get("/v1/jobs/"+job_id).status_code, 404)
            self.assertEqual(client.get(job["artifacts"][0]["content_url"]).status_code, 404)

    def test_historical_legacy_delivery_still_cuts_124_to_120_without_snapshot_change(self):
        spec = self.application(native=False)
        with TestClient(self.app) as client:
            plan, job_id = self.confirm(client)
            self.assertNotIn("delivery_spec", plan["execution"])
            self.assertEqual(self.runner.run_once(spec.worker_id, spec.pool)["state"], "succeeded")
            job, files = self.download(client, job_id)
            self.assertNotIn("delivery_spec", job)
            self.assertEqual(frame_numbers(files["video"]), list(range(120)))
            self.assertEqual(len(audio_samples(files["audio"]))//2, 160000)
            self.assertEqual(max(abs(x) for x in audio_samples(files["audio"])), 0)

    def test_native_publication_restart_never_fetches_encodes_or_regenerates(self):
        spec = self.application()
        with TestClient(self.app) as client:
            _, job_id = self.confirm(client)
            original = self.app.state.storage.put
            def response_lost(*args, **kwargs):
                original(*args, **kwargs)
                raise StorageWriteUncertain(args[0])
            with patch.object(self.app.state.storage, "put", side_effect=response_lost):
                self.assertEqual(self.runner.run_once(spec.worker_id, spec.pool)["state"], "collecting")
            self.now += 31
            restarted = WorkerRunner(self.repo, self.app.state.storage, self.root / "worker",
                backend=self.backend, control=self.control, submission_guard=lambda _: self.fail("No new admission"))
            with patch.object(self.backend, "submit", side_effect=AssertionError("No regeneration")), \
                    patch.object(self.backend, "fetch", side_effect=AssertionError("No refetch")), \
                    patch("studio_platform.worker.ffmpeg", side_effect=AssertionError("No re-encode")):
                self.assertEqual(restarted.run_once(spec.worker_id, spec.pool)["state"], "succeeded")
            _, files = self.download(client, job_id)
            self.assertEqual(frame_numbers(files["video"])[-1], 123)
            self.assertEqual(self.backend.starts, 1)
            self.assertEqual(self.repo.get_job(self.scope, job_id)["attempt_no"], 1)

    def test_invalid_native_timing_stays_collecting_without_regeneration(self):
        spec = self.application()
        with TestClient(self.app) as client:
            _, job_id = self.confirm(client)
            original = probe
            def bad_timing(path):
                value = original(path)
                if Path(path) == self.paths["video"]:
                    next(s for s in value["streams"] if s.get("codec_type") == "video")["nb_frames"] = "120"
                return value
            with patch("studio_platform.worker.probe", side_effect=bad_timing):
                self.assertEqual(self.runner.run_once(spec.worker_id, spec.pool)["state"], "collecting")
            self.assertEqual(self.backend.starts, 1)
            self.assertEqual(self.repo.list_artifacts(self.scope, job_id), [])
            self.now += 31
            self.assertEqual(self.runner.run_once(spec.worker_id, spec.pool)["state"], "succeeded")
            self.assertEqual(self.backend.starts, 1)

    def test_slot_identity_legacy_hash_and_wrong_exporter_recovery_are_fenced(self):
        legacy = self.spec(False)
        self.assertNotIn("output_delivery", worker_spec_payload(legacy))
        self.control.register(legacy)
        with self.assertRaisesRegex(Conflict, "configuration_output_delivery_conflict"):
            self.control.register(self.spec(configuration_id=legacy.configuration_id))
        native = self.spec()
        worker = self.control.register(native)
        self.assertNotEqual(worker["spec_hash"], request_hash(worker_spec_payload(replace(native, output_delivery=""))))
        with self.assertRaisesRegex(Conflict, "recovery_worker_binding_required"):
            self.control.require_recovery_binding(replace(native, output_delivery=""))
        job = {"pool": native.pool, "request": self.compiled, "execution_plan": {
            "enabled": True, "backend": native.backend, "configuration_id": native.configuration_id,
            "engine_manifest_digest": native.engine_manifest_digest, "output_delivery": NATIVE_DELIVERY}}
        self.assertTrue(self.control.matches(worker, job))
        stale = {**worker, "spec": worker_spec_payload(replace(native, output_delivery=""))}
        self.assertFalse(self.control.matches(stale, job))
        self.assertEqual(self.control.pool_status(native.pool, model_id=native.model_id,
            configuration_id=native.configuration_id, backend=native.backend,
            engine_manifest_digest=native.engine_manifest_digest)["matched_slots"], 0)

    def test_frozen_contract_rejects_unknown_or_changed_values(self):
        execution = {"backend": "wangp-worker", "output_delivery": NATIVE_DELIVERY,
                     "delivery_spec": native_delivery_spec(self.compiled)}
        job = {"request": self.compiled, "execution_plan": execution}
        self.assertEqual(delivery_spec(job)["frame_count"], 124)
        for changed in ({"output_delivery": "unknown"}, {"output_delivery": None},
                {"backend": "comfy-worker"}, {"delivery_spec": {**execution["delivery_spec"], "frame_count": 120}}):
            with self.subTest(changed=changed), self.assertRaises(BackendError):
                delivery_spec({**job, "execution_plan": {**execution, **changed}})
        self.assertIsNone(delivery_spec({"request": self.compiled, "execution_plan": {}}))
        value = policy(self.now)
        for invalid in (NATIVE_DELIVERY, "unknown", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_policy({**value, "output_delivery": invalid})


if __name__ == "__main__":
    unittest.main()
