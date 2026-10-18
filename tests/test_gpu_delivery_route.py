"""Frozen GPU/CPU export route, exact slot matching and profile gates; no cloud."""
import copy
from dataclasses import replace
import unittest

import test_platform_execution_profiles as fixtures
from studio_platform.control import WorkerControl, WorkerSpec, worker_spec_payload
from studio_platform.execution_policy import ExecutionPolicies, validate_policy
from studio_platform.inference.outputs import (
    GPU_R2_DELIVERY, NATIVE_DELIVERY, delivery_spec, gpu_delivery, native_delivery_spec,
)
from studio_platform.inference.protocol import BackendError
from studio_platform.runtime_catalog import engine_manifest, get_profile, profile_output_delivery

PRUNED = "h3-pruned-rank8-int8-sdpa-p4-29gib-fl-r2-v5"
BASE = "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3"


class FrozenDeliveryRouteTests(unittest.TestCase):
    setUp = fixtures.ExecutionProfileTests.setUp
    write = fixtures.ExecutionProfileTests.write
    compiled = fixtures.ExecutionProfileTests.compiled

    def configure(self):
        for index, profile in enumerate((PRUNED, BASE)):
            value = self.values[index]
            value.update(deployment_profile_id=profile, output_delivery=GPU_R2_DELIVERY,
                model_id=get_profile(profile)["model_id"],
                engine_manifest_digest=engine_manifest(profile, "fl").digest)
        self.write()

    def register(self, index, route=GPU_R2_DELIVERY):
        value = self.values[index]
        spec = WorkerSpec(f"gpu-worker-{index}", value["pool"], "test-only", f"gpu-instance-{index}",
            (f"gpu-{index}",), tuple(value["recipe_ids"]), value["model_id"], value["configuration_id"],
            "wangp-worker", value["engine_manifest_digest"], output_delivery=route)
        control = WorkerControl(self.repo)
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        return spec

    def test_admission_freezes_new_route_and_later_rollout_change_does_not_rewrite_it(self):
        self.configure()
        self.register(0)
        compiled, fingerprint = self.compiled(PRUNED)
        decision = ExecutionPolicies(self.settings, self.repo).evaluate(compiled, self.scope, fingerprint)
        self.assertTrue(decision.execution["enabled"], decision.execution)
        frozen = copy.deepcopy(decision.execution)
        job = {"request": compiled, "execution_plan": frozen}
        self.assertTrue(gpu_delivery(job))
        self.assertEqual(delivery_spec(job)["frame_count"], 124)
        self.values[0]["enabled"] = False
        self.write()
        self.assertEqual(job["execution_plan"], frozen)
        self.assertTrue(gpu_delivery(job))
        self.assertEqual(job["execution_plan"]["delivery_spec"]["policy"], GPU_R2_DELIVERY)

    def test_cpu_and_gpu_delivery_slots_never_match_each_others_accepted_work(self):
        self.configure()
        gpu = self.register(0)
        cpu = replace(gpu, worker_id="cpu-only", output_delivery=NATIVE_DELIVERY)
        worker_gpu, worker_cpu = {"spec": worker_spec_payload(gpu)}, {"spec": worker_spec_payload(cpu)}
        base = {"pool": gpu.pool, "backend": "wangp-worker", "enabled": True,
            "model_id": gpu.model_id, "configuration_id": gpu.configuration_id,
            "engine_manifest_digest": gpu.engine_manifest_digest, "output_delivery": GPU_R2_DELIVERY}
        job = {"pool": gpu.pool, "request": {"recipe_id": "h3-base-fl2va-v1", "model": gpu.model_id}, "execution_plan": base}
        self.assertTrue(WorkerControl.matches(worker_gpu, job))
        self.assertFalse(WorkerControl.matches(worker_cpu, job))
        old = {**job, "execution_plan": {**base, "output_delivery": NATIVE_DELIVERY}}
        self.assertTrue(WorkerControl.matches(worker_cpu, old))
        self.assertFalse(WorkerControl.matches(worker_gpu, old))

    def test_historical_contract_retains_native_and_missing_routes(self):
        compiled, _ = self.compiled()
        old = {"request": compiled, "execution_plan": {"backend": "wangp-worker",
            "output_delivery": NATIVE_DELIVERY, "delivery_spec": native_delivery_spec(compiled)}}
        self.assertFalse(gpu_delivery(old))
        self.assertEqual(delivery_spec(old)["policy"], NATIVE_DELIVERY)
        self.assertFalse(gpu_delivery({"request": compiled, "execution_plan": {}}))
        changed = {**old, "execution_plan": {**old["execution_plan"], "output_delivery": GPU_R2_DELIVERY}}
        with self.assertRaisesRegex(BackendError, "invalid_output_delivery_contract"):
            delivery_spec(changed)

    def test_rollout_policy_requires_exact_appended_profile_delivery_and_manifest(self):
        self.configure()
        self.assertEqual(validate_policy(self.values[0])["output_delivery"], GPU_R2_DELIVERY)
        for change in ({"output_delivery": NATIVE_DELIVERY},
                {"engine_manifest_digest": "a" * 64}, {"deployment_profile_id": fixtures.PRUNED}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_policy({**self.values[0], **change})
        no_profile = {key: value for key, value in self.values[0].items() if key != "deployment_profile_id"}
        with self.assertRaises(ValueError):
            validate_policy(no_profile)
        self.assertNotIn("output_delivery", engine_manifest(fixtures.PRUNED, "fl").document)
        self.assertEqual(profile_output_delivery(fixtures.PRUNED), NATIVE_DELIVERY)
        for profile in (PRUNED, BASE):
            self.assertEqual(engine_manifest(profile, "fl").document["output_delivery"], GPU_R2_DELIVERY)
            with self.assertRaises(ValueError):
                engine_manifest(profile, "ref")


if __name__ == "__main__":
    unittest.main()
