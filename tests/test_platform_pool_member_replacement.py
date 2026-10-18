"""Opt-in replacement generations: real isolated ledger and fake provider only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from sqlalchemy import insert, select, update

from studio_platform.capacity import capacity_member_claim_allowed
from studio_platform.control import WorkerControl
from studio_platform.on_demand_scaler import OnDemandController
from studio_platform.pool_member_generations import bindings, successful_generation
from studio_platform.repository import (Conflict, budget_reservations, capacity_member_generations,
    instance_intents, jobs, scaler_receipts, registered_workers, attempts, artifacts)
from studio_platform.scaler import ProviderFact
from studio_platform.service_policy import validate_service_policy, ServicePolicyError
from test_platform_repository import LedgerCase
import test_platform_pool_service as pool_fixture
from test_platform_on_demand_scaler import HeartbeatBoot
from studio_platform.on_demand_scaler import OnDemandConfig
from studio_platform.production_scaler import RECIPE
from studio_platform.settings import Settings
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.repository import request_hash
from test_platform_execution_policy import policy
import test_platform_production_scaler as production
import test_platform_service_policy as service


class ReplacementTests(LedgerCase):
    submit = pool_fixture.PoolServiceTests.submit
    tick = pool_fixture.PoolServiceTests.tick
    grant = pool_fixture.PoolServiceTests.grant

    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        base = production.configuration(self.root, self.now)
        authority = service.service_policy(self.now, end=base.hard_deadline)
        authority.update(mode="continuing-two-members", member_ids=["a", "b"],
            member_replacement={"version": 1, "max_replacements": 3, "backoff_s": 60, "failure_limit": 2})
        self.config = OnDemandConfig(**{**asdict(base), "work_dir": self.root/"replacement",
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
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        self.repo.configure_budget("job-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        self.provider = production.FakeProvider(lambda: self.now)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        self.policies = ExecutionPolicies(self.settings, self.repo)
        # Larger existing accounts make the configured lower service ceiling,
        # rather than a coincidentally low test account, observable.
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=30_000_000)

    def chain(self):
        with self.repo.engine.connect() as connection:
            return bindings(connection, self.grant())

    def row(self, intent_id):
        return next(r for r in self.repo.list_instance_intents(pool=self.config.pool) if r["id"] == intent_id)

    def begin(self):
        scope, job = self.submit()
        self.tick(); self.tick()
        _, current = self.chain()
        return scope, job, current["a"]["intent_id"], current["b"]["intent_id"]

    def close_member(self, intent_id, *, proof=True, failure=True, settled=True):
        cycle = self.controller.current; row = self.row(intent_id)
        if failure:
            cycle._boot_failure(row, {"state": "bootstrap_failed"})
        boot = cycle.boots[intent_id]
        boot.request_drain()
        boot.control.retire(boot.worker, upstream_idle_confirmed=True)
        fact = ProviderFact("destroyed", row["provider_instance_id"], actual_cost_microusd=100_000 if settled else None)
        self.provider.facts[intent_id] = fact
        lease = cycle.scaler.acquire(self.config.pool, cycle.leader_id)
        fact, observed = cycle.scaler._call(row, "reconcile")
        cycle.scaler._apply(lease, intent_id, fact, observed)
        if proof:
            with self.repo.transaction() as conn:
                conn.execute(insert(scaler_receipts).values(id=str(uuid.uuid4()), intent_id=intent_id,
                    operation="member_local_closed", observed_at=self.now, facts={
                        "version": 1, "intent_id": intent_id, "instance_id": row["provider_instance_id"],
                        "worker_id": boot.worker, "config_hash": cycle.config.fingerprint(),
                        "sources": cycle.config.source_sha256, "local_port": boot.port,
                        "bootstrap_identity": {"intent_id": intent_id, "instance_id": row["provider_instance_id"],
                            "configuration_id": cycle.config.configuration_id, "sources": cycle.config.source_sha256},
                        "kind": "owned_fleet_exited", "fleet_hash": "a"*64,
                        "children": [{"worker_id": boot.worker, "pid": 123, "exit_code": 0}],
                        "local_transport_closed": True}))
        return row

    def create(self, member="a"):
        cycle = self.controller.current
        lease = cycle.scaler.acquire(self.config.pool, cycle.leader_id)
        return cycle.members.create_once(lease, cycle.config.capacity_approval_id, member)

    def test_legacy_policy_is_unchanged_and_limits_and_ports_are_explicit(self):
        legacy = dict(self.config.service_policy); legacy.pop("member_replacement")
        self.assertNotIn("member_replacement", validate_service_policy(legacy))
        for patch_value in ({"max_replacements": True}, {"backoff_s": 0}, {"failure_limit": 0}, {"version": 2}):
            value = {**self.config.service_policy, "member_replacement": {**self.config.service_policy["member_replacement"], **patch_value}}
            with self.assertRaises(ServicePolicyError): validate_service_policy(value)
        with self.assertRaises(ValueError): replace(self.config, port_start=65533)

    def test_one_retired_member_replaced_atomically_without_rebinding_job_or_peer(self):
        scope, job, a, b = self.begin(); old = self.repo.get_job(scope, job["id"])
        sibling = WorkerControl(self.repo).get("lium-"+b.replace("-", ""))
        self.close_member(a)
        self.assertEqual(self.create()["reason"], "member_replacement_backoff")
        self.now += 61
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.create(), range(2)))
        self.assertEqual(len({r["intent_id"] for r in results}), 1)
        history, current = self.chain(); newest = current["a"]["intent_id"]
        self.assertNotEqual(newest, a); self.assertEqual(current["b"]["intent_id"], b)
        self.assertEqual(current["a"]["previous_intent_id"], a); self.assertEqual(len(history), 3)
        self.assertEqual(len(self.provider.creates), 3)
        self.assertEqual(WorkerControl(self.repo).get(sibling["id"]), sibling)
        after = self.repo.get_job(scope, job["id"])
        for field in ("id", "request", "request_hash", "execution_plan", "current_attempt_id", "attempt_no"):
            self.assertEqual(after[field], old[field])
        self.assertEqual(self.row(newest)["hard_deadline"], min(self.grant()["expires_at"], self.config.hard_deadline))
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000)
        self.assertEqual(self.repo.get_budget("finite-budget")["spent_microusd"], 100_000)
        self.assertEqual(self.controller.current.port_for(newest), self.config.port_start+2)
        self.assertEqual(self.controller.current.port_for(b), self.config.port_start+1)

    def test_unknown_new_rental_never_replayed_and_old_ready_worker_cannot_claim(self):
        scope, job, a, b = self.begin(); self.close_member(a); self.now += 61
        self.provider.uncertain = True
        result = self.create(); self.assertEqual(result["provider_state"], "unknown")
        again = self.create(); self.assertEqual(result["intent_id"], again["intent_id"])
        self.assertEqual(len(self.provider.creates), 3)
        worker = WorkerControl(self.repo).get("lium-"+a.replace("-", ""))
        # Even contradictory stale-ready metadata may not resurrect generation0.
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == a).values(state="ready"))
        with self.repo.engine.connect() as conn:
            self.assertFalse(capacity_member_claim_allowed(self.repo, conn, self.repo.get_job(scope, job["id"]), worker))
        self.assertEqual(self.row(result["intent_id"])["state"], "creation_unknown")

    def test_missing_stop_proof_or_final_bill_never_replaces_or_drains_b(self):
        _, _, a, b = self.begin(); self.close_member(a, proof=False, settled=False); self.now += 61
        self.assertEqual(self.create()["reason"], "member_replacement_billing_unconfirmed")
        fact = ProviderFact("destroyed", self.row(a)["provider_instance_id"], actual_cost_microusd=100_000)
        self.provider.facts[a] = fact; cycle = self.controller.current
        observed, at = cycle.scaler._call(self.row(a), "reconcile")
        cycle.scaler._apply(cycle.scaler.acquire(self.config.pool, cycle.leader_id), a, observed, at)
        self.assertEqual(self.create()["reason"], "member_replacement_local_stop_unconfirmed")
        self.assertEqual(len(self.provider.creates), 2)
        self.assertFalse(WorkerControl(self.repo).get("lium-"+b.replace("-", ""))["drain_requested"])

    def test_no_waiting_demand_does_not_finance_replacement(self):
        scope, job, a, _ = self.begin(); self.close_member(a); self.now += 61
        self.repo.request_cancel(scope, job["id"])
        self.assertEqual(self.create()["reason"], "member_replacement_no_waiting_demand")
        self.assertEqual(len(self.provider.creates), 2)

    def test_atomic_failure_rolls_back_generation_account_action(self):
        _, _, a, _ = self.begin(); self.close_member(a); self.now += 61
        before = self.repo.get_budget("finite-budget")
        with patch.object(self.controller.current.scaler, "_preparation_receipt", side_effect=RuntimeError("synthetic write failure")):
            with self.assertRaises(RuntimeError): self.create()
        self.assertEqual(len(self.chain()[0]), 2)
        self.assertEqual(self.repo.get_budget("finite-budget"), before)
        self.assertEqual(len(self.provider.creates), 2)

    def test_service_ceiling_rejects_replacement_without_resetting_higher_account(self):
        _, _, a, _ = self.begin(); self.close_member(a); self.now += 61
        with self.repo.transaction() as connection:
            from studio_platform.repository import budget_accounts
            connection.execute(update(budget_accounts).where(budget_accounts.c.id == "finite-budget")
                .values(spent_microusd=2_100_000))
        from studio_platform.repository import BudgetExceeded
        with self.assertRaises(BudgetExceeded): self.create()
        self.assertEqual(len(self.chain()[0]), 2)
        self.assertEqual(self.repo.get_budget("finite-budget")["spent_microusd"], 2_100_000)

    def test_restart_preserves_newest_binding_ports_and_once_only_action(self):
        _, _, a, _ = self.begin(); self.close_member(a); self.now += 61
        newest = self.create()["intent_id"]
        port = self.controller.current.port_for(newest)
        reopened = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        reopened.leader_id = self.controller.leader_id
        reopened.initialize()
        self.assertEqual(reopened.current.port_for(newest), port)
        self.controller = reopened
        self.assertEqual(self.create()["intent_id"], newest)
        self.assertEqual(len(self.provider.creates), 3)

    def test_corrupt_generation_chain_fails_closed(self):
        _, _, a, b = self.begin(); self.close_member(a); self.now += 61
        self.create()
        with self.repo.transaction() as conn:
            conn.execute(update(capacity_member_generations).values(previous_intent_id=b))
        with self.assertRaisesRegex(Conflict, "capacity_member_generation_chain_invalid"):
            self.create()

    def test_unknown_attempt_keeps_obligation_and_blocks_replacement(self):
        scope, job, a, _ = self.begin(); boot = self.controller.current.boots[a]
        claim = boot.control.claim(boot.worker, self.config.pool)
        boot.control.queue.begin_submission(claim.lease); boot.control.queue.mark_submission_unknown(claim.lease)
        boot.control.observe(boot.worker, job["id"])
        row = self.row(a); cycle = self.controller.current
        fact = ProviderFact("destroyed", row["provider_instance_id"], actual_cost_microusd=100_000)
        self.provider.facts[a] = fact
        observed, at = cycle.scaler._call(row, "reconcile")
        cycle.scaler._apply(cycle.scaler.acquire(self.config.pool, cycle.leader_id), a, observed, at)
        self.now += 61
        result = self.create()
        self.assertIn(result["reason"], ("member_replacement_worker_unretired", "member_replacement_attempt_unresolved"))
        self.assertEqual(self.repo.get_job(scope, job["id"])["current_attempt_id"], claim.lease.attempt_id)
        self.assertEqual(len(self.provider.creates), 2)

    def complete_on(self, member_intent, scope, job):
        boot = self.controller.current.boots[member_intent]
        claim = boot.control.claim(boot.worker, self.config.pool)
        self.assertIsNotNone(claim); self.assertEqual(claim.job["id"], job["id"])
        queue = boot.control.queue
        queue.begin_submission(claim.lease); queue.record_submitted(claim.lease, "synthetic-upstream")
        queue.begin_collection(claim.lease)
        queue.complete(claim.lease, [{"kind": "video", "object_key": "synthetic/"+job["id"]+".mp4",
            "size_bytes": 1, "sha256": "b"*64, "validated": True}], actual_cost_microusd=0)
        boot.control.observe(boot.worker, job["id"])

    def test_healthy_b_claims_while_replacement_a_is_unknown(self):
        scope, job, a, b = self.begin(); self.close_member(a); self.now += 61
        self.provider.uncertain = True; self.create()
        boot = self.controller.current.boots[b]
        boot.control.heartbeat(boot.worker, boot.control.get(boot.worker)["fence"])
        self.complete_on(b, scope, job)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "succeeded")
        self.assertEqual(len(self.provider.creates), 3)

    def test_failure_streak_survives_restart_and_sibling_success_does_not_reset_it(self):
        scope, job, a, b = self.begin(); self.close_member(a); self.now += 61
        a1 = self.create()["intent_id"]
        self.tick(); self.tick()
        self.complete_on(b, scope, job)
        self.submit(story="new-demand")
        self.close_member(a1); self.now += 121
        held = self.create()
        self.assertEqual(held["reason"], "member_replacement_failure_limit")
        self.assertEqual(held["consecutive_failures"], 2)
        reopened = OnDemandController(self.repo, self.settings, self.config, provider=self.provider, boot_factory=HeartbeatBoot)
        reopened.leader_id = self.controller.leader_id; reopened.initialize(); self.controller = reopened
        self.assertEqual(self.create()["reason"], "member_replacement_failure_limit")
        self.assertEqual(len(self.provider.creates), 3)

    def test_successful_own_generation_resets_streak_but_not_absolute_cap(self):
        # New explicit fixture policy is frozen before the first reservation.
        self.repo.set_capacity_approval_enabled(self.grant()["id"], enabled=False)
        self.config = replace(self.config, capacity_approval_id="capped-approval", cycle_id="capped-cycle",
            work_dir=self.root/"capped", service_policy={**self.config.service_policy,
                "member_replacement": {**self.config.service_policy["member_replacement"], "max_replacements": 1}})
        self.controller = OnDemandController(self.repo, self.settings, self.config, provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        scope, job, a, b = self.begin(); self.close_member(a); self.now += 61
        a1 = self.create()["intent_id"]; self.tick(); self.tick()
        self.complete_on(a1, scope, job)
        with self.repo.engine.connect() as conn:
            self.assertTrue(successful_generation(conn, a1)); self.assertFalse(successful_generation(conn, b))
        self.submit(story="new-capped-demand")
        self.close_member(a1, failure=False); self.now += 121
        self.assertEqual(self.create()["reason"], "member_replacement_limit")
        self.assertEqual(len(self.provider.creates), 3)

    def test_pre_post_demand_loss_cannot_start_new_rental_or_release_peer(self):
        scope, job, a, b = self.begin(); self.close_member(a); self.now += 61
        original = self.controller.current.members.before_create
        def cancel(*args):
            self.repo.request_cancel(scope, job["id"])
            return original(*args)
        with patch.object(self.controller.current.members, "before_create", side_effect=cancel):
            result = self.create()
        self.assertEqual(result["provider_state"], "not_created")
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 2_000_000)
        self.assertFalse(WorkerControl(self.repo).get("lium-"+b.replace("-", ""))["drain_requested"])
        self.now += 61
        self.submit(story="new-demand-after-no-rent")
        next_result = self.create()
        self.assertEqual(next_result["state"], "creation_observed")
        self.assertNotEqual(next_result["intent_id"], result["intent_id"])
        history, current = self.chain()
        self.assertEqual(current["a"]["generation"], 2)
        self.assertEqual(current["a"]["previous_intent_id"], result["intent_id"])
        self.assertEqual(current["b"]["intent_id"], b)
        self.assertEqual(len(history), 4)
        self.assertEqual(len(self.provider.creates), 3)  # No paid call for ordinal1.
        self.assertEqual(self.controller.current.port_for(next_result["intent_id"]), self.config.port_start+4)

    def test_no_rent_requires_exact_positive_create_receipt_and_does_not_reset_failures(self):
        scope, job, a, _ = self.begin(); self.close_member(a); self.now += 61
        original = self.controller.current.members.before_create
        def cancel(*args):
            self.repo.request_cancel(scope, job["id"])
            return original(*args)
        with patch.object(self.controller.current.members, "before_create", side_effect=cancel):
            no_rent = self.create()["intent_id"]
        self.now += 61; self.submit(story="after-no-rent")
        with self.repo.transaction() as conn:
            receipt = conn.execute(select(scaler_receipts).where(scaler_receipts.c.intent_id == no_rent,
                scaler_receipts.c.operation == "create")).mappings().one()
            conn.execute(update(scaler_receipts).where(scaler_receipts.c.id == receipt["id"])
                .values(facts={**receipt["facts"], "absence_confirmed": False}))
        self.assertEqual(self.create()["reason"], "member_replacement_removal_unconfirmed")
        with self.repo.transaction() as conn:
            conn.execute(update(scaler_receipts).where(scaler_receipts.c.id == receipt["id"]).values(facts=receipt["facts"]))
            gate = self.controller.current.members.replacement_status(conn, self.grant(), "a")
            self.assertEqual(gate["consecutive_failures"], 1)
        self.provider.uncertain = True
        unknown = self.create()["intent_id"]
        self.assertEqual(self.create()["intent_id"], unknown)
        self.assertEqual(len(self.provider.creates), 3)

    def test_actual_boot_closure_requires_owned_exited_child_and_successful_transport_close(self):
        from studio_platform.production_scaler_boot import ProductionBoot
        _, _, a, _ = self.begin(); self.close_member(a, proof=False)
        c = self.controller.current.config; row = self.row(a)
        boot = ProductionBoot(self.repo, self.provider, c, row, self.config.port_start)
        worker_id = "lium-"+a.replace("-", "")
        proc = SimpleNamespace(pid=234, poll=lambda: None)
        config = SimpleNamespace(slots=[SimpleNamespace(enabled=True, spec=SimpleNamespace(worker_id=worker_id))], fingerprint=lambda: "f"*64)
        boot.fleet = SimpleNamespace(config=config, children={worker_id: proc})
        directory = boot.config.work_dir/a; directory.mkdir(parents=True)
        files, _ = boot._sources()
        (directory/"bootstrap-state.json").write_text(json.dumps({"phase": "fleet_started", "local_port": boot.config.local_port,
            "identity": boot._identity(row, files)}))
        self.assertFalse(boot.close_if_safe(destroyed=True))
        proc.poll = lambda: 0
        class BrokenTransport:
            def close(self): raise OSError("synthetic close failed")
        boot.host = BrokenTransport()
        with self.assertRaises(OSError): boot.close_if_safe(destroyed=True)
        from studio_platform.pool_member_generations import one_receipt
        with self.repo.engine.connect() as conn:
            self.assertIsNone(one_receipt(conn, a, "member_local_closed"))
        closed = []
        boot.host = SimpleNamespace(close=lambda: closed.append(True))
        self.assertTrue(boot.close_if_safe(destroyed=True)); self.assertEqual(closed, [True])
        with self.repo.engine.connect() as conn:
            proof = one_receipt(conn, a, "member_local_closed")
            self.assertEqual(proof["children"], [{"worker_id": worker_id, "pid": 234, "exit_code": 0}])
            self.assertTrue(proof["local_transport_closed"])
        self.now += 61; self.assertEqual(self.create()["state"], "creation_observed")

    def test_lost_fleet_handles_are_not_a_closure_certificate(self):
        from studio_platform.production_scaler_boot import ProductionBoot
        _, _, a, _ = self.begin(); self.close_member(a, proof=False)
        boot = ProductionBoot(self.repo, self.provider, self.controller.current.config, self.row(a), self.config.port_start)
        directory = boot.config.work_dir/a; directory.mkdir(parents=True)
        (directory/"fleet.json").write_text("{}")
        self.assertFalse(boot.close_if_safe(destroyed=True))
        self.now += 61
        self.assertEqual(self.create()["reason"], "member_replacement_local_stop_unconfirmed")

    def test_restart_failed_preparation_without_owned_transport_cannot_mint_stop_proof(self):
        from studio_platform.production_scaler_boot import ProductionBoot
        from studio_platform.pool_member_generations import one_receipt
        _, _, a, _ = self.begin(); self.close_member(a, proof=False)
        with self.repo.transaction() as conn:
            # This fixture represents a never-registered failed preparation.
            from sqlalchemy import delete
            from studio_platform.repository import registered_devices
            conn.execute(delete(registered_devices).where(registered_devices.c.worker_id == "lium-"+a.replace("-", "")))
            conn.execute(delete(registered_workers).where(registered_workers.c.id == "lium-"+a.replace("-", "")))
        boot = ProductionBoot(self.repo, self.provider, self.controller.current.config, self.row(a), self.config.port_start)
        directory = boot.config.work_dir/a; directory.mkdir(parents=True)
        files, _ = boot._sources()
        (directory/"bootstrap-state.json").write_text(json.dumps({"phase": "bootstrap_failed",
            "identity": boot._identity(self.row(a), files)}))
        reopened = ProductionBoot(self.repo, self.provider, self.controller.current.config, self.row(a), self.config.port_start)
        reopened.host = SimpleNamespace(close=lambda: None)  # Even a new SSH connection cannot prove old ownership.
        self.assertTrue(reopened.close_if_safe(destroyed=True))  # Cleanup is safe, replacement remains held.
        with self.repo.engine.connect() as conn:
            self.assertIsNone(one_receipt(conn, a, "member_local_closed"))
        self.now += 61
        self.assertEqual(self.create()["reason"], "member_replacement_local_stop_unconfirmed")
        boot.host = SimpleNamespace(close=lambda: None)
        self.assertTrue(boot.close_if_safe(destroyed=True))
        self.assertEqual(self.create()["state"], "creation_observed")

    def test_positive_never_bootstrapped_timeout_can_replace_without_a_worker_row(self):
        original = self.provider.create
        def pending(tag, launch, **kwargs):
            value = original(tag, launch, **kwargs)
            if len(self.provider.creates) == 1:
                value = ProviderFact("starting", value.instance_id, provider_status="PENDING", preparation_stage="configuring_ssh")
                self.provider.facts[tag] = value
            return value
        self.provider.create = pending
        scope, job, a, b = self.begin()
        self.assertNotIn(a, self.controller.current.boots)
        self.provider.billing = lambda *args: 100_000
        for _ in range(13): self.tick()
        # The normal separate final-invoice poll is authoritative even when the
        # removal response did not yet include its cost; no synthetic fact added.
        self.tick()
        history, current = self.chain()
        self.assertEqual(len(history), 3)
        self.assertNotEqual(current["a"]["intent_id"], a)
        self.assertEqual(current["b"]["intent_id"], b)
        self.assertEqual(len(self.provider.creates), 3)
        self.assertEqual(self.repo.get_job(scope, job["id"])["attempt_no"], 0)
        with self.repo.engine.connect() as conn:
            self.assertIsNone(conn.execute(select(registered_workers).where(
                registered_workers.c.id == "lium-"+a.replace("-", ""))).first())

    def test_authority_revocation_and_original_deadline_block_new_generation(self):
        _, _, a, _ = self.begin(); self.close_member(a); self.now += 61
        self.repo.set_capacity_approval_enabled(self.grant()["id"], enabled=False)
        self.assertEqual(self.create()["reason"], "member_replacement_no_waiting_demand")
        self.repo.set_capacity_approval_enabled(self.grant()["id"], enabled=True)
        self.now = self.grant()["expires_at"]+1
        self.assertEqual(self.create()["reason"], "member_replacement_no_waiting_demand")
        self.assertEqual(len(self.provider.creates), 2)

    def test_only_own_validated_real_output_resets_streak_and_certificate_is_exact(self):
        scope, job, a, _ = self.begin(); self.close_member(a); self.now += 61
        a1 = self.create()["intent_id"]; self.tick(); self.tick()
        self.complete_on(a1, scope, job)
        self.submit(story="after-own-success")
        self.close_member(a1); self.now += 61
        with self.repo.transaction() as conn:
            artifact = conn.execute(select(artifacts).where(artifacts.c.job_id == job["id"])).mappings().one()
            conn.execute(update(artifacts).where(artifacts.c.id == artifact["id"])
                .values(metadata={**artifact["metadata"], "validated": False}))
        self.assertEqual(self.create()["reason"], "member_replacement_failure_limit")
        with self.repo.transaction() as conn:
            conn.execute(update(artifacts).where(artifacts.c.id == artifact["id"]).values(metadata=artifact["metadata"]))
            receipt = conn.execute(select(scaler_receipts).where(scaler_receipts.c.intent_id == a1,
                scaler_receipts.c.operation == "member_local_closed")).mappings().one()
            conn.execute(update(scaler_receipts).where(scaler_receipts.c.id == receipt["id"])
                .values(facts={**receipt["facts"], "local_port": self.config.port_start}))
        self.assertEqual(self.create()["reason"], "member_replacement_local_stop_unconfirmed")
        with self.repo.transaction() as conn:
            conn.execute(update(scaler_receipts).where(scaler_receipts.c.id == receipt["id"]).values(facts=receipt["facts"]))
        new = self.create()
        self.assertEqual(new["state"], "creation_observed")
        self.assertEqual(self.controller.current.port_for(new["intent_id"]), self.config.port_start+4)
        self.assertEqual(len(self.provider.creates), 4)

    def test_replacement_history_survives_portable_backup_without_enabling_recovery(self):
        from studio_platform.backup import backup_local, backup_postgres_engine, restore_local
        from studio_platform.storage import LocalObjectStore
        from studio_platform.repository import Repository, capacity_approvals
        scope, job, a, _ = self.begin(); self.close_member(a); self.now += 61; self.create()
        store = LocalObjectStore(self.root/"objects")
        target = self.root/"replacement-backup"
        if self.repo.engine.dialect.name == "postgresql":
            backup_postgres_engine(self.repo.engine, store.root, target, schema=self.schema)
        else:
            backup_local(Path(self.repo.engine.url.database), store.root, target)
        destination = self.root/"replacement-restored"
        restore_local(target, destination)
        restored = Repository("sqlite:///"+(destination/"platform.sqlite3").as_posix())
        try:
            with self.repo.engine.connect() as source, restored.engine.connect() as dest:
                expected = list(source.execute(select(capacity_member_generations)).mappings())
                self.assertEqual(list(dest.execute(select(capacity_member_generations)).mappings()), expected)
                approval = dest.execute(select(capacity_approvals).where(capacity_approvals.c.id == self.grant()["id"])).mappings().one()
                self.assertEqual(approval["enabled"], 0)
                self.assertEqual(len(bindings(dest, approval)[0]), 3)
            self.assertEqual(restored.get_job(scope, job["id"])["status"], "recovery_hold")
        finally:
            restored.close()

    def test_opt_in_still_retires_clean_idle_pair_after_600_seconds_without_new_demand(self):
        scope, job, a, _ = self.begin(); self.complete_on(a, scope, job); self.tick()
        for _ in range(37): self.tick()
        self.assertEqual(self.provider.destroys, [])
        self.tick(); self.assertEqual(len(self.provider.destroys), 2)
        self.provider.billing = lambda *args: 100_000
        self.tick(); self.tick()
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 0)
