"""Quick Chat -> current WanGP policy/ledger, no runtime or network calls."""
import copy
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from studio_platform.api import create_app
from studio_platform.capabilities import VERSION
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.inference.outputs import NATIVE_DELIVERY
from studio_platform.repository import Conflict, Scope, jobs
from studio_platform.settings import Settings
from test_platform_execution_policy import policy
from test_platform_repository import LedgerCase


class QuickChatWanGPTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.root = Path(self.temp.name)
        value = policy(self.now)
        value.update(backend="wangp-worker", engine_manifest_digest="a"*64,
                     recipe_ids=["h3-base-fl2va-v1"], output_delivery=NATIVE_DELIVERY)
        value["envelope"]["controls"] = {"sampler_name": ["euler"], "scheduler": ["auto"],
            "video_decode": ["tiled"], "audio_decode": ["normal"], "encoder_device": ["default"]}
        path = self.root/"synthetic-policy.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        self.control = WorkerControl(self.repo)
        self.control.register(WorkerSpec("synthetic-slot", "synthetic-pool", "test-only", "test-instance",
            ("test-gpu",), ("h3-base-fl2va-v1",), "MiniMax-H3-Base-BF16", "synthetic-config",
            "wangp-worker", engine_manifest_digest="a"*64, output_delivery=NATIVE_DELIVERY))
        self.control.mark_ready("synthetic-slot", upstream_idle_confirmed=True)
        for name, owner in (("test-tenant:sixnine", None), ("test-owner:sixnine:superdan", "superdan")):
            self.repo.configure_budget(name, tenant_id="sixnine", owner_id=owner, limit_microusd=10_000_000)
        self.app = create_app(Settings(self.root/"data", auth_mode="local-test", database_url=self.url,
            generation_enabled=True, execution_backend="wangp-worker", execution_policy_file=path), repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
        self.session = self.write("/v1/quick-chat/sessions", {}, "session")["session"]
        self.base = "/v1/quick-chat/sessions/"+self.session["id"]

    def write(self, path, body, key):
        response = self.client.post(path, json=body, headers={"Idempotency-Key": key})
        self.assertLess(response.status_code, 300, response.text)
        return response.json()

    def card(self, controls, key):
        return self.write(self.base+"/cards", {"recipe_id": "h3-base-fl2va-v1",
            "prompt": "Synthetic policy fixture", "controls": controls, "inputs": {}, "copies": 1}, key)["revision"]

    def preflight(self, revision, key):
        return self.write(self.base+"/revisions/"+revision["id"]+"/preflights",
            {"capabilities_version": VERSION, "revision_hash": revision["input_hash"]}, key)

    def test_current_engine_defaults_and_native_delivery_reach_original_job(self):
        revision = self.card({"duration": 5, "resolution": "480P"}, "minimal-card")
        plan = self.preflight(revision, "preflight")
        self.assertEqual(plan["status"], "ready", plan)
        public = plan["items"][0]["plan"]
        effective = public["effective_request"]
        self.assertEqual(effective["backend"], "wangp-local")
        self.assertEqual(effective["model"], "MiniMax-H3-Base-BF16")
        self.assertEqual(effective["steps"], 50)
        self.assertEqual(effective["sampler_name"], "euler")
        self.assertEqual(effective["video_tile_size"], 256)
        self.assertEqual(public["execution"]["delivery_spec"]["policy"], NATIVE_DELIVERY)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])
        submission = self.write(self.base+"/revisions/"+revision["id"]+"/submissions",
            {"confirmed": True, "preflight_id": plan["id"], "revision_hash": revision["input_hash"]}, "confirm")
        job = submission["items"][0]["job"]
        frozen = self.repo.get_job_for_owner("sixnine", "superdan", job["id"])
        self.assertEqual(frozen["execution_plan"]["backend"], "wangp-worker")
        self.assertEqual(frozen["execution_plan"]["engine_manifest_digest"], "a"*64)
        self.assertEqual(job["delivery_spec"], public["execution"]["delivery_spec"])
        self.assertEqual(job["effective_request"], effective)
        self.assertEqual(self.client.get("/v1/jobs/"+job["id"]).json()["delivery_spec"], job["delivery_spec"])

    def test_explicit_legacy_controls_remain_saved_and_preflight_rejects_them(self):
        for index, controls in enumerate(({"steps": 20}, {"sampler_name": "res_multistep"},
                {"video_decode": "normal"}, {"ref_image_size": "max"}, {"video_temporal_size": 128})):
            with self.subTest(controls=controls):
                revision = self.card(controls, "legacy-"+str(index))
                before = copy.deepcopy(revision)
                result = self.preflight(revision, "reject-"+str(index))
                self.assertEqual(result["status"], "blocked", result)
                self.assertIsNone(result["items"][0]["plan"])
                self.assertEqual(self.client.get(self.base+"/revisions/"+revision["id"]).json(), before)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])

    def test_inert_plan_refresh_cannot_rebind_existing_configuration_delivery(self):
        revision = self.card({"duration": 5, "resolution": "480P"}, "refresh-card")
        public = self.preflight(revision, "refresh-preflight")["items"][0]["plan"]
        # Build only an inert planned execution; no queue reservation or attempt.
        principal = self.app.state.auth.session(self.client.cookies["sixnine_session"])
        admission = self.app.state.generation_admission
        job = admission.create_planned(principal, public["plan_id"], "inert-execution")
        scope = Scope("sixnine", "superdan", job["project_id"], "quick-chat-execution")
        plan = self.app.state.owned_plan(principal, public["plan_id"])
        changed = copy.deepcopy(plan["execution_plan"])
        changed.pop("output_delivery")
        changed.pop("delivery_spec")
        fresh = self.repo.create_plan(scope, plan["request"], changed,
            expires_at=self.now+600, estimated_cost_microusd=plan["estimated_cost_microusd"])
        with self.assertRaisesRegex(Conflict, "configuration_output_delivery_conflict"):
            self.repo.refresh_unadmitted_plan(scope, job["id"], fresh["id"])
        self.assertEqual(self.repo.get_job(scope, job["id"])["plan_id"], plan["id"])
