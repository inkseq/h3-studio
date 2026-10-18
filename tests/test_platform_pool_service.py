"""Demand-triggered original-pair lifecycle; synthetic cloud and CPU ledger only."""
from dataclasses import asdict, replace
import json
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, insert, select, update

from studio_platform.capacity import pool_member_ids, transfer_unsubmitted_capacity
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.on_demand_scaler import OnDemandConfig, OnDemandController, cycle_config
from studio_platform.pool_member_controller import PoolServiceCycle
from studio_platform.production_scaler import RECIPE, save
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE, MULTIMODAL_INPUT_LIMITS
from studio_platform.inference.outputs import NATIVE_DELIVERY
from studio_platform.queue import TaskQueue
from studio_platform.repository import Conflict, capacity_approvals, capacity_pool_members, capacity_waiters, instance_intents, jobs, request_hash
from studio_platform.repository import Scope
from studio_platform.scaler import ProviderFact
from studio_platform.service_policy import TWO_MEMBER_MODE
from studio_platform.settings import Settings
from test_platform_execution_policy import policy
from test_platform_repository import LedgerCase
from test_platform_api import generation_request
import test_platform_on_demand_scaler as single
import test_platform_production_scaler as production
import test_platform_service_policy as service


class PoolServiceTests(LedgerCase):
    tick = single.OnDemandTests.tick
    submit = single.OnDemandTests.submit
    finish = single.OnDemandTests.finish

    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        base = production.configuration(self.root, self.now)
        authority = service.service_policy(self.now, end=base.hard_deadline)
        authority.update(mode=TWO_MEMBER_MODE, member_ids=["a", "b"])
        self.config = OnDemandConfig(**{**asdict(base), "work_dir": self.root/"service",
            "service_policy": authority, "allowed_owners": authority["owner_ids"], "max_cycles": None,
            "scale_policy": {**base.scale_policy, "idle_before_drain_s": 600},
            "launches": base.launches[:1], "manifests": base.manifests[:1], "provider_preparation_timeout_s": 120})
        self.value = policy(self.now)
        self.value.update(pool=self.config.pool, configuration_id=self.config.configuration_id,
            recipe_ids=[RECIPE], budget_accounts=["job-budget"])
        self.value["qualification"].update(evidence_id=self.config.qualification_evidence_id, expires_at=self.now+7000)
        self.value["reservation"].update(expected_runtime_s=300, expires_at=self.now+7000)
        self.value["envelope"].update(max_duration_seconds=6, max_reference_files=0, max_guides=0, allow_first_last=False)
        self.value["envelope"]["controls"].update(video_decode=["tiled"], encoder_device=["cpu"], ref_image_size=["max"])
        self.path = self.root/"policy.json"
        self.path.write_text(json.dumps(self.value)); self.path.chmod(0o600)
        self.config = replace(self.config, execution_policy_sha256=request_hash(self.value))
        self.settings = Settings(self.config.data_dir, database_url=self.url, auth_mode="password",
            public_origin="https://www.sixnine.art", generation_enabled=True, execution_backend="comfy-worker",
            execution_policy_file=self.path)
        self.repo.configure_capacity(max_instances=2, max_physical_gpus=2)
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=6_000_000)
        self.repo.configure_budget("job-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        self.provider = production.FakeProvider(lambda: self.now)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=single.HeartbeatBoot)
        self.controller.initialize()
        self.policies = ExecutionPolicies(self.settings, self.repo)

    def mapping(self):
        with self.repo.engine.connect() as connection:
            return {r["member_id"]: r["intent_id"] for r in connection.execute(select(capacity_pool_members).where(
                capacity_pool_members.c.approval_id == self.controller.current.config.capacity_approval_id)).mappings()}

    def grant(self):
        with self.repo.engine.connect() as connection:
            return dict(connection.execute(select(capacity_approvals).where(
                capacity_approvals.c.id == self.controller.current.config.capacity_approval_id)).mappings().one())

    def start(self):
        scope, job = self.submit()
        self.tick(); self.tick()
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        return scope, job

    def test_confirmed_task_starts_two_original_nodes_b_ready_first_serves_same_job(self):
        self.assertIsInstance(self.controller.current, PoolServiceCycle)
        self.tick()
        self.assertEqual(self.provider.creates, [])
        original = self.provider.create
        def slow_a(tag, launch, **kwargs):
            value = original(tag, launch, **kwargs)
            if len(self.provider.creates) == 1:
                value = ProviderFact("starting", value.instance_id, provider_status="PENDING", preparation_stage="provider_preparing")
                self.provider.facts[tag] = value
            return value
        self.provider.create = slow_a
        scope, job = self.start()
        mapping = self.mapping()
        self.assertEqual(len(self.provider.creates), 2)
        self.assertNotIn(mapping["a"], self.controller.current.boots)
        boot = self.controller.current.boots[mapping["b"]]
        claimed = boot.control.claim(boot.worker, self.config.pool)
        self.assertEqual(claimed.job["id"], job["id"])
        self.assertEqual(self.repo.get_job(scope, job["id"])["request"], job["request"])
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000)
        self.assertEqual(pool_member_ids(self.grant()["payload"]), ("a", "b"))

    def test_warm_requests_keep_current_approval_and_member_claim_fence(self):
        self.start()
        scope, job = self.submit("supervan", "warm")
        self.assertEqual(job["status"], "waiting_capacity")
        self.assertEqual(job["execution_plan"]["capacity_binding"], "pool-members-v1")
        self.assertEqual(job["execution_plan"]["capacity_approval_id"], self.grant()["id"])
        self.tick()
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        self.assertEqual(len(self.provider.creates), 2)

    def test_pool_boot_guard_uses_bound_members_without_assigning_waiter_to_one_node(self):
        scope, job = self.start()
        current = self.controller.current
        lease = current.scaler.acquire(self.config.pool, self.controller.leader_id)
        for intent_id in self.mapping().values():
            self.assertTrue(current._bootstrap_start_allowed(intent_id, lease))
        self.assertFalse(current._bootstrap_start_allowed("unowned", lease))
        self.repo.request_cancel(scope, job["id"])
        for intent_id in self.mapping().values():
            self.assertFalse(current._bootstrap_start_allowed(intent_id, lease))

    def test_peer_unknown_attempt_preserves_owner_while_healthy_member_serves_other_job(self):
        scope, one = self.start()
        a, b = [self.controller.current.boots[self.mapping()[key]] for key in ("a", "b")]
        first = a.control.claim(a.worker, self.config.pool)
        a.control.queue.begin_submission(first.lease)
        a.control.queue.mark_submission_unknown(first.lease)
        a.control.observe(a.worker, one["id"])
        other_scope, two = self.submit("supervan", "different-work")
        self.tick()
        second = b.control.claim(b.worker, self.config.pool)
        self.assertEqual(second.job["id"], two["id"])
        self.assertEqual(self.repo.get_job(scope, one["id"])["current_attempt_id"], first.lease.attempt_id)
        self.assertEqual(a.control.get(a.worker)["state"], "unknown")
        self.assertFalse(self.controller.stopping())
        self.assertEqual(self.grant()["enabled"], 1)
        self.assertEqual(len(self.provider.creates), 2)

    def test_runtime_failure_quarantines_only_exact_member_and_does_not_revoke_peer(self):
        scope, job = self.start()
        a = self.mapping()["a"]
        row = next(r for r in self.repo.list_instance_intents(pool=self.config.pool) if r["id"] == a)
        self.controller.current._boot_failure(row, {"state": "bootstrap_failed"})
        self.tick()
        b = self.controller.current.boots[self.mapping()["b"]]
        self.assertEqual(b.control.claim(b.worker, self.config.pool).job["id"], job["id"])
        self.assertFalse(self.controller.stopping())
        self.assertEqual(self.grant()["enabled"], 1)
        self.assertEqual(len(self.provider.creates), 2)

    def test_idle_pair_waits_600_seconds_and_settlement_before_next_cycle(self):
        scope, job = self.start()
        self.finish(scope, job)
        self.tick()
        for _ in range(37): self.tick()  # 592 seconds with healthy heartbeats.
        self.assertEqual(self.provider.destroys, [])
        self.tick()
        self.assertEqual(len(self.provider.destroys), 2)
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000)
        self.provider.billing = lambda *args: 100_000
        self.tick(); self.tick()
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(self.repo.get_budget("finite-budget")["spent_microusd"], 200_000)
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 0)
        self.assertEqual(len(self.provider.creates), 2)  # New empty cycle rents nothing.

    def test_provider_timeout_retires_only_unused_a_and_preserves_b_and_original_waiter(self):
        original = self.provider.create
        def pending(tag, launch, **kwargs):
            value = original(tag, launch, **kwargs)
            if len(self.provider.creates) == 1:
                value = ProviderFact("starting", value.instance_id, provider_status="PENDING", preparation_stage="configuring_ssh")
                self.provider.facts[tag] = value
            return value
        self.provider.create = pending
        scope, job = self.start()
        mapping = self.mapping()
        before = self.repo.get_budget("job-budget")
        for _ in range(8): self.tick()
        self.assertEqual(self.provider.destroys, [mapping["a"]])
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.grant()["enabled"], 1)
        self.assertEqual(self.repo.get_budget("job-budget"), before)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        b = self.controller.current.boots[mapping["b"]]
        self.assertEqual(b.control.claim(b.worker, self.config.pool).job["id"], job["id"])
        self.assertFalse(self.controller.current.rotation_allowed())

    def test_both_provider_timeouts_report_repair_without_searching_or_new_rental(self):
        original = self.provider.create
        def pending(tag, launch, **kwargs):
            value = original(tag, launch, **kwargs)
            value = ProviderFact("starting", value.instance_id, provider_status="PENDING", preparation_stage="provider_preparing")
            self.provider.facts[tag] = value
            return value
        self.provider.create = pending
        self.provider.billing = lambda *args: 100_000
        scope, job = self.submit()
        self.tick()
        status = self.tick(121)
        self.assertEqual(status["reason"], "queued_task_repair_required")
        self.assertEqual(len(status["member_holds"]), 2)
        self.assertEqual(status["billing_pending"], 0)
        self.assertTrue(status["all_destroyed"])
        self.assertEqual(self.controller.sequence, 1)
        kept = self.repo.get_job(scope, job["id"])
        self.assertEqual(kept["request"], job["request"])
        self.assertEqual(kept["error_code"], "capacity_queued_task_repair_required")
        self.assertEqual(kept["status"], "waiting_capacity")
        self.tick()
        self.assertEqual(len(self.provider.creates), 2)

    def test_whole_pair_ttl_transfers_original_unsubmitted_waiter_without_deadline_or_budget_reset(self):
        scope, job = self.start()
        old_approval = self.grant()["id"]
        with self.repo.transaction() as connection:
            before = dict(connection.execute(select(capacity_waiters).where(
                capacity_waiters.c.job_id == job["id"])).mappings().one())
            connection.execute(update(instance_intents).where(instance_intents.c.pool == self.config.pool)
                .values(hard_deadline=self.now+32))
        reserved = self.repo.get_budget("job-budget")["reserved_microusd"]
        self.provider.billing = lambda *args: 100_000
        self.tick(); self.tick()
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(len(self.provider.creates), 2)
        moved = self.repo.get_job(scope, job["id"])
        self.assertEqual(moved["request"], job["request"])
        self.assertEqual(moved["status"], "waiting_capacity")
        self.assertEqual(moved["attempt_no"], 0)
        self.assertEqual(moved["execution_plan"]["capacity_binding"], "pool-members-v1")
        self.assertNotEqual(moved["execution_plan"]["capacity_approval_id"], old_approval)
        with self.repo.engine.connect() as connection:
            after = connection.execute(select(capacity_waiters).where(
                capacity_waiters.c.job_id == job["id"])).mappings().one()
        self.assertEqual(after["deadline"], before["deadline"])
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], reserved)
        self.tick(); self.tick()
        self.assertEqual(len(self.provider.creates), 4)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")

    def test_restart_reconstructs_exact_original_members_ports_and_never_reposts_create(self):
        scope, job = self.start()
        mapping = self.mapping()
        ports = {key: self.controller.current.port_for(value) for key, value in mapping.items()}
        previous = self.controller
        restarted = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=single.HeartbeatBoot)
        restarted.initialize()
        self.assertEqual(restarted.current._managed()[0][0]["pool"], self.config.pool)
        self.assertEqual({key: restarted.current.port_for(value) for key,value in mapping.items()}, ports)
        self.assertEqual(restarted.tick()["decision"], "not_leader")
        # Synthetic natural shutdown then expiry hands the sole pool leader to
        # the new controller. Real ProductionBoot process ownership has its own
        # tests; no fake lost handle is used as child-stop or deletion proof.
        previous.current.scaler.leader_seconds = 1
        previous.current.scaler.acquire(self.config.pool, previous.leader_id)
        self.now += 2
        self.controller = restarted
        self.tick()
        self.assertEqual(self.mapping(), mapping)
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_job(scope, job["id"])["request"], job["request"])
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000)

    def test_unresolved_submission_keeps_whole_pool_nonidle_after_600_seconds(self):
        scope, job = self.start()
        a = self.controller.current.boots[self.mapping()["a"]]
        claim = a.control.claim(a.worker, self.config.pool)
        a.control.queue.begin_submission(claim.lease)
        a.control.queue.mark_submission_unknown(claim.lease)
        a.control.observe(a.worker, job["id"])
        for _ in range(40): self.tick()
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self.repo.get_job(scope, job["id"])["current_attempt_id"], claim.lease.attempt_id)
        self.assertEqual(self.controller.sequence, 1)

    def test_revocation_drains_pool_without_releasing_unknown_attempt_obligations(self):
        scope, job = self.start()
        a = self.controller.current.boots[self.mapping()["a"]]
        claim = a.control.claim(a.worker, self.config.pool)
        a.control.queue.begin_submission(claim.lease)
        a.control.queue.mark_submission_unknown(claim.lease)
        a.control.observe(a.worker, job["id"])
        self.repo.set_capacity_approval_enabled(self.grant()["id"], enabled=False)
        self.tick()
        self.assertTrue(self.controller.stopping())
        self.assertEqual(self.repo.get_job(scope, job["id"])["current_attempt_id"], claim.lease.attempt_id)
        self.assertGreater(self.repo.get_budget("job-budget")["reserved_microusd"], 0)
        self.assertNotIn(self.mapping()["a"], self.provider.destroys)

    def test_fast_intervening_job_restarts_both_members_full_idle_interval(self):
        scope, job = self.start()
        self.finish(scope, job)
        self.tick()
        for _ in range(36): self.tick()
        later_scope, later = self.submit("supervan", "fast-between-polls")
        self.controller.current.cold.advance_once(self.grant()["id"])
        self.finish(later_scope, later)
        self.tick()
        for _ in range(37): self.tick()
        self.assertEqual(self.provider.destroys, [])
        self.tick()
        self.assertEqual(len(self.provider.destroys), 2)

    def test_second_member_budget_refusal_does_not_stop_first_ready_member(self):
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=2_000_000)
        scope, job = self.start()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(set(self.mapping()), {"a"})
        boot = self.controller.current.boots[self.mapping()["a"]]
        self.assertEqual(boot.control.claim(boot.worker, self.config.pool).job["id"], job["id"])
        self.assertFalse(self.controller.stopping())
        self.assertEqual(self.repo.get_budget("finite-budget")["limit_microusd"], 2_000_000)

    def test_cumulative_service_ceiling_is_atomic_below_larger_account_limit(self):
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=10_000_000)
        with self.repo.transaction() as connection:
            self.repo._reserve(connection, self.config.scope, ["finite-budget"], "instance",
                "historical-synthetic-rental", 3_000_000)
            self.repo._settle(connection, "instance", "historical-synthetic-rental", 3_000_000)
        scope, job = self.start()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(set(self.mapping()), {"a"})
        budget = self.repo.get_budget("finite-budget")
        self.assertEqual(budget["spent_microusd"], 3_000_000)
        self.assertEqual(budget["reserved_microusd"], 2_000_000)
        self.assertEqual(budget["limit_microusd"], 10_000_000)
        self.tick()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.repo.list_instance_intents(pool=self.config.pool)), 1)
        a = self.controller.current.boots[self.mapping()["a"]]
        self.assertEqual(a.control.claim(a.worker, self.config.pool).job["id"], job["id"])

    def test_quarantine_write_failure_fences_claims_and_old_partial_hold_replays_before_activation(self):
        scope, original = self.start()
        waiting_scope, waiting = self.submit("supervan", "waiting-through-repair")
        rows = {r["id"]: r for r in self.repo.list_instance_intents(pool=self.config.pool)}
        a, b = (rows[self.mapping()[member]] for member in ("a", "b"))
        cycle = self.controller.current
        with patch("studio_platform.pool_member_controller.save", side_effect=OSError("synthetic receipt failure")):
            with self.assertRaises(OSError):
                cycle._boot_failure(a, {"state": "bootstrap_failed"})
        self.assertEqual(cycle.boots[a["id"]].control.get(cycle.boots[a["id"]].worker)["state"], "draining")
        self.assertFalse((cycle.config.work_dir/"member-holds"/(a["id"]+".json")).exists())
        self.assertIsNotNone(cycle.member_hold(a))  # Committed ledger proof survives the lost file.
        # Reproduce the interrupted old file-before-ledger ordering. A restart
        # must consume this evidence before any waiter can be activated on B.
        save(cycle.config.work_dir/"member-holds"/(b["id"]+".json"), {
            "config_hash": cycle.config.fingerprint(), "intent_id": b["id"],
            "instance_id": b["provider_instance_id"], "sources": cycle.config.source_sha256,
            "reason": "bootstrap_failed", "observed_at": self.now})
        status = self.tick()
        self.assertEqual(len(status["member_holds"]), 2)
        self.assertEqual(status["reason"], "queued_task_repair_required")
        kept = self.repo.get_job(waiting_scope, waiting["id"])
        self.assertEqual(kept["status"], "waiting_capacity")
        self.assertEqual(kept["error_code"], "capacity_queued_task_repair_required")
        self.assertEqual(self.repo.get_job(scope, original["id"])["attempt_no"], 0)
        self.assertIn(cycle.boots[b["id"]].control.get(cycle.boots[b["id"]].worker)["state"], ("draining", "retired"))
        self.assertEqual(len(self.provider.creates), 2)
        self.provider.billing = lambda *args: 100_000
        self.tick(); self.tick()
        self.assertEqual(self.controller.sequence, 1)
        self.assertFalse(cycle.rotation_allowed())

    def test_pair_transfer_rejects_unbound_or_missing_original_waiter_without_mutation(self):
        scope, job = self.start()
        previous = self.grant()["id"]
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.pool == self.config.pool)
                .values(hard_deadline=self.now+16))
        self.provider.billing = lambda *args: 100_000
        with patch.object(self.controller, "_can_rotate", return_value=False):
            self.tick()
        next_cycle = PoolServiceCycle(self.repo, self.settings, cycle_config(self.config, 2),
            provider=self.provider, boot_factory=single.HeartbeatBoot)
        next_cycle.initialize()
        self.repo.set_capacity_approval_enabled(previous, enabled=False)
        current = self.repo.get_job(scope, job["id"])
        with self.repo.engine.connect() as connection:
            waiter = dict(connection.execute(select(capacity_waiters).where(
                capacity_waiters.c.job_id == job["id"])).mappings().one())
        for field in ("capacity_binding", "capacity_approval_id", "capacity_approval_hash", "waiter"):
            with self.subTest(missing=field):
                execution = dict(current["execution_plan"])
                if field != "waiter":
                    execution.pop(field)
                with self.repo.transaction() as connection:
                    connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(execution_plan=execution))
                    if field == "waiter":
                        connection.execute(delete(capacity_waiters).where(capacity_waiters.c.job_id == job["id"]))
                with self.assertRaisesRegex(Conflict, "capacity_pool_transfer_binding_mismatch"):
                    transfer_unsubmitted_capacity(self.repo, previous, next_cycle.config.capacity_approval_id,
                        allowed_owners=self.config.allowed_owners, children_done_confirmed=True)
                after = self.repo.get_job(scope, job["id"])
                self.assertEqual(after["execution_plan"], execution)
                self.assertEqual(after["request"], current["request"])
                self.assertEqual(after["attempt_no"], 0)
                with self.repo.transaction() as connection:
                    connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(execution_plan=current["execution_plan"]))
                    if field == "waiter":
                        connection.execute(insert(capacity_waiters).values(**waiter))

    def test_duplicate_provider_instance_is_held_without_boot_or_third_rental(self):
        from studio_platform.repository import Conflict
        original = self.provider.create
        known = []
        def duplicate(tag, launch, **kwargs):
            value = original(tag, launch, **kwargs)
            if not known:
                known.append(value.instance_id)
            value = replace(value, instance_id=known[0])
            self.provider.facts[tag] = value
            return value
        self.provider.create = duplicate
        self.submit()
        with self.assertRaisesRegex(Conflict, "duplicate_provider_instance"):
            self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.controller.current.boots, {})
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000)

    def test_native_wangp_pair_keeps_manifest_delivery_and_both_start_guards(self):
        class NativeBoot(single.HeartbeatBoot):
            def __init__(self, repo, provider, config, intent, port, **kwargs):
                self.repo, self.config, self.intent, self.port = repo, config, intent, port
                self.drained = self.closed = False
                self.worker = "lium-"+intent["id"].replace("-", "")
                self.control = WorkerControl(repo)
                self.control.register(WorkerSpec(self.worker, config.pool, "lium", intent["provider_instance_id"],
                    ("GPU-"+intent["id"],), (RECIPE,), production.MODEL, config.configuration_id,
                    "wangp-worker", config.engine_manifest_digest, output_delivery=NATIVE_DELIVERY))
                self.control.mark_ready(self.worker, upstream_idle_confirmed=True)
        digest = "a"*64
        self.config = replace(self.config, pool="native-pair", configuration_id="native-pair-config",
            cycle_id="native-pair-service", capacity_approval_id="native-pair-approval",
            execution_backend="wangp-worker", engine_manifest_digest=digest, output_delivery=NATIVE_DELIVERY,
            qualification_profile=QUEUED_TASK_PROFILE, work_dir=self.root/"native-pair",
            source_sha256={name:"b"*64 for name in ("wangp-bootstrap.py", "wangp-manifest.json", "wangp-runtime.json", "wangp-package.tar.gz")},
            launches=[{**self.config.launches[0], "configuration_id": "native-pair-config"}],
            manifests=[{**self.config.manifests[0], "configuration_id": "native-pair-config"}])
        self.value.update(backend="wangp-worker", pool=self.config.pool, configuration_id=self.config.configuration_id,
            engine_manifest_digest=digest, output_delivery=NATIVE_DELIVERY)
        self.value["qualification"].update(status="runtime_required", profile=QUEUED_TASK_PROFILE)
        self.value["reservation"]["expected_runtime_s"] = 1800
        self.value["envelope"].update(max_duration_seconds=362/24, input_limits={**MULTIMODAL_INPUT_LIMITS,
            "max_images": 0, "max_videos": 0, "max_audios": 0, "guide_kinds": [], "guide_recipe_ids": [], "allow_video_audio": False},
            controls={"sampler_name": ["euler"], "scheduler": ["auto"], "video_decode": ["tiled"],
                "audio_decode": ["normal"], "encoder_device": ["default"]})
        self.path.write_text(json.dumps(self.value))
        self.config = replace(self.config, execution_policy_sha256=request_hash(self.value))
        self.settings = replace(self.settings, execution_backend="wangp-worker")
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=NativeBoot)
        self.controller.initialize()
        self.policies = ExecutionPolicies(self.settings, self.repo)
        request = generation_request()
        request["controls"] = {"duration": 5, "resolution": "480P"}
        compiled, fingerprint = compile_request(request, lambda _: None, backend="wangp-worker")
        scope = Scope("sixnine", "superdan", "native-story")
        admission = self.policies.evaluate(compiled, scope, fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        plan = self.repo.create_plan(scope, compiled, admission.execution, expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost)
        job = self.repo.create_job(scope, plan["id"], "native-pair-proof", initial_status="waiting_capacity",
            budget_account_ids=admission.execution["budget_account_ids"])
        self.tick(); self.tick()
        for boot in self.controller.current.boots.values():
            self.assertTrue(boot.start_guard())
        self.assertEqual(len(self.controller.current.boots), 2)
        after = self.repo.get_job(scope, job["id"])
        self.assertEqual(after["status"], "queued")
        self.assertEqual(after["execution_plan"]["engine_manifest_digest"], digest)
        self.assertEqual(after["execution_plan"]["delivery_spec"]["frame_count"], 124)
        self.assertEqual(after["request"], job["request"])
