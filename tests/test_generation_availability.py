"""Current public mode observations: isolated SQL/fake identities, no cloud calls."""

import copy
from dataclasses import replace
import json
import secrets
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event, insert, update, select

from studio_platform.api import create_app
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.execution_profiles import tested_envelope
from studio_platform.generation_availability import generation_availability
from studio_platform.operator_capacity import (
    DeploymentBinding,
    OperatorRegistry,
    operator_nodes,
    operator_commands,
    operator_heartbeats,
)
from studio_platform.repository import (
    Conflict,
    instance_intents,
    registered_workers,
    request_hash,
)
from studio_platform.runtime_catalog import (
    engine_manifest,
    profile_output_delivery,
    public_catalog,
)
from studio_platform.capacity_types import LaunchSpec
import test_platform_execution_profiles as profile_tests
from test_platform_execution_profiles import PRUNED, INT8
from test_platform_execution_policy import policy as legacy_policy


class AvailabilityTests(unittest.TestCase):
    write = profile_tests.ExecutionProfileTests.write
    compiled = profile_tests.ExecutionProfileTests.compiled

    def setUp(self):
        profile_tests.ExecutionProfileTests.setUp(self)
        self.now = self.repo.clock()
        self.repo.clock = lambda: self.now
        ref = copy.deepcopy(self.values[0])
        ref.update(
            pool="ref-pool",
            configuration_id="ref-config",
            recipe_ids=["h3-base-ref2va-v1"],
            engine_manifest_digest=engine_manifest(PRUNED, "ref").digest,
            envelope=tested_envelope(PRUNED, "ref"),
        )
        self.values.append(ref)
        self.write()
        self.registry = OperatorRegistry()

    def result(self):
        return generation_availability(self.settings, self.repo, registry=self.registry)

    def modes(self, profile=PRUNED):
        return next(
            p["modes"]
            for p in self.result()["profiles"]
            if p["deployment_profile_id"] == profile
        )

    def worker(self, index, *, ready=True, provider="test-only", instance=None):
        p = self.values[index]
        spec = WorkerSpec(
            f"private-worker-{index}",
            p["pool"],
            provider,
            instance or f"private-instance-{index}",
            (f"private-gpu-{index}",),
            tuple(p["recipe_ids"]),
            p["model_id"],
            p["configuration_id"],
            "wangp-worker",
            p["engine_manifest_digest"],
            output_delivery="native-frames-v1",
        )
        control = WorkerControl(self.repo)
        control.register(spec)
        if ready:
            control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        return spec

    def mutate_worker(self, spec, **values):
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == spec.worker_id)
                .values(**values)
            )

    def node(self):
        p = self.values[0]
        self.repo.configure_pool(p["pool"], max_instances=2, max_physical_gpus=2)
        intent = self.repo.reserve_instance_intent(
            self.scope,
            p["pool"],
            "private-intent",
            provider="lium",
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=1,
            hard_deadline=self.now + 1800,
            budget_account_ids=["test-tenant:sixnine"],
            dry_run=False,
        )
        self.repo.update_instance(intent["id"], "creating")
        self.repo.update_instance(
            intent["id"], "starting", provider_instance_id="private-provider-instance"
        )
        binding = DeploymentBinding(
            "private-binding",
            PRUNED,
            "RTX 5090",
            1,
            p["pool"],
            p["configuration_id"],
            p["model_id"],
            tuple(p["recipe_ids"]),
            p["engine_manifest_digest"],
            LaunchSpec("lium", p["configuration_id"], p["model_id"]),
            self.scope,
            ("test-tenant:sixnine",),
            1,
            1,
            self.now + 3600,
            enabled=True,
        )
        self.registry = OperatorRegistry([binding])
        with self.repo.transaction() as connection:
            connection.execute(
                insert(operator_commands).values(
                    id="private-command",
                    actor="private-actor",
                    idempotency_key="private-key",
                    request_hash="a" * 64,
                    kind="start",
                    state="waiting",
                    payload={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            connection.execute(
                insert(operator_nodes).values(
                    intent_id=intent["id"],
                    command_id="private-command",
                    ordinal=0,
                    binding_id=binding.binding_id,
                    binding_hash=binding.fingerprint,
                    payload={"selection": {"runtime_profile_id": PRUNED, "mode": "fl"}},
                    desired_state="running",
                    runtime_state="preparing",
                    updated_at=self.now,
                )
            )
            connection.execute(
                insert(operator_heartbeats).values(
                    id="global",
                    controller_id="private-controller",
                    observed_at=self.now,
                    state="running",
                )
            )
        return intent

    def test_none_fl_ref_both_and_exact_profile_are_not_substituted(self):
        self.assertEqual({m["state"] for m in self.modes().values()}, {"unavailable"})
        other = self.worker(1)
        self.assertTrue(self.modes(INT8)["fl"]["available"])
        self.assertFalse(self.modes()["fl"]["available"])
        ref = self.worker(2)
        self.assertEqual(
            [self.modes()[m]["state"] for m in ("fl", "ref")], ["unavailable", "ready"]
        )
        fl = self.worker(0)
        self.assertTrue(all(m["available"] for m in self.modes().values()))
        WorkerControl(self.repo).retire(ref.worker_id, upstream_idle_confirmed=True)
        self.assertEqual(
            [self.modes()[m]["state"] for m in ("fl", "ref")], ["ready", "unavailable"]
        )

    def test_busy_registered_stale_future_hash_and_drain_are_distinct(self):
        spec = self.worker(0, ready=False)
        self.assertEqual(self.modes()["fl"]["state"], "starting")
        self.mutate_worker(spec, state="busy", current_job_id="private-job")
        self.assertEqual(self.modes()["fl"]["state"], "busy")
        self.assertTrue(self.modes()["fl"]["available"])
        self.mutate_worker(spec, expires_at=self.now - 1)
        self.assertEqual(self.modes()["fl"]["state"], "unknown")
        self.mutate_worker(spec, expires_at=self.now + 90, updated_at=self.now + 1)
        self.assertEqual(self.modes()["fl"]["state"], "unknown")
        self.mutate_worker(spec, updated_at=self.now, spec_hash="f" * 64)
        self.assertEqual(self.modes()["fl"]["state"], "unknown")
        self.mutate_worker(spec, drain_requested=1)
        self.assertEqual(self.modes()["fl"]["state"], "unavailable")

    def test_starting_requires_fresh_exact_binding_and_excludes_stopping(self):
        intent = self.node()
        self.assertEqual(self.modes()["fl"]["state"], "starting")
        self.assertFalse(self.modes()["fl"]["available"])
        self.now += 31
        self.assertEqual(self.modes()["fl"]["state"], "unknown")
        self.now -= 31
        with self.repo.transaction() as connection:
            connection.execute(update(operator_nodes).values(binding_hash="f" * 64))
        self.assertEqual(self.modes()["fl"]["state"], "unknown")
        with self.repo.transaction() as connection:
            connection.execute(update(operator_nodes).values(desired_state="stopped"))
        self.assertEqual(self.modes()["fl"]["state"], "unavailable")

    def confirmed_node(self):
        intent = self.node()
        with self.repo.transaction() as connection:
            node = connection.execute(select(operator_nodes)).mappings().one()
            connection.execute(
                update(operator_nodes).values(
                    runtime_state="ready",
                    payload={
                        **node["payload"],
                        "lifetime": {
                            "state": "verified",
                            "instance_id": "private-provider-instance",
                            "observed_at": self.now,
                            "safe_deadline": intent["hard_deadline"],
                        },
                    },
                )
            )
        return intent

    def test_stop_request_excludes_fresh_worker_and_invalidates_prior_preflight(self):
        self.confirmed_node()
        self.worker(0, provider="lium", instance="private-provider-instance")
        compiled, fingerprint = self.compiled()
        policies = ExecutionPolicies(self.settings, self.repo)
        admitted = policies.evaluate(compiled, self.scope, fingerprint)
        self.assertTrue(admitted.execution["enabled"], admitted.execution)
        plan = {"request": compiled, "execution_plan": admitted.execution}
        with self.repo.transaction() as connection:
            connection.execute(update(operator_nodes).values(desired_state="stopped"))
        self.assertEqual(self.modes()["fl"]["state"], "unavailable")
        with self.assertRaisesRegex(
            Conflict, "execution_policy_changed_or_unavailable"
        ):
            policies.ensure_current(plan, self.scope)

    def test_disabled_expired_and_invalid_config_do_not_claim_readiness(self):
        self.worker(0)
        self.settings = replace(self.settings, generation_enabled=False)
        self.assertEqual(self.modes()["fl"]["state"], "disabled")
        self.settings = replace(self.settings, generation_enabled=True)
        self.values[0]["enabled"] = False
        self.write()
        self.assertEqual(self.modes()["fl"]["state"], "disabled")
        self.values[0]["enabled"] = True
        self.values[0]["qualification"]["expires_at"] = self.now + 10
        self.write()
        self.assertEqual(self.modes()["fl"]["state"], "disabled")
        self.path.write_text("invalid")
        self.assertEqual(self.modes()["fl"]["state"], "unknown")

    def test_expired_known_instance_excludes_fresh_worker_and_invalidates_prior_preflight(
        self,
    ):
        intent = self.confirmed_node()
        self.worker(0, provider="lium", instance="private-provider-instance")
        compiled, fingerprint = self.compiled()
        policies = ExecutionPolicies(self.settings, self.repo)
        admitted = policies.evaluate(compiled, self.scope, fingerprint)
        self.assertTrue(admitted.execution["enabled"], admitted.execution)
        plan = {"request": compiled, "execution_plan": admitted.execution}
        with self.repo.transaction() as connection:
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent["id"])
                .values(hard_deadline=self.now)
            )
        self.assertEqual(self.modes()["fl"]["state"], "unavailable")
        self.assertFalse(self.modes()["fl"]["available"])
        with self.assertRaisesRegex(
            Conflict, "execution_policy_changed_or_unavailable"
        ):
            policies.ensure_current(plan, self.scope)

    def test_legacy_null_route_uses_only_its_actual_policy_not_named_default(self):
        self.worker(0)
        self.settings = replace(self.settings, default_deployment_profile_id=PRUNED)
        self.assertEqual(self.modes(None)["fl"]["state"], "disabled")
        legacy = legacy_policy(self.now)
        legacy["recipe_ids"] = ["h3-base-fl2va-v1"]
        path = self.root / "legacy.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.settings = replace(
            self.settings, execution_policy_file=path, execution_backend="comfy-worker"
        )
        spec = WorkerSpec(
            "private-legacy",
            legacy["pool"],
            "private-provider",
            "private-legacy-instance",
            ("private-legacy-gpu",),
            tuple(legacy["recipe_ids"]),
            legacy["model_id"],
            legacy["configuration_id"],
        )
        control = WorkerControl(self.repo)
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.assertEqual(self.modes(None)["fl"]["state"], "ready")
        self.assertEqual(self.modes(None)["ref"]["state"], "disabled")
        self.assertFalse(any(m["available"] for m in self.modes().values()))
        self.path.write_text("invalid")
        self.assertEqual(self.modes(None)["fl"]["state"], "ready")
        path.write_text("invalid")
        self.assertEqual(self.modes(None)["fl"]["state"], "unknown")

    def test_authenticated_scopes_safe_projection_and_no_side_effect(self):
        self.worker(0)
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/generation-availability").status_code, 401)
            for i, scopes in enumerate(
                (("jobs:read",), ("jobs:write",), ("assets:read",))
            ):
                token = secrets.token_urlsafe(40)
                app.state.auth.register_client(
                    f"availability-{i}", token, "superdan", ["story-one"], scopes
                )
                response = client.get(
                    "/v1/generation-availability",
                    headers={"Authorization": "Bearer " + token},
                )
                self.assertEqual(response.status_code, 200 if i < 2 else 403)
                if i < 2:
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertNotIn("private-", response.text)
                    self.assertEqual(response.json()["expires_at"], self.now + 10)
            client.post(
                "/api/auth/login", json={"username": "supervan"}
            ).raise_for_status()
            self.assertEqual(client.get("/v1/generation-availability").status_code, 200)
            self.assertEqual(client.get("/v1/operator/capacity/state").status_code, 403)

    def test_one_request_batches_reads_across_all_actual_profile_modes(self):
        def observed():
            statements = []

            def read(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            event.listen(self.repo.engine, "before_cursor_execute", read)
            try:
                value = self.result()
            finally:
                event.remove(self.repo.engine, "before_cursor_execute", read)
            self.assertTrue(
                all(
                    statement.lstrip().upper().startswith("SELECT")
                    for statement in statements
                )
            )
            return value, len(statements)

        small, small_queries = observed()
        template = copy.deepcopy(self.values[0])
        self.values = []
        for index, profile in enumerate(public_catalog()["profiles"]):
            for model in profile["models"]:
                identity, mode = profile["id"], model["mode"]
                policy = copy.deepcopy(template)
                policy.update(
                    deployment_profile_id=identity,
                    model_id=profile["model_id"],
                    pool=f"bulk-{index}-{mode}",
                    configuration_id=f"bulk-config-{index}-{mode}",
                    recipe_ids=[model["generation_recipe_id"]],
                    engine_manifest_digest=engine_manifest(identity, mode).digest,
                    output_delivery=profile_output_delivery(identity),
                    envelope=tested_envelope(identity, mode),
                )
                self.values.append(policy)
        self.write()
        large, large_queries = observed()
        self.assertGreater(len(self.values), 20)
        self.assertEqual(large_queries, small_queries)
        self.assertLessEqual(large_queries, 6)
        self.assertEqual(
            next(p for p in large["profiles"] if p["deployment_profile_id"] == PRUNED),
            next(p for p in small["profiles"] if p["deployment_profile_id"] == PRUNED),
        )
        self.assertTrue(
            all(
                not mode["available"]
                for profile in large["profiles"]
                for mode in profile["modes"].values()
            )
        )

    def test_batch_matches_individual_worker_status_and_next_request_is_fresh(self):
        workers = [self.worker(0), self.worker(1, ready=False), self.worker(2)]
        self.mutate_worker(
            workers[2], state="busy", current_job_id="private-running-job"
        )
        control = WorkerControl(self.repo)

        def status(index, snapshot=None):
            policy = self.values[index]
            return control.pool_status(
                policy["pool"],
                model_id=policy["model_id"],
                configuration_id=policy["configuration_id"],
                recipe_id=policy["recipe_ids"][0],
                backend=policy["backend"],
                engine_manifest_digest=policy["engine_manifest_digest"],
                output_delivery=policy["output_delivery"],
                expected_runtime_s=policy["reservation"]["expected_runtime_s"],
                deployment_profile_id=policy["deployment_profile_id"],
                _snapshot=snapshot,
            )

        individual = [status(index) for index in range(3)]
        with control.pool_status_snapshot() as snapshot:
            self.assertEqual(
                [status(index, snapshot) for index in range(3)], individual
            )
        self.assertEqual(
            (
                individual[0]["ready"],
                individual[1]["registered"],
                individual[2]["busy"],
            ),
            (1, 1, 1),
        )
        with self.assertRaisesRegex(ValueError, "pool_status_snapshot_unavailable"):
            status(0, snapshot)
        self.mutate_worker(workers[0], expires_at=self.now - 1)
        self.assertEqual(self.modes()["fl"]["state"], "unknown")
        self.mutate_worker(workers[0], expires_at=self.now + 90, drain_requested=1)
        self.assertEqual(self.modes()["fl"]["state"], "unavailable")

    def test_disabled_or_invalid_configuration_does_not_open_a_read_snapshot(self):
        self.settings = replace(self.settings, generation_enabled=False)
        with patch.object(
            self.repo.engine,
            "connect",
            side_effect=AssertionError("unexpected_database_read"),
        ):
            self.assertEqual(self.modes()["fl"]["state"], "disabled")
        self.settings = replace(self.settings, generation_enabled=True)
        self.path.write_text("invalid")
        with patch.object(
            self.repo.engine,
            "connect",
            side_effect=AssertionError("unexpected_database_read"),
        ):
            self.assertEqual(self.modes()["fl"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
