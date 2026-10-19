"""Historical ledger/API contracts with direct fixtures; no rental/controller calls."""

from dataclasses import asdict, replace
import json
import io
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update, insert

from studio_platform.auth import Principal
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.operator_capacity import (
    DeploymentBinding,
    OperatorCapacity,
    OperatorError,
    OperatorRegistry,
    operator_commands,
    operator_nodes,
    selection,
    operator_inventory,
    operator_heartbeats,
    public_bootstrap,
)
from studio_platform.operator_routes import register_routes
from studio_platform.repository import (
    instance_intents,
    jobs,
    registered_workers,
    scaler_actions,
    scaler_receipts,
)
from studio_platform.capacity_types import LaunchSpec
from test_platform_repository import LedgerCase


class OperatorCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.actor = Principal("superdan", "browser", auth_mode="password")
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id, operator_capacity_owners=("superdan",)
        )
        self.binding = DeploymentBinding(
            binding_id="test-bf16",
            runtime_profile_id="h3-test-bf16",
            gpu_type="NVIDIA RTX PRO 6000 Blackwell",
            gpu_count=1,
            pool="operator-test",
            configuration_id="test-config",
            model_id="test-h3",
            recipe_ids=("h3-base-fl2va-v1",),
            engine_manifest_digest="a" * 64,
            launch=LaunchSpec("lium", "test-config", "test-h3"),
            scope=self.scope,
            budget_account_ids=("owner-budget",),
            hourly_cost_microusd=360_000,
            reservation_per_node_microusd=1_000_000,
            expires_at=10_000,
            enabled=True,
            boot={"source_dir": "/private/test-only"},
        )
        self.registry = OperatorRegistry([self.binding])
        self.service = OperatorCapacity(self.repo, self.settings, self.registry)
        self.repo.configure_pool(
            "operator-test", max_instances=10, max_physical_gpus=10
        )
        self.seeded_instances = []
        self.policy = {
            "expected_version": 0,
            "enabled": True,
            "max_instances": 4,
            "max_physical_gpus": 4,
            "max_hourly_cost_microusd": 2_000_000,
            "idle_shutdown_seconds": 600,
            "max_ttl_seconds": 3600,
        }
        self.service.update_policy(self.actor, self.policy)
        self.chosen = {
            "runtime_profile_id": self.binding.runtime_profile_id,
            "gpu_type": self.binding.gpu_type,
            "mode": "fl",
            "node_count": 1,
            "gpu_count": 1,
            "ttl_seconds": 3600,
        }

    def create(self, *, chosen=None, key="start-one"):
        preview = self.service.preview(self.actor, chosen or self.chosen)
        self.assertTrue(preview["can_start"], preview["blockers"])
        return self.service.start(
            self.actor, {"preview_id": preview["preview_id"]}, key
        )["operation"]

    def seed_running(self, *, command_id=None, ready=True):
        """Seed historical accepted nodes without any retired rental/controller code."""
        with self.repo.engine.connect() as conn:
            commands = [
                dict(row)
                for row in conn.execute(
                    select(operator_commands).where(operator_commands.c.kind == "start")
                ).mappings()
                if command_id is None or row["id"] == command_id
            ]
        for command in commands:
            payload = command["payload"]
            binding = self.registry.get(payload["binding_id"])
            chosen = payload["selection"]
            for ordinal in range(chosen["node_count"]):
                intent = self.repo.reserve_instance_intent(
                    binding.scope,
                    binding.pool,
                    "operator-" + command["id"] + "-" + str(ordinal),
                    physical_gpus=binding.gpu_count,
                    slots=binding.execution_slots,
                    reserved_cost_microusd=binding.reservation_per_node_microusd,
                    hard_deadline=payload["hard_deadline"],
                    budget_account_ids=binding.budget_account_ids,
                    dry_run=False,
                    provider=binding.launch.provider,
                )
                if not intent["created"]:
                    continue
                self.repo.update_instance(intent["id"], "creating")
                instance_id = "fake-" + intent["id"]
                intent = self.repo.update_instance(
                    intent["id"], "starting", provider_instance_id=instance_id
                )
                with self.repo.transaction() as conn:
                    conn.execute(
                        insert(scaler_actions).values(
                            intent_id=intent["id"],
                            pool=binding.pool,
                            launch_spec=asdict(binding.launch),
                            create_started_at=self.now,
                            last_observed_at=self.now,
                            last_observation={
                                "state": "running",
                                "instance_id": instance_id,
                            },
                        )
                    )
                    conn.execute(
                        insert(operator_nodes).values(
                            intent_id=intent["id"],
                            command_id=command["id"],
                            ordinal=ordinal,
                            binding_id=binding.binding_id,
                            binding_hash=binding.fingerprint,
                            payload={
                                "selection": chosen,
                                "hourly_cost_microusd": binding.hourly_cost_microusd,
                                "lifetime": {
                                    "state": "verified",
                                    "instance_id": instance_id,
                                    "safe_deadline": intent["hard_deadline"],
                                    "observed_at": self.now,
                                },
                            },
                            desired_state="running",
                            runtime_state="ready" if ready else "waiting_provider",
                            updated_at=self.now,
                        )
                    )
                self.seeded_instances.append(intent["id"])
                if ready:
                    control = WorkerControl(self.repo)
                    for ordinal in range(binding.execution_slots):
                        spec = WorkerSpec(
                            "worker-" + intent["id"] + "-" + str(ordinal),
                            binding.pool,
                            binding.launch.provider,
                            instance_id,
                            tuple("GPU-" + str(i) for i in range(binding.gpu_count))
                            if binding.execution_slots == 1
                            else ("GPU-" + str(ordinal),),
                            binding.recipe_ids,
                            binding.model_id,
                            binding.configuration_id,
                            backend="wangp-worker",
                            engine_manifest_digest=binding.engine_manifest_digest,
                        )
                        control.register(spec)
                        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
                    self.repo.update_instance(intent["id"], "ready")
                with self.repo.transaction() as conn:
                    conn.execute(
                        update(operator_commands)
                        .where(operator_commands.c.id == command["id"])
                        .values(state="completed", updated_at=self.now)
                    )
        with self.repo.transaction() as conn:
            existing = conn.execute(select(operator_heartbeats)).first()
            if existing is None:
                conn.execute(
                    insert(operator_heartbeats).values(
                        id="global",
                        controller_id="test-history",
                        observed_at=self.now,
                        state="running",
                    )
                )


