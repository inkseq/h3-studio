"""Dispatch readiness and admission use the same immutable route; no cloud calls."""
from dataclasses import replace
import json
from pathlib import Path
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies, validate_policy
from studio_platform.settings import Settings
from studio_platform.repository import Scope
from studio_platform.capabilities import compile_request
from test_platform_repository import LedgerCase
from test_platform_execution_policy import policy
from test_platform_api import generation_request

class DispatchPolicyTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.repo.configure_capacity(max_instances=3, max_physical_gpus=3)
        self.value = policy(self.now)
        self.value.update(backend="wangp-worker", engine_manifest_digest="a"*64,
            recipe_ids=["h3-base-fl2va-v1"], budget_accounts=["owner-budget"])
        self.value["envelope"]["controls"].pop("ref_image_size")
        self.path = Path(self.temp.name)/"dispatch-policy.json"
        self.settings = Settings(Path(self.temp.name)/"data", auth_mode="local-test",
            generation_enabled=True, execution_backend="wangp-worker", execution_policy_file=self.path)
    def write(self):
        self.path.write_text(json.dumps(self.value),encoding="utf-8")
        self.path.chmod(0o600)
    def ready(self, name, route):
        spec=WorkerSpec(name,"synthetic-pool","test-only","instance-"+name,("gpu",),
            ("h3-base-fl2va-v1",),self.value["model_id"],"synthetic-config","wangp-worker",
            engine_manifest_digest="a"*64,dispatch_backend=route)
        self.control.register(spec)
        self.control.mark_ready(name,upstream_idle_confirmed=True)
    def evaluate(self):
        compiled,fingerprint=compile_request(generation_request(),lambda _:None)
        return ExecutionPolicies(self.settings,self.repo).evaluate(compiled,self.scope,fingerprint)
    def test_policy_freezes_hatchet_and_legacy_shape_remains_unchanged(self):
        self.ready("hatchet","hatchet-v1")
        self.value["dispatch_backend"]="hatchet-v1"
        self.write()
        result=self.evaluate()
        self.assertTrue(result.execution["enabled"],result.execution["blockers"])
        self.assertEqual(result.execution["dispatch_backend"],"hatchet-v1")
        self.ready("legacy","legacy")
        del self.value["dispatch_backend"]
        self.write()
        result=self.evaluate()
        self.assertTrue(result.execution["enabled"],result.execution["blockers"])
        self.assertNotIn("dispatch_backend",result.execution)
    def test_legacy_ready_cannot_admit_hatchet_job(self):
        self.ready("legacy","legacy")
        self.value["dispatch_backend"]="hatchet-v1"
        self.write()
        result=self.evaluate()
        self.assertFalse(result.execution["enabled"])
        self.assertEqual(result.execution["registered_healthy_slots"],0)
    def test_pool_status_separates_identically_configured_routes(self):
        self.ready("legacy","legacy");self.ready("hatchet","hatchet-v1")
        args=dict(model_id=self.value["model_id"],configuration_id="synthetic-config",
            backend="wangp-worker",engine_manifest_digest="a"*64,recipe_id="h3-base-fl2va-v1")
        self.assertEqual(self.control.pool_status("synthetic-pool",**args)["ready"],1)
        self.assertEqual(self.control.pool_status("synthetic-pool",dispatch_backend="hatchet-v1",**args)["ready"],1)
    def test_hatchet_is_not_accepted_for_other_engine_or_unknown_route(self):
        self.value["dispatch_backend"]="hatchet-v1"
        validate_policy(self.value)
        self.value["backend"]="comfy-worker"
        with self.assertRaises(ValueError):validate_policy(self.value)
        self.value["backend"]="wangp-worker";self.value["dispatch_backend"]="typo"
        with self.assertRaises(ValueError):validate_policy(self.value)
