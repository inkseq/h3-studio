"""Enhance plan -> job -> WorkerRunner -> RunPod (fake) -> collector -> artifact (#795).

Real ffmpeg output and the real collector, artifact writer and repository; only
the RunPod API is faked. The fake /run writes the output where the presigned
PUT points, as the RunPod worker would.
"""

import io
import json
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from studio_platform.enhance_backend import Config, RunPodEnhanceBackend
from studio_platform.enhance_plans import (
    BACKEND,
    CONFIGURATION,
    POOL,
    compile_enhance,
)
from studio_platform.repository import Repository, Scope
from studio_platform.storage import LocalObjectStore, SecretURL
from studio_platform.worker import WorkerRunner
from tests.test_enhance_plans import PROJECT, body, source


class PresignedStore(LocalObjectStore):
    def presign_download(self, key, *, expires_seconds=300, download_filename=None):
        return SecretURL("https://r2.test/" + key, "GET", 0, {})

    def presign_worker_upload(self, key, *, content_type, expires_seconds=300):
        return SecretURL("https://r2.test/" + key, "PUT", 0, {})


def enhanced_mp4(path, size):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24"]
        + ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        + ["-frames:v", "124", "-t", str(124 / 24), "-c:v", "libx264"]
        + ["-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
        timeout=120,
    )
    return path.read_bytes()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg")
class EnhanceFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        network = mock.patch.object(
            socket.socket, "connect", side_effect=AssertionError("no network")
        )
        network.start()
        self.addCleanup(network.stop)
        self.store = PresignedStore(self.root / "objects")
        self.repo = Repository(
            "sqlite:///" + str(self.root / "db.sqlite3").replace("\\", "/")
        )
        self.addCleanup(self.repo.close)
        self.repo.create_schema()

    def run_job(self, tier, size, statuses):
        draft = self.store.put("owners/o/assets/a/take.mp4", io.BytesIO(b"draft"))
        stored = {"key": draft.key, "size_bytes": draft.size_bytes, "sha256": None}
        compiled, display = compile_enhance(
            body(tier), PROJECT, lambda e, k: source(object=stored)
        )
        scope = Scope("tenant", "owner", "project-1")
        plan = self.repo.create_plan(
            scope,
            compiled,
            {
                "pool": POOL,
                "backend": BACKEND,
                "configuration_id": CONFIGURATION,
                "enabled": True,
                "expected_runtime_s": 900,
            },
            expires_at=self.repo.clock() + 900,
            estimated_cost_microusd=0,
        )
        job = self.repo.create_job(scope, plan["id"], "enhance-job")
        output = enhanced_mp4(self.root / "enhanced.mp4", size)
        calls = []

        def runpod(request):
            path = request.url.path.rsplit("/", 2)
            calls.append(request.url.path)
            if path[-1] == "run":
                sent = json.loads(request.content)["input"]
                key = sent["output_put_url"].removeprefix("https://r2.test/")
                self.store.put(key, io.BytesIO(output), content_type="video/mp4")
                return httpx.Response(200, json={"id": "rp-1", "status": "IN_QUEUE"})
            return httpx.Response(200, json={"id": "rp-1", **statuses.pop(0)})

        backend = RunPodEnhanceBackend(
            self.store, "/unused", transport=httpx.MockTransport(runpod)
        )
        backend._config = Config("key", "endpoint1", 1000)
        runner = WorkerRunner(
            self.repo,
            self.store,
            self.root / "worker",
            backend=backend,
            submission_guard=lambda job: True,
        )
        for _ in range(6):
            state = runner.run_once("enhance-worker", POOL)["state"]
            current = self.repo.get_job(scope, job["id"])
            if current["status"] in ("succeeded", "failed", "cancelled"):
                break
            self.repo.clock = (lambda now: lambda: now)(self.repo.clock() + 60)
        return current, display, calls, state

    def test_1080p_take_is_enhanced_validated_and_stored_with_audio(self):
        done = {
            "status": "COMPLETED",
            "output": {"uploaded": True, "seconds": 61.0},
            "delayTime": 30_000,
            "executionTime": 62_000,
        }
        job, display, calls, _ = self.run_job(
            "1080p", "1920x1080", [{"status": "IN_PROGRESS"}, done, done]
        )
        self.assertEqual(job["status"], "succeeded", job.get("error_code"))
        self.assertEqual(display["credits"], 100)
        self.assertEqual(calls.count("/v2/endpoint1/run"), 1)
        self.assertEqual(job["result"]["actual_cost_microusd"], 92_000)
        scope = Scope("tenant", "owner", "project-1")
        artifacts = self.repo.list_artifacts(scope, job["id"])
        meta = {a["metadata"]["kind"]: a["metadata"] for a in artifacts}
        self.assertEqual(set(meta), {"video", "audio"})
        video = meta["video"]
        self.assertTrue(video["validated"] and video["has_audio"])
        self.assertEqual(
            (video["width"], video["height"], video["fps"]), (1920, 1080, 24)
        )
        self.assertAlmostEqual(video["duration_s"], 124 / 24, places=3)

    def test_wrong_size_output_fails_without_a_second_run(self):
        done = {"status": "COMPLETED", "output": {"uploaded": True}}
        job, _, calls, _ = self.run_job("2k", "1920x1080", [done] * 4)
        self.assertNotEqual(job["status"], "succeeded")
        self.assertEqual(calls.count("/v2/endpoint1/run"), 1)


if __name__ == "__main__":
    unittest.main()
