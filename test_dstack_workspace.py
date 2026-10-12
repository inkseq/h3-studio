"""P4 ownership, durable group commands and exact queue-impact tests; no providers."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from studio_platform.auth import Principal
from studio_platform.dstack_operator import DstackOperator
from studio_platform.dstack_workspace import CapacityLoss, DstackWorkspace, operator_groups, stop_impact
from studio_platform.operator_capacity import OperatorError, operator_commands, operator_nodes
from studio_platform.repository import attempts, budget_reservations, instance_intents, jobs, registered_workers
from test_platform_repository import LedgerCase
import test_dstack_operator as fixtures


class WorkspaceTests(LedgerCase):
    def setUp(self):
        super().setUp()
        fixtures.DstackOperatorTests._setup_dstack(self)
        self.settings.operator_admin_owners = ("superdan",)
        self.workspace = DstackWorkspace(self.service)

    def start_node(self, key=None):
        preview = self.service.preview(self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900})
        return self.service.start(self.owner, {"preview_id": preview["preview_id"]}, key or uuid.uuid4().hex)["node_id"]

    def waiting_job(self, key=None, *, owner=None, profile_id="5090-fl"):
        profile = self.service.profiles[profile_id]
        request = {"recipe_id": "h3-base-fl2va-v1", "deployment_profile_id": profile["runtime_profile_id"],
            "request": {"model": profile["model_id"], "mode": "fl", "prompt": "PRIVATE-CREATIVE-TEXT"}}
        execution = {"pool": profile["pool"], "enabled": True, "backend": "wangp-worker",
            "dispatch_backend": "hatchet-v1", "output_delivery": "native-frames-v1",
            "deployment_profile_id": profile["runtime_profile_id"], "configuration_id": profile["configuration_id"],
            "engine_manifest_digest": profile["manifest_digest"], "expected_runtime_s": 60}
        scope = self.scope if owner is None else self.other
        plan = self.repo.create_plan(scope, request, execution, expires_at=self.now+1000, estimated_cost_microusd=0)
        return self.repo.create_job(scope, plan["id"], key or uuid.uuid4().hex)

    def ready_worker(self, node_id, *, state="ready"):
        binding = self.store.load(node_id)
        run = self.client.runs[binding["run_name"]]
        instance = "gpu-"+node_id
        run.update(status="running", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": instance,
            "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}}}})
        self.capacity.observe(node_id)
        worker_id = "dstack-"+node_id.replace("-", "")
        with self.repo.engine.connect() as connection:
            pool = connection.execute(select(instance_intents.c.pool).where(instance_intents.c.id == node_id)).scalar_one()
        spec = {"pool": pool, "backend": "wangp-worker", "provider": "vast",
            "dispatch_backend": "hatchet-v1", "output_delivery": "native-frames-v1",
            "recipe_ids": ["h3-base-fl2va-v1"], "model_id": binding["model_id"],
            "configuration_id": binding["configuration_id"], "engine_manifest_digest": binding["manifest_digest"]}
        with self.repo.transaction() as connection:
            connection.execute(insert(registered_workers).values(id=worker_id, pool=pool, provider="vast",
                instance_id=instance, spec=spec, spec_hash="a"*64, state=state, current_job_id=None,
                drain_requested=0, fence=1, expires_at=self.now+120, updated_at=self.now))
            original = connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id == node_id)).mappings().one()
            payload = copy.deepcopy(original["payload"])
            payload["dstack"]["broker_observation"] = {"ready": True, "worker_id": worker_id,
                "observed_at": self.now, "heartbeat_at": self.now}
            connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                .values(payload=payload, runtime_state="busy" if state == "busy" else "ready"))
        return worker_id

    def test_groups_replay_owner_isolation_and_ungrouped_move_preserve_runtime(self):
        group = self.workspace.create_group(self.owner, {"name": "My group"}, "create-one")
        self.assertEqual(self.workspace.create_group(self.owner, {"name": "My group"}, "create-one"), group)
        node = self.start_node()
        with self.repo.engine.connect() as connection:
            before = connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id == node)).mappings().one()
        self.workspace.move(self.owner, node, {"group_id": group["id"]}, "move-in")
        with self.assertRaisesRegex(OperatorError, "dstack_node_not_found"):
            self.workspace.move(self.other_owner, node, {"group_id": None}, "cross-owner")
        other_group = self.workspace.create_group(self.other_owner, {"name": "Other group"}, "other-group")
        with self.assertRaisesRegex(OperatorError, "dstack_group_not_found"):
            self.workspace.move(self.owner, node, {"group_id": other_group["id"]}, "foreign-group")
        self.workspace.move(self.owner, node, {"group_id": None}, "move-out")
        with self.repo.engine.connect() as connection:
            after = connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id == node)).mappings().one()
            self.assertEqual(len(connection.execute(select(operator_groups.c.id)).all()), 2)
        self.assertEqual(before["payload"]["dstack"], after["payload"]["dstack"])
        self.assertEqual(before["desired_state"], after["desired_state"])
        self.assertIsNone(after["payload"]["group_id"])
        self.assertEqual(self.client.stopped, [])

    def test_last_compatible_queue_warning_busy_backup_and_stale_backup(self):
        first, second = self.start_node(), self.start_node()
        self.ready_worker(first)
        self.ready_worker(second, state="busy")
        job = self.waiting_job()
        with self.repo.engine.connect() as connection:
            self.assertEqual(stop_impact(connection, self.settings, [first], self.now)["warnings"], [])
            both = stop_impact(connection, self.settings, [first, second], self.now)
        self.assertEqual(both["warnings"][0]["waiting_jobs"], 1)
        self.assertEqual(both["warnings"][0]["waiting_job_ids"], [job["id"]])
        self.now += 31
        with self.repo.engine.connect() as connection:
            impact = stop_impact(connection, self.settings, [first], self.now)
        self.assertEqual(impact["warnings"][0]["waiting_jobs"], 1)
        self.assertNotIn("PRIVATE-CREATIVE-TEXT", json.dumps(impact))

    def test_same_clock_group_membership_changes_invalidate_reviewed_version(self):
        group = self.workspace.create_group(self.owner, {"name": "Same tick"}, "same-tick-group")
        preview = self.service.preview(self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900,
            "group_id": group["id"]})
        node = self.service.start(self.owner, {"preview_id": preview["preview_id"]}, "same-tick-start")["node_id"]
        after_start = self.workspace.state(self.owner)["groups"][0]["version"]
        self.assertGreater(after_start, group["version"])
        with self.assertRaisesRegex(OperatorError, "operator_group_version_conflict"):
            self.workspace.group_command(self.owner, group["id"], {"expected_version": group["version"]},
                "stale-membership-stop", "stop")
        self.workspace.move(self.owner, node, {"group_id": None}, "same-tick-leave")
        self.assertGreater(self.workspace.state(self.owner)["groups"][0]["version"], after_start)
        self.assertEqual(self.client.stopped, [])

    def test_queue_projection_freezes_eligibility_during_heartbeat_change(self):
        node = self.start_node()
        self.ready_worker(node)
        self.waiting_job()
        with patch("studio_platform.dstack_workspace._compatible", side_effect=[True, False, False]) as eligible:
            state = self.workspace.state(self.owner)
        queue = next(item for item in state["queue"] if item["mode"] == "fl")
        self.assertEqual(queue["ready"], 1)
        self.assertEqual(queue["rough_wait_seconds"], 0)
        self.assertEqual(queue["unserved_waiting"], 0)
        self.assertEqual(eligible.call_count, 1)

    def test_queue_read_time_follows_newer_observation_during_owner_projection(self):
        node = self.start_node()
        self.ready_worker(node)
        self.waiting_job()
        original_state = self.service.state
        def newer_observation(actor):
            result = original_state(actor)
            self.now += 1
            self.capacity.observe(node)
            return result
        with patch.object(self.service, "state", side_effect=newer_observation):
            state = self.workspace.state(self.owner)
        queue = next(item for item in state["queue"] if item["mode"] == "fl")
        self.assertEqual(queue["ready"], 1)
        self.assertEqual(queue["unserved_waiting"], 0)
        self.assertEqual(state["observed_at"], self.now)

    def test_wrong_profile_other_owner_and_uncertain_attempt_are_not_new_queue(self):
        node = self.start_node()
        self.ready_worker(node)
        self.waiting_job(owner="supervan")
        wrong = self.waiting_job()
        uncertain = self.waiting_job()
        deferred = self.waiting_job()
        with self.repo.transaction() as connection:
            document = copy.deepcopy(wrong["request"])
            document["deployment_profile_id"] = "another-profile"
            connection.execute(update(jobs).where(jobs.c.id == wrong["id"]).values(request=document))
            for job, submitted in ((uncertain, True), (deferred, False)):
                identity = str(uuid.uuid4())
                connection.execute(insert(attempts).values(id=identity, job_id=job["id"], number=1,
                    status="unknown" if submitted else "failed", fence=1, worker_id="previous-worker",
                    created_at=self.now, updated_at=self.now,
                    submission_started_at=self.now if submitted else None, upstream_stopped=0))
                connection.execute(update(jobs).where(jobs.c.id == job["id"])
                    .values(attempt_no=1, current_attempt_id=identity))
        with self.repo.engine.connect() as connection:
            impact = stop_impact(connection, self.settings, [node], self.now)
        self.assertEqual(impact["warnings"][0]["waiting_job_ids"], [deferred["id"]])

    def test_group_stop_confirmation_replay_and_stopping_cost_rows_preserved(self):
        group = self.workspace.create_group(self.owner, {"name": "Both modes"}, "group-create")
        node = self.start_node()
        self.ready_worker(node)
        self.workspace.move(self.owner, node, {"group_id": group["id"]}, "join")
        self.waiting_job()
        with self.assertRaises(CapacityLoss):
            self.workspace.group_command(self.owner, group["id"], {}, "stop-group", "stop")
        impact = self.workspace.impact(self.owner, group_id=group["id"])
        result = self.workspace.group_command(self.owner, group["id"],
            {"acknowledge_capacity_loss": impact["fingerprint"]}, "confirmed-stop-group", "stop")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.workspace.group_command(self.owner, group["id"],
            {"acknowledge_capacity_loss": impact["fingerprint"]}, "confirmed-stop-group", "stop"), result)
        state = self.workspace.state(self.owner)
        self.assertEqual(state["nodes"][0]["lifecycle_phase"], "stopping")
        self.assertGreater(state["groups"][0]["hourly_cost_microusd"], 0)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(budget_reservations.c.state)).scalar_one(), "reserved")
        self.assertEqual(self.client.stopped, [])  # Controller owns the provider call.

    def test_admin_workspace_is_explicit_and_never_shares_private_prompt(self):
        node = self.start_node()
        self.ready_worker(node)
        self.waiting_job()
        self.assertEqual(self.workspace.state(self.other_owner)["nodes"], [])
        with self.assertRaisesRegex(OperatorError, "operator_admin_forbidden"):
            self.workspace.state(self.other_owner, admin=True)
        admin = self.workspace.state(self.owner, admin=True)
        self.assertEqual(admin["nodes"][0]["owner_id"], "superdan")
        self.assertEqual({item["account"] for item in admin["accounts"]}, {"superdan", "supervan"})
        self.assertNotIn("PRIVATE-CREATIVE-TEXT", json.dumps(admin))
        with self.assertRaisesRegex(OperatorError, "operator_forbidden"):
            self.workspace.state(Principal("superdan", "agent", machine=True), admin=True)

    def test_mode_queue_estimate_stays_unknown_when_one_profile_has_no_capacity(self):
        node = self.start_node()
        self.ready_worker(node)
        self.waiting_job()
        unavailable = self.waiting_job()
        with self.repo.transaction() as connection:
            request = copy.deepcopy(unavailable["request"])
            request["deployment_profile_id"] = "a-different-unavailable-profile"
            connection.execute(update(jobs).where(jobs.c.id == unavailable["id"]).values(request=request))
        mode = next(item for item in self.workspace.state(self.owner)["queue"] if item["mode"] == "fl")
        self.assertEqual(mode["waiting"], 2)
        self.assertEqual(mode["unserved_waiting"], 1)
        self.assertIsNone(mode["rough_wait_seconds"])

    def test_queue_wait_is_last_start_delay_separate_from_completion(self):
        node = self.start_node()
        self.ready_worker(node)
        self.waiting_job()
        self.waiting_job()
        mode = next(item for item in self.workspace.state(self.owner)["queue"] if item["mode"] == "fl")
        self.assertEqual(mode["rough_wait_seconds"], 60)
        self.assertEqual(mode["rough_complete_seconds"], 120)
        self.assertEqual(mode["estimate_basis"], "compatible_fifo_last_start_runtime_quotes")

    def test_workspace_poll_defers_new_node_that_races_with_read_projection(self):
        first = self.start_node()
        original = self.service.state
        added = []
        def racing(principal):
            projection = original(principal)
            added.append(self.start_node())
            return projection
        with patch.object(self.service, "state", side_effect=racing):
            state = self.workspace.state(self.owner)
        self.assertEqual([item["node_id"] for item in state["nodes"]], [first])
        self.assertEqual({item["node_id"] for item in self.workspace.state(self.owner)["nodes"]}, {first, added[0]})

    def test_queue_wait_never_shares_capacity_across_incompatible_pools_and_configurations(self):
        alternate = copy.deepcopy(self.config["profiles"][0])
        alternate.update(id="alternate-fl", pool="alternate-pool", configuration_id="alternate-native")
        self.config["profiles"].append(alternate)
        self.repo.configure_pool("alternate-pool", max_instances=2, max_physical_gpus=2)
        self.service = DstackOperator(self.repo, self.settings, self.capacity, self.config)
        self.workspace = DstackWorkspace(self.service)
        first = self.start_node()
        preview = self.service.preview(self.owner, {"profile_id": "alternate-fl", "ttl_seconds": 900})
        second = self.service.start(self.owner, {"preview_id": preview["preview_id"]}, "alternate-start")["node_id"]
        self.ready_worker(first)
        self.ready_worker(second)
        self.waiting_job()
        for _ in range(3):
            self.waiting_job(profile_id="alternate-fl")
        mode = next(item for item in self.workspace.state(self.owner)["queue"] if item["mode"] == "fl")
        self.assertEqual(mode["waiting"], 4)
        self.assertEqual(mode["ready"], 2)
        self.assertEqual(mode["unserved_waiting"], 0)
        self.assertEqual(mode["rough_wait_seconds"], 120)
        self.assertEqual(mode["rough_complete_seconds"], 180)

    def test_group_hold_partial_keeps_successful_original_command_and_budget(self):
        group = self.workspace.create_group(self.owner, {"name": "Pair"}, "create-pair")
        first, second = self.start_node(), self.start_node()
        for node in (first, second):
            self.workspace.move(self.owner, node, {"group_id": group["id"]}, "join-"+node)
        self.service.node_command(self.owner, second, {}, "stop-second")
        with self.repo.engine.connect() as connection:
            reserved_before = list(connection.execute(select(budget_reservations.c.amount_microusd)).scalars())
        # Snapshot both while running, then a stop races with the per-node hold.
        original = self.workspace._targets
        self.workspace._targets = lambda *args, **kwargs: [dict(row) for row in args[0].execute(
            select(operator_nodes).where(operator_nodes.c.intent_id.in_((first, second)))).mappings()]
        result = self.workspace.group_command(self.owner, group["id"], {"hold_seconds": 300}, "hold-pair", "hold")
        self.workspace._targets = original
        self.assertEqual(result["state"], "partial")
        self.assertEqual({item["node_id"]: item["state"] for item in result["results"]}[second], "rejected")
        self.assertEqual(self.workspace.group_command(self.owner, group["id"], {"hold_seconds": 300}, "hold-pair", "hold"), result)
        with self.repo.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(budget_reservations.c.amount_microusd)).scalars()), reserved_before)

    def test_group_child_acceptance_crash_replay_keeps_original_body_after_queue_change(self):
        group = self.workspace.create_group(self.owner, {"name": "Crash recovery"}, "recovery-group")
        node = self.start_node()
        self.ready_worker(node)
        self.workspace.move(self.owner, node, {"group_id": group["id"]}, "recovery-join")
        original_job = self.waiting_job()
        impact = self.workspace.impact(self.owner, group_id=group["id"])
        body = {"acknowledge_capacity_loss": impact["fingerprint"]}
        with patch.object(self.workspace, "_save_child_result", side_effect=RuntimeError("simulated_process_crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated_process_crash"):
                self.workspace.group_command(self.owner, group["id"], body, "recovery-stop", "stop")
        with self.repo.engine.connect() as connection:
            parent = connection.execute(select(operator_commands).where(operator_commands.c.kind == "dstack_group_stop")).mappings().one()
            child = connection.execute(select(operator_commands).where(operator_commands.c.kind == "dstack_stop")).mappings().one()
            frozen_request = parent["payload"]["child_requests"][node]
            self.assertEqual(parent["payload"]["results"], [])
        added_job = self.waiting_job()
        replay = self.workspace.group_command(self.owner, group["id"], body, "recovery-stop", "stop")
        self.assertEqual(replay["state"], "completed")
        self.assertEqual(replay["results"][0]["command_id"], child["id"])
        self.assertEqual(replay["results"][0]["state"], "accepted")
        self.assertEqual(replay["child_requests"][node], frozen_request)
        with self.repo.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(operator_commands).where(operator_commands.c.kind == "dstack_stop")).all()), 1)
            for job in (original_job, added_job):
                self.assertEqual(connection.execute(select(jobs.c.status).where(jobs.c.id == job["id"])).scalar_one(), "queued")
        self.assertEqual(self.client.stopped, [])

    def test_admin_can_read_stop_impact_and_stop_foreign_node_without_owner_impersonation(self):
        node = self.start_node()
        self.ready_worker(node)
        self.waiting_job()
        self.settings.operator_admin_owners = ("supervan",)
        impact = self.workspace.impact(self.other_owner, node_id=node)
        self.assertEqual(impact["warnings"][0]["waiting_jobs"], 1)
        result = self.service.node_command(self.other_owner, node,
            {"acknowledge_capacity_loss": impact["fingerprint"]}, "admin-stop-one")
        with self.repo.engine.connect() as connection:
            command = connection.execute(select(operator_commands).where(operator_commands.c.id == result["command_id"])).mappings().one()
            payload = connection.execute(select(operator_nodes.c.payload).where(operator_nodes.c.intent_id == node)).scalar_one()
        from studio_platform.dstack_operator import _actor
        self.assertEqual(command["actor"], _actor(self.settings.tenant_id, "supervan"))
        self.assertEqual(payload["owner_id"], "superdan")
        with self.assertRaisesRegex(OperatorError, "dstack_node_not_found"):
            self.workspace.move(self.other_owner, node, {"group_id": None}, "admin-cannot-move-owner")

    def test_http_auth_csrf_and_group_command_require_idempotency(self):
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        settings = Settings(Path(self.temp.name)/"workspace-api", tenant_id=self.scope.tenant_id,
            auth_mode="local-test", database_url=self.url, operator_capacity_owners=("superdan", "supervan"),
            operator_admin_owners=("superdan",))
        self.client.close = lambda: None
        app = create_app(settings, repository=self.repo, dstack_operator=self.service)
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/operator/dstack/workspace").status_code, 401)
            client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
            self.assertEqual(client.get("/v1/operator/dstack/admin/workspace").status_code, 403)
            client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
            self.assertEqual(client.post("/v1/operator/dstack/groups", json={"name": "Group"}).status_code, 422)
            self.assertEqual(client.post("/v1/operator/dstack/groups", json={"name": "Group"},
                headers={"Idempotency-Key": "csrf", "Origin": "https://untrusted.example"}).status_code, 403)
            created = client.post("/v1/operator/dstack/groups", json={"name": "Group"}, headers={"Idempotency-Key": "create-http"})
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(client.get("/v1/operator/dstack/workspace").json()["groups"][0]["name"], "Group")

    def test_detail_stage_query_authorizes_before_loading_protected_reader(self):
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        node = self.start_node()
        self.ready_worker(node)
        settings = Settings(Path(self.temp.name)/"metrics-api", tenant_id=self.scope.tenant_id,
            auth_mode="local-test", database_url=self.url, operator_capacity_owners=("superdan", "supervan"),
            operator_admin_owners=("superdan",))
        self.client.close = lambda: None
        app = create_app(settings, repository=self.repo, dstack_operator=self.service)
        route = "/v1/operator/dstack/nodes/"+node+"/metrics"
        with TestClient(app) as client, patch("studio_platform.stage_metrics.configured_stage_reader") as load:
            self.assertEqual(client.get(route).status_code, 401)
            client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
            self.assertEqual(client.get(route).status_code, 404)
            load.assert_not_called()
            client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
            result = client.get(route)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["node_id"], node)
            self.assertIn("usage", result.json())
            load.assert_called_once()