class OperatorTests(OperatorCase):
    def test_cookie_operator_required_and_machine_key_never_grants_rental(self):
        for actor, code in (
            (None, "operator_login_required"),
            (Principal("supervan", "browser"), "operator_forbidden"),
            (
                Principal("superdan", "pat", machine=True, all_projects=True),
                "operator_forbidden",
            ),
        ):
            for function in (
                lambda: self.service.state(actor),
                lambda: self.service.preview(actor, self.chosen),
                lambda: self.service.update_policy(actor, self.policy),
            ):
                with self.assertRaisesRegex(OperatorError, code):
                    function()
        self.assertTrue(
            self.service.state(self.actor)["operator"]["permissions"]["start"]
        )

    def test_explicit_second_operator_can_manage_capacity_but_keys_cannot(self):
        self.settings.operator_capacity_owners = ("superdan", "supervan")
        other = Principal("supervan", "browser", auth_mode="password")
        preview = self.service.preview(other, self.chosen)
        self.assertTrue(preview["can_start"])
        operation = self.service.start(
            other, {"preview_id": preview["preview_id"]}, "supervan-start"
        )["operation"]
        self.assertEqual(operation["kind"], "start")
        self.assertTrue(self.service.state(other)["operator"]["permissions"]["stop"])
        for actor in (
            Principal("outsider", "browser"),
            Principal("supervan", "pat", machine=True, all_projects=True),
            Principal("superdan", "pat", machine=True, all_projects=True),
        ):
            with self.assertRaisesRegex(OperatorError, "operator_forbidden"):
                self.service.preview(actor, self.chosen)

    def test_provider_minimum_ttl_is_visible_and_blocks_start(self):
        self.registry.bindings[self.binding.binding_id] = replace(
            self.binding, max_ttl_seconds=7200, min_ttl_seconds=3780
        )
        preview = self.service.preview(self.actor, self.chosen)
        self.assertEqual(preview["minimum_ttl_seconds"], 3780)
        self.assertIn(
            {"code": "operator_ttl_below_provider_minimum"}, preview["blockers"]
        )
        self.assertFalse(preview["can_start"])

    def test_pool_pause_blocks_legacy_preview_and_stale_preview_without_reservation(
        self,
    ):
        budget = self.repo.get_budget("owner-budget")
        for instances, gpus in ((0, 1), (1, 0), (0, 0)):
            with self.subTest(instances=instances, gpus=gpus):
                self.repo.configure_pool(
                    self.binding.pool, max_instances=1, max_physical_gpus=1
                )
                before = self.service.preview(self.actor, self.chosen)
                self.assertTrue(before["can_start"])
                self.repo.configure_pool(
                    self.binding.pool, max_instances=instances, max_physical_gpus=gpus
                )
                paused = self.service.preview(self.actor, self.chosen)
                self.assertFalse(paused["can_start"])
                self.assertEqual(paused["blockers"], [{"code": "operator_pool_paused"}])
                with self.assertRaisesRegex(OperatorError, "operator_pool_paused"):
                    self.service.start(
                        self.actor, {"preview_id": before["preview_id"]}, "paused-start"
                    )
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.assertEqual(self.seeded_instances, [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(operator_commands)),
                0,
            )

    def test_pool_pause_preserves_existing_start_idempotency(self):
        before = self.service.preview(self.actor, self.chosen)
        body = {"preview_id": before["preview_id"]}
        original = self.service.start(self.actor, body, "before-pause")
        self.repo.configure_pool(
            self.binding.pool, max_instances=0, max_physical_gpus=0
        )
        self.assertEqual(self.service.start(self.actor, body, "before-pause"), original)
        self.assertFalse(self.service.preview(self.actor, self.chosen)["can_start"])
        self.assertEqual(self.repo.list_instance_intents(), [])

    def test_preview_only_and_no_fake_generation_jobs(self):
        preview = self.service.preview(self.actor, self.chosen)
        self.assertTrue(preview["can_start"])
        self.assertEqual(preview["estimated_hourly_cost_microusd"], 360_000)
        self.assertEqual(self.seeded_instances, [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(instance_intents)
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(select(func.count()).select_from(jobs)).scalar_one(),
                0,
            )
        value = json.dumps(preview) + json.dumps(self.service.state(self.actor))
        self.assertNotIn("/private", value)
        self.assertNotIn("launch", value)

    def test_start_idempotency_survives_expiry_policy_change_and_forbids_new_key(self):
        preview = self.service.preview(self.actor, self.chosen)
        body = {"preview_id": preview["preview_id"]}
        first = self.service.start(self.actor, body, "stable")
        self.now += 180
        self.service.update_policy(
            self.actor, {**self.policy, "expected_version": 1, "enabled": False}
        )
        self.assertEqual(first, self.service.start(self.actor, body, "stable"))
        with self.assertRaisesRegex(OperatorError, "operator_idempotency_conflict"):
            self.service.start(self.actor, {"preview_id": "different"}, "stable")
        with self.assertRaisesRegex(
            OperatorError, "operator_preview_already_confirmed"
        ):
            self.service.start(self.actor, body, "different-key")

    def test_policy_compare_and_set_does_not_modify_budget_or_existing_deadline(self):
        self.create()
        self.seed_running()
        original = self.repo.list_instance_intents()[0]
        budget = self.repo.get_budget("owner-budget")
        self.service.update_policy(
            self.actor,
            {
                **self.policy,
                "expected_version": 1,
                "max_instances": 1,
                "max_ttl_seconds": 120,
            },
        )
        with self.assertRaisesRegex(OperatorError, "operator_policy_version_conflict"):
            self.service.update_policy(
                self.actor, {**self.policy, "expected_version": 1}
            )
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(
            self.repo.list_instance_intents()[0]["hard_deadline"],
            original["hard_deadline"],
        )

    def test_pending_commands_count_towards_global_gpu_and_hourly_caps(self):
        self.service.update_policy(
            self.actor,
            {
                **self.policy,
                "expected_version": 1,
                "max_instances": 2,
                "max_physical_gpus": 2,
            },
        )
        self.create(chosen={**self.chosen, "node_count": 2})
        blocked = self.service.preview(self.actor, self.chosen)
        self.assertFalse(blocked["can_start"])
        self.assertIn({"code": "operator_gpu_limit"}, blocked["blockers"])
        self.seed_running()
        self.assertEqual(len(self.seeded_instances), 2)
        state = self.service.state(self.actor)
        self.assertEqual(state["summary"]["gpus_allocated"], 2)
        self.assertEqual(state["summary"]["hourly_cost_microusd"], 720_000)

    def test_public_bootstrap_masks_malformed_private_fields(self):
        value = public_bootstrap(
            {
                'state': {'secret': 'SECRET'},
                'reason_code': ['SECRET'],
                'observed_at': float('nan'),
                'log': 'SECRET',
                'slots': [
                    {
                        'state': {'secret': 'SECRET'},
                        'phase': ['SECRET'],
                        'failure_phase': 'SECRET',
                        'error_code': 'https://private.invalid/?token=SECRET',
                        'error_type': {'secret': 'SECRET'},
                        'message': 'SECRET',
                    }
                ],
            }
        )
        self.assertEqual(value['state'], 'blocked')
        self.assertEqual(
            value['slots'][0]['error_code'], 'UnclassifiedBootstrapFailure'
        )
        self.assertNotIn('observed_at', value)
        self.assertNotIn('SECRET', json.dumps(value))

    def test_removal_feedback_times_out_without_mutating_ledger_or_calling_provider(
        self,
    ):
        self.create()
        self.seed_running()
        node = self.service.state(self.actor)["nodes"][0]
        self.assertIsNone(node["removal_confirmation"])
        self.service.node_command(
            self.actor,
            node["id"],
            {"expected_version": node["version"]},
            "stop-feedback",
            "stop",
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node["id"])
                .values(state="destroying")
            )
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == node["id"])
                .values(
                    destroy_started_at=self.now,
                    last_observed_at=self.now,
                    last_observation={"state": "unknown", "private": "do-not-expose"},
                )
            )
            connection.execute(
                insert(scaler_receipts).values(
                    id="feedback-claim",
                    intent_id=node["id"],
                    operation="removal_check_started",
                    observed_at=self.now + 60,
                    facts={"state": "unknown"},
                )
            )
        budget = self.repo.get_budget("owner-budget")
        calls = len(self.seeded_instances)
        self.now += 299
        feedback = self.service.state(self.actor)["nodes"][0]["removal_confirmation"]
        self.assertEqual(feedback["state"], "pending")
        self.assertEqual(feedback["next_check_at"], feedback["requested_at"] + 120)
        self.assertEqual(feedback["last_checked_at"], feedback["requested_at"])
        self.now += 1
        for _ in range(3):
            state = self.service.state(self.actor)
            self.assertEqual(
                state["nodes"][0]["removal_confirmation"]["state"], "overdue"
            )
            self.assertNotIn("do-not-expose", json.dumps(state))
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(calls, len(self.seeded_instances))
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "destroying")
        with self.repo.transaction() as connection:
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node["id"])
                .values(state="destroyed")
            )
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == node["id"])
                .values(
                    last_observed_at=self.now, last_observation={"state": "destroyed"}
                )
            )
        feedback = self.service.state(self.actor)["nodes"][0]["removal_confirmation"]
        self.assertEqual(feedback["state"], "confirmed")
        self.assertIsNone(feedback["next_check_at"])
        self.assertIsNone(feedback["reason_code"])

    def test_multiple_cards_can_be_one_node_with_two_execution_slots(self):
        binding = replace(
            self.binding, gpu_count=2, execution_slots=2, hourly_cost_microusd=720_000
        )
        self.registry.bindings[binding.binding_id] = binding
        self.create(chosen={**self.chosen, "gpu_count": 2})
        self.seed_running()
        state = self.service.state(self.actor)
        self.assertEqual(state["summary"]["nodes_active"], 1)
        self.assertEqual(state["summary"]["gpus_allocated"], 2)
        self.assertEqual(state["summary"]["slots_ready"], 2)
        self.assertEqual(state["operations"][0]["state"], "completed")

    def test_unmanaged_existing_capacity_is_not_silently_assigned_zero_cost(self):
        self.repo.configure_pool("external", max_instances=1, max_physical_gpus=1)
        self.repo.reserve_instance_intent(
            self.scope,
            "external",
            "legacy",
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=100_000,
            hard_deadline=5000,
            budget_account_ids=("owner-budget",),
            dry_run=False,
            provider="lium",
        )
        preview = self.service.preview(self.actor, self.chosen)
        self.assertIn(
            {"code": "operator_unpriced_existing_capacity"}, preview["blockers"]
        )
        self.assertIsNone(
            self.service.state(self.actor)["summary"]["hourly_cost_microusd"]
        )

    def test_registry_json_file_retains_exact_binding_and_is_not_public(self):
        path = Path(self.temp.name) / "registry.json"
        path.write_text(
            json.dumps({"schema_version": 1, "bindings": [asdict(self.binding)]}),
            encoding="utf-8",
        )
        loaded = OperatorRegistry.from_file(path)
        self.assertEqual(
            loaded.get(self.binding.binding_id).fingerprint, self.binding.fingerprint
        )
        self.assertNotIn("/private", json.dumps(loaded.catalog()))
        path.write_text(
            json.dumps({"schema_version": 1, "bindings": [{"surprise": "value"}]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(OperatorError, "operator_registry_file_invalid"):
            OperatorRegistry.from_file(path)

    def test_invalid_country_types_raise_safe_validation_not_typeerror(self):
        for country in ([{}], [[]], ["us"], ["US", "US"]):
            with self.assertRaisesRegex(OperatorError, "operator_countries_invalid"):
                selection({**self.chosen, "filters": {"allowed_countries": country}})

    def test_retired_routes_authenticate_then_410_without_ledger_writes(self):
        self.create()
        self.seed_running()
        before = self.service.state(self.actor)
        budget = self.repo.get_budget("owner-budget")
        app = FastAPI()
        actor = [self.actor]

        @app.middleware("http")
        async def principal(request: Request, call_next):
            request.state.principal = actor[0]
            return await call_next(request)

        register_routes(app, service=self.service)
        requests = (
            ("GET", "/offers?runtime_profile_id=test&gpu_type=test&mode=fl", None),
            ("GET", "/candidates?model_id=test&mode=fl", None),
            ("POST", "/market-refreshes", {}),
            ("POST", "/previews", self.chosen),
            ("POST", "/starts", {"preview_id": "unused"}),
        )
        with TestClient(app) as client:
            for identity, status in (
                (self.actor, 410),
                (None, 401),
                (Principal("foreign", "browser", auth_mode="password"), 403),
                (Principal("superdan", "pat", machine=True), 403),
            ):
                actor[0] = identity
                for method, path, body in requests:
                    with self.subTest(identity=identity, path=path):
                        response = client.request(
                            method,
                            "/v1/operator/capacity" + path,
                            json=body,
                            headers={"Idempotency-Key": "retired-unused"},
                        )
                        self.assertEqual(response.status_code, status, response.text)
                        self.assertEqual(response.headers["cache-control"], "no-store")
                        if status == 410:
                            self.assertEqual(
                                response.json()["code"],
                                "legacy_capacity_runtime_retired",
                            )
            actor[0] = self.actor
            response = client.get("/v1/operator/capacity/state")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), before)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(len(self.repo.list_instance_intents()), 1)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(operator_commands)), 1
            )

    def test_opposite_generation_modes_resolve_different_deployments(self):
        ref = replace(
            self.binding,
            binding_id="test-ref",
            recipe_ids=("h3-base-ref2va-v1",),
            configuration_id="test-ref-config",
            launch=replace(self.binding.launch, configuration_id="test-ref-config"),
        )
        self.registry.bindings[ref.binding_id] = ref
        self.assertEqual(
            self.registry.resolve(selection(self.chosen)).binding_id,
            self.binding.binding_id,
        )
        self.assertEqual(
            self.registry.resolve(selection({**self.chosen, "mode": "ref"})).binding_id,
            ref.binding_id,
        )
        with self.assertRaisesRegex(OperatorError, "operator_selection_invalid"):
            selection(
                {key: value for key, value in self.chosen.items() if key != "mode"}
            )

    def test_stale_snapshot_is_explicit_and_does_not_invent_telemetry(self):
        self.create()
        self.seed_running()
        self.now += 121
        state = self.service.state(self.actor)
        self.assertTrue(state["controller"]["stale"])
        self.assertTrue(state["nodes"][0]["stale"])
        self.assertTrue(state["nodes"][0]["slots"][0]["stale"])
        self.assertEqual(state["summary"]["slots_ready"], 0)
