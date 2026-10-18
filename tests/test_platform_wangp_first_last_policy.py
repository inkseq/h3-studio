"""Owned CPU image uploads and admission only; no provider or inference call."""
import copy
from dataclasses import replace
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from studio_platform.api import create_app
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.inference.outputs import NATIVE_DELIVERY
from studio_platform.inference.wangp_compiler import compile_settings
from studio_platform.production_scaler import MODEL, RECIPE, ScalerError, verify_policy
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE, MULTIMODAL_INPUT_LIMITS
from studio_platform.repository import Scope, attempts, instance_intents, request_hash
from studio_platform.settings import Settings
from test_platform_api import generation_request, png, project
from test_platform_execution_policy import policy
from test_platform_production_scaler import configuration
from test_platform_repository import LedgerCase


DIGEST = "a"*64


class WanGPFirstLastPolicyTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.repo.engine.dispose)
        root = Path(self.temp.name)
        self.scope = Scope("sixnine", "superdan", "story-one")
        base = configuration(root, self.now)
        self.config = replace(base, execution_backend="wangp-worker", engine_manifest_digest=DIGEST,
            qualification_profile=QUEUED_TASK_PROFILE, output_delivery=NATIVE_DELIVERY,
            source_sha256={name: "b"*64 for name in
                ("wangp-bootstrap.py", "wangp-manifest.json", "wangp-runtime.json", "wangp-package.tar.gz")})
        self.value = policy(self.now)
        self.value.update(backend="wangp-worker", engine_manifest_digest=DIGEST, output_delivery=NATIVE_DELIVERY,
            pool=self.config.pool, configuration_id=self.config.configuration_id, recipe_ids=[RECIPE],
            budget_accounts=["fl-job-budget"])
        self.value["qualification"].update(status="runtime_required", profile=QUEUED_TASK_PROFILE,
            evidence_id=self.config.qualification_evidence_id, expires_at=self.now+7000)
        self.value["reservation"].update(expected_runtime_s=1800, expires_at=self.now+7000)
        self.value["envelope"].update(max_duration_seconds=362/24, max_reference_files=2, max_guides=0,
            allow_first_last=True, input_limits={**MULTIMODAL_INPUT_LIMITS,
                "max_images": 0, "max_videos": 0, "max_audios": 0,
                "guide_kinds": [], "guide_recipe_ids": [], "allow_video_audio": False})
        self.value["envelope"]["controls"] = {"sampler_name": ["euler"], "scheduler": ["auto"],
            "video_decode": ["tiled"], "audio_decode": ["normal"], "encoder_device": ["default"]}
        self.path = root/"policy.json"
        self.settings = Settings(root/"data", tenant_id="sixnine", database_url=self.url, auth_mode="local-test",
            generation_enabled=True, execution_backend="wangp-worker", execution_policy_file=self.path)
        self.write()
        self.repo.configure_budget("fl-job-budget", tenant_id="sixnine", owner_id="superdan", limit_microusd=10_000_000)
        control = WorkerControl(self.repo)
        spec = WorkerSpec("fixture-only", self.config.pool, "test-only", "fixture-instance", ("fixture-gpu",),
            (RECIPE,), MODEL, self.config.configuration_id, "wangp-worker", DIGEST, output_delivery=NATIVE_DELIVERY)
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.app = create_app(self.settings, repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
        self.client.post("/v1/projects", json={"project": project()}).raise_for_status()

    def write(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        self.path.chmod(0o600)
        self.config = replace(self.config, execution_policy_sha256=request_hash(self.value))

    def upload(self, name):
        response = self.client.post("/v1/assets", data={"client_project_id": "story-one", "client_asset_id": name},
            files={"file": (name+".png", png(), "image/png")})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "ready")
        return response.json()["id"]

    def plan(self, inputs):
        request = generation_request(inputs=inputs)
        request["controls"]["seed"] = "424242"
        return self.client.post("/v1/generation-plans", json=request)

    def test_production_gate_allows_only_explicit_bounded_first_last_envelope(self):
        original = copy.deepcopy(self.value)
        for enabled, maximum in ((False, 0), (True, 0), (True, 1), (True, 2)):
            self.value = copy.deepcopy(original)
            self.value["envelope"].update(allow_first_last=enabled, max_reference_files=maximum)
            self.write()
            self.assertEqual(verify_policy(self.config, self.settings), self.value)
        for patch in ({"allow_first_last": False, "max_reference_files": 1}, {"max_reference_files": 3},
                      {"max_guides": 1}):
            self.value = copy.deepcopy(original)
            self.value["envelope"].update(patch)
            self.write()
            with self.subTest(patch=patch), self.assertRaisesRegex(ScalerError, "outside_wangp_recipe"):
                verify_policy(self.config, self.settings)
        for field, value in (("max_images", 1), ("max_videos", 1), ("max_audios", 1),
                ("guide_kinds", ["image"]), ("guide_recipe_ids", ["h3-base-ref2va-v1"]), ("allow_video_audio", True)):
            self.value = copy.deepcopy(original)
            self.value["envelope"]["input_limits"][field] = value
            self.write()
            with self.subTest(field=field), self.assertRaisesRegex((ScalerError, ValueError),
                    "outside_wangp_recipe|Invalid qualified guide scope"):
                verify_policy(self.config, self.settings)

    def test_owned_first_last_uploads_reach_confirmed_native_job_with_same_image_roles(self):
        self.assertEqual(verify_policy(self.config, self.settings), self.value)
        first, last = self.upload("first"), self.upload("last")
        for inputs, mode in (({"first_frame": first}, "S"), ({"last_frame": last}, "TE"),
                ({"first_frame": first, "last_frame": last}, "SE")):
            with self.subTest(mode=mode):
                response = self.plan(inputs)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()["status"], "ready", response.text)
                confirmed = self.client.post("/v1/jobs", json={"plan_id": response.json()["plan_id"]},
                    headers={"Idempotency-Key": "first-last-"+mode})
                self.assertEqual(confirmed.status_code, 202, confirmed.text)
                job = self.repo.get_job(self.scope, confirmed.json()["id"])
                self.assertEqual(job["status"], "queued")
                self.assertEqual(job["execution_plan"]["output_delivery"], NATIVE_DELIVERY)
                compiled = job["request"]
                self.assertEqual(set(compiled["assets"]), set(inputs.values()))
                metadata = {key: value["metadata"] for key,value in compiled["assets"].items()}
                handles = {key: "opaque-"+key for key in metadata}
                mapped = compile_settings(compiled["request"], metadata, compiled["output_spec"], handles)
                self.assertEqual(mapped["image_prompt_type"], mode)
                self.assertEqual(mapped["image_start"], handles.get(inputs.get("first_frame")))
                self.assertEqual(mapped["image_end"], handles.get(inputs.get("last_frame")))
                self.assertIsNone(mapped["image_refs"])
                self.assertEqual(job["execution_plan"]["delivery_spec"]["frame_count"], 124)
        with self.repo.engine.connect() as conn:
            self.assertEqual(list(conn.execute(select(attempts))), [])
            self.assertEqual(list(conn.execute(select(instance_intents))), [])

    def test_aggregate_pixel_and_disabled_first_last_limits_still_block_owned_uploads(self):
        first, last = self.upload("first"), self.upload("last")
        for patch, inputs in (({"max_reference_files": 1}, {"first_frame": first, "last_frame": last}),
                ({"max_reference_files": 0}, {"first_frame": first}),
                ({"allow_first_last": False, "max_reference_files": 0}, {"last_frame": last})):
            self.value["envelope"].update(patch)
            self.write()
            verify_policy(self.config, self.settings)
            response = self.plan(inputs)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["status"], "blocked", response.text)
        self.value["envelope"].update(max_reference_files=2, allow_first_last=True)
        self.value["envelope"]["input_limits"]["max_image_pixels"] = 256*256
        self.write()
        verify_policy(self.config, self.settings)
        response = self.plan({"first_frame": first})
        self.assertEqual(response.json()["status"], "blocked", response.text)
        self.assertEqual(self.repo.list_jobs(self.scope), [])

    def test_ordinary_reference_inputs_and_foreign_owned_images_remain_rejected(self):
        first = self.upload("first")
        for kind in ("images", "videos", "audios"):
            response = self.plan({kind: [first]})
            self.assertEqual(response.status_code, 422, response.text)
        mixed = self.plan({"first_frame": first, "images": [first]})
        self.assertEqual(mixed.status_code, 422, mixed.text)
        self.client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
        self.client.post("/v1/projects", json={"project": project()}).raise_for_status()
        self.assertEqual(self.plan({"first_frame": first}).status_code, 404)
        self.assertEqual(self.plan({"last_frame": first}).status_code, 404)
        self.assertEqual(self.repo.list_jobs(self.scope), [])
