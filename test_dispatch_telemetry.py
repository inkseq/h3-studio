"""Closed host telemetry survives direct queue transport migration."""
import copy
import json
import uuid
import unittest
from unittest.mock import patch
from sqlalchemy import insert, update
from studio_platform.dispatch_telemetry import _host_telemetry_context
from studio_platform.operator_capacity import operator_commands, operator_nodes
from test_platform_repository import LedgerCase

class HostContextTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.repo.configure_pool("host-context-test", max_instances=2, max_physical_gpus=2)
        self.node = self.repo.reserve_instance_intent(self.scope, "host-context-test", "host-context-one",
            reserved_cost_microusd=100, hard_deadline=self.now+1000,
            budget_account_ids=("owner-budget",), provider="vast", dry_run=False)["id"]
        self.worker = "dstack-" + self.node.replace("-", "")
        self.profile = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
        self.spec = {"backend": "wangp-worker", "provider": "vast", "instance_id": "123",
            "configuration_id": "context-test-native", "engine_manifest_digest": "a"*64}
        host = {"version": 1, "provider": "vastai", "machine_id": "19492", "provider_instance_id": "123",
            "offer_id": "42", "observed_at": self.now, "region": "kr-southkorea", "gpu": "RTX5090",
            "gpu_count": 1, "allocated_ram_mib": 29*1024, "allocated_vram_mib": 32*1024,
            "cpu": 8, "disk_mib": 128*1024, "reliability": .99, "quoted_hourly_microusd": 600000,
            "identity_confirmed": True, "physical_id_source": "vast_machine_id"}
        payload = {"tenant_id": self.scope.tenant_id, "owner_id": self.scope.owner_id,
            "capacity_backend": "dstack-v1", "host_observation": host,
            "private_endpoint": "SECRET private endpoint", "dstack": {"backend": "vastai",
                "profile_id": self.profile, "mode": "fl", "provider_instance_id": "123",
                "configuration_id": "context-test-native", "manifest_digest": "a"*64,
                "run_name": "sixnine-" + self.node.replace("-", ""),
                "observed_at": self.now, "runtime_incarnation": "c"*32,
                "preparation": {"model": {"state": "loaded"}}}}
        command = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(insert(operator_commands).values(id=command, actor="test-operator",
                idempotency_key="context-original-key", request_hash="a"*64, kind="dstack_start",
                state="accepted", payload={}, created_at=self.now, updated_at=self.now))
            connection.execute(insert(operator_nodes).values(intent_id=self.node, command_id=command,
                ordinal=0, binding_id="test-original-binding", binding_hash="a"*64, payload=payload,
                desired_state="running", runtime_state="ready", updated_at=self.now))
        self.job_id = self.context_job(self.scope, "original-context-job")

    def context_job(self, scope, key):
        plan = self.repo.create_plan(scope, {"deployment_profile_id": self.profile,
            "request": {"mode": "fl", "prompt": "SECRET private prompt"}},
            {"configuration_id": "context-test-native"}, expires_at=self.now+1000,
            estimated_cost_microusd=0)
        return self.repo.create_job(scope, plan["id"], key)["id"]

    def context(self, **overrides):
        return _host_telemetry_context(self.repo, **{"node_id": self.node,
            "worker_id": self.worker, "spec": self.spec, "job_id": self.job_id, **overrides})

    def test_exact_host_context_is_closed_and_preserves_billing(self):
        budget = self.repo.get_budget("owner-budget")
        value = self.context()
        self.assertEqual(value["machine_id"], "19492")
        self.assertEqual(value["host_identity_source"], "vast_machine_id")
        self.assertEqual(value["quoted_hourly_microusd"], 600000)
        self.assertEqual(value["gpu_type"], "rtx5090")
        self.assertEqual(value["warmth"], "warm")
        self.assertNotIn("SECRET", json.dumps(value))
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_foreign_owner_slot_configuration_or_instance_never_gets_host_context(self):
        foreign = self.context_job(self.other, "foreign-context-job")
        self.assertEqual(self.context(job_id=foreign), {})
        self.assertEqual(self.context(worker_id="different-worker"), {})
        for field, value in (("instance_id", "456"), ("configuration_id", "changed"),
                ("engine_manifest_digest", "b"*64), ("provider", "runpod")):
            self.assertEqual(self.context(spec={**self.spec, field: value}), {})
        self.assertEqual(self.context(node_id="missing-or-unknown"), {})

    def test_stale_model_observation_is_unknown_not_warm_and_read_failure_is_failsoft(self):
        self.now += 31
        self.assertEqual(self.context()["warmth"], "unknown")
        with patch.object(self.repo.engine, "connect", side_effect=RuntimeError("SECRET database detail")):
            self.assertEqual(self.context(), {})


if __name__ == "__main__":
    unittest.main()
