"""Public API -> WanGP boundary -> downloadable CPU fixture, NOT H3 inference."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.inference.wangp import WanGPBackend
from studio_platform.inference.wangp_contract import PreparedRequest, RuntimeObservation, RuntimeOutput, canonical_json
from studio_platform.runtime_hosts.wangp import WanGPHost
from studio_platform.runtime_hosts.wangp_receipts import ReceiptJournal
from studio_platform.settings import Settings
from studio_platform.worker import WorkerRunner
from test_platform_api import project, generation_request
from test_platform_execution_policy import policy
from test_platform_wangp_host import FakeSession
from test_platform_wangp_receipts import manifest
from test_platform_repository import LedgerCase


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU media utilities required")
class WanGPAPITests(LedgerCase):
    def test_saved_draft_empty_regions_work_but_unsupported_controls_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Settings(Path(temporary), auth_mode="local-test",
                database_url=self.url, execution_backend="wangp-worker"), repository=self.repo)
            with TestClient(app) as client:
                client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
                for index, controls in enumerate(({}, {"video_temporal_size": 128}, {"ref_image_size": "max"})):
                    with self.subTest(controls=controls):
                        ident = "draft-" + str(index)
                        client.post("/v1/projects", json={"project": project(ident)}).raise_for_status()
                        edit = client.post(f"/v1/projects/{ident}/actions", json={
                            "expected_version": 1, "actions": [{"op": "shot.configure_generation",
                            "shot_id": "shot-one", "prompt": "Synthetic saved draft",
                            "controls": controls}]})
                        self.assertEqual(edit.status_code, 200, edit.text)
                        prefix = f"/v1/projects/{ident}/shots/shot-one"
                        before = client.get(prefix + "/generation-draft").json()
                        self.assertEqual(before["draft"]["inputs"]["guides"], [])
                        result = client.post(prefix + "/generation-plans", json={"expected_version": 2})
                        if controls:
                            self.assertEqual(result.status_code, 422, result.text)
                        else:
                            self.assertEqual(result.status_code, 201, result.text)
                            self.assertEqual(result.json()["effective_request"]["backend"], "wangp-local")
                            self.assertNotIn("guides", result.json()["effective_request"])
                        self.assertEqual(client.get(prefix + "/generation-draft").json(), before)
                # Explicit guide conditioning still fails, even though empty
                # draft regions are accepted. No input resolution is attempted.
                invalid = generation_request("draft-0", inputs={"guides": ["invalid"]})
                self.assertEqual(client.post("/v1/generation-plans", json=invalid).status_code, 422)

    def test_confirm_once_collect_original_and_download_owner_isolated_outputs(self):
        self.now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            execution_file = root / "execution.json"
            settings = Settings(root / "data", auth_mode="local-test", database_url=self.url, generation_enabled=True,
                execution_backend="wangp-worker", execution_policy_file=execution_file)
            app = create_app(settings, repository=self.repo)
            repo = app.state.repository
            try:
                engine = manifest()
                value = policy(repo.clock())
                value.update(backend="wangp-worker", recipe_ids=["h3-base-fl2va-v1"],
                             engine_manifest_digest=engine.digest)
                value["envelope"].update(max_reference_files=0, max_guides=0)
                value["envelope"]["controls"] = {"sampler_name": ["euler"], "scheduler": ["auto"],
                    "video_decode": ["tiled"], "audio_decode": ["normal"], "encoder_device": ["default"]}
                execution_file.write_text(json.dumps(value), encoding="utf-8")
                repo.configure_capacity(max_instances=1, max_physical_gpus=1)
                for ident, owner in (("test-tenant:sixnine", None), ("test-owner:sixnine:superdan", "superdan")):
                    repo.configure_budget(ident, tenant_id="sixnine", owner_id=owner, limit_microusd=2_000_000)
                control = WorkerControl(repo)
                control.register(WorkerSpec("synthetic-slot", "synthetic-pool", "test-only", "test-instance",
                    ("test-gpu",), ("h3-base-fl2va-v1",), "MiniMax-H3-Base-BF16", "synthetic-config",
                    "wangp-worker", engine_manifest_digest=engine.digest))
                control.mark_ready("synthetic-slot", upstream_idle_confirmed=True)
                output = root / "raw"
                output.mkdir()
                video, audio = output / "cpu.mp4", output / "cpu.wav"
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "color=c=navy:s=256x256:r=24", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=32000", "-t", "5.2", "-c:v", "libx264",
                    "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(video)],
                    check=True, capture_output=True, timeout=30)
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=32000", "-t", "5.2", "-ac", "2", str(audio)],
                    check=True, capture_output=True, timeout=30)
                journal = ReceiptJournal(root / "receipts.sqlite", slot_key="slot-1",
                                         manifest_digest=engine.digest, create=True)
                session = FakeSession()
                host = WanGPHost(session=session, journal=journal, manifest=engine,
                                 output_root=output, sealed_root=root / "sealed")
                try:
                    def compiler(job, tag, store, heartbeat):
                        return PreparedRequest(job["id"], tag, job["request_hash"], engine.digest,
                            canonical_json({"synthetic": True}), canonical_json(job["request"]["output_spec"]), True)
                    backend = WanGPBackend(enabled=True, slot_key="slot-1", manifest=engine,
                                           transport=host, compiler=compiler)
                    runner = WorkerRunner(repo, app.state.storage, root / "worker", backend=backend,
                        control=control, submission_guard=ExecutionPolicies(settings, repo).submission_allowed,
                        retry_after_s=0)
                    with TestClient(app) as client:
                        client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
                        client.post("/v1/projects", json={"project": project()}).raise_for_status()
                        body = generation_request(controls={"duration": 5, "resolution": "custom",
                                                            "width": 256, "height": 256, "seed": "123"})
                        plan = client.post("/v1/generation-plans", json=body)
                        self.assertEqual(plan.status_code, 201, plan.text)
                        self.assertEqual(plan.json()["status"], "ready", plan.text)
                        headers = {"Idempotency-Key": "synthetic-confirm-once"}
                        submitted = client.post("/v1/jobs", json={"plan_id": plan.json()["plan_id"]}, headers=headers)
                        self.assertEqual(submitted.status_code, 202, submitted.text)
                        ident = submitted.json()["id"]
                        replay = client.post("/v1/jobs", json={"plan_id": plan.json()["plan_id"]}, headers=headers)
                        self.assertEqual(replay.json()["id"], ident)
                        runner.run_once("synthetic-slot", "synthetic-pool")
                        self.assertEqual(session.calls, 1)
                        session.handle.observation = RuntimeObservation("succeeded", stopped=True,
                            outputs={"video": RuntimeOutput(video, "video/mp4"), "audio": RuntimeOutput(audio, "audio/wav")})
                        for _ in range(4):
                            runner.run_once("synthetic-slot", "synthetic-pool")
                            status = client.get("/v1/jobs/" + ident).json()
                            if status["status"] == "succeeded":
                                break
                        self.assertEqual(status["status"], "succeeded", status)
                        self.assertEqual(session.calls, 1)
                        artifacts = client.get("/v1/jobs/" + ident + "/artifacts").json()["artifacts"]
                        self.assertGreaterEqual(len(artifacts), 2)
                        for item in artifacts:
                            response = client.get("/v1/artifacts/" + item["id"] + "/content")
                            self.assertEqual(response.status_code, 200)
                            self.assertTrue(response.content)
                        client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
                        self.assertEqual(client.get("/v1/jobs/" + ident).status_code, 404)
                        self.assertEqual(client.get("/v1/artifacts/" + artifacts[0]["id"] + "/content").status_code, 404)
                finally:
                    host.close()
            finally:
                repo.close()


if __name__ == "__main__":
    unittest.main()
