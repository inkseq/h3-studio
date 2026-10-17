"""Explicit two-member lifecycle within the existing scaler and rental ledger.

This module supplies no provider, operating authority or alternate task queue.
The caller owns the same fenced pool leader and injects its current policy guard.
"""
from __future__ import annotations

from dataclasses import asdict
from contextlib import nullcontext
import json
import uuid

from .budget_policy import effective_budget_limit

from sqlalchemy import insert, select, update

from .capacity import pool_member_ids, reserve_capacity_member
from .repository import (Conflict, Scope, capacity_approvals, capacity_pool_members, capacity_member_generations,
    budget_accounts, capacity_waiters, instance_intents, jobs, registered_workers, scaler_actions,
    scaler_receipts, canonical, request_hash, BudgetExceeded)
from .scaler import LaunchSpec
from .pool_member_generations import bindings as member_bindings, replacement_policy, retirement_ledger, one_receipt, successful_generation, no_rent_proven


def port_for_member(config, repo, intent_id):
    """Stable ports belong to approved member IDs, never offer aliases or order."""
    from .production_scaler import ScalerError, save
    from .service_policy import service_member_ids
    members = service_member_ids(config)
    if not members:
        raise ScalerError("capacity_pool_members_required")
    with repo.engine.connect() as connection:
        approval = connection.execute(select(capacity_approvals).where(
            capacity_approvals.c.id == config.capacity_approval_id)).mappings().one()
        bindings, _ = member_bindings(connection, approval)
    if (pool_member_ids(approval["payload"]) != members
            or any(r["approval_hash"] != approval["approval_hash"] or r["member_id"] not in members for r in bindings)):
        raise ScalerError("capacity_pool_member_identity_mismatch")
    expected = {r["intent_id"]: config.port_start+2*r["generation"]+members.index(r["member_id"]) for r in bindings}
    if intent_id not in expected:
        raise ScalerError("capacity_pool_unknown_member")
    path = config.work_dir/"cycle-state.json"
    receipt = json.loads(path.read_text())
    ports = receipt.get("ports")
    if (receipt.get("config_hash") != config.fingerprint() or not isinstance(ports, dict)
            or any(key not in expected or type(value) is not int or value != expected[key]
                   for key, value in ports.items())):
        raise ScalerError("capacity_pool_port_identity_mismatch")
    if intent_id not in ports:
        ports[intent_id] = expected[intent_id]
        save(path, receipt)
    return ports[intent_id]


class MemberLaunchCoordinator:
    """Atomically attach an approved member generation to its only create intent.

    A return after a persisted action is never permission to repeat its POST.
    The existing ScaleCoordinator reconciles that exact intent after uncertainty.
    Replacement requires separately frozen policy and positive predecessor
    retirement evidence; original membership is never rebound.
    """

    def __init__(self, scaler, *, approval_guard, job_guard, budget_ceiling_microusd, retirement_guard=None):
        if type(budget_ceiling_microusd) is not int or budget_ceiling_microusd <= 0:
            raise ValueError("capacity_pool_service_budget_required")
        self.scaler, self.repo = scaler, scaler.repo
        self.approval_guard, self.job_guard = approval_guard, job_guard
        self.budget_ceiling_microusd = budget_ceiling_microusd
        self.retirement_guard = retirement_guard

    def managed(self, connection, approval):
        p = approval["payload"]
        members = pool_member_ids(p)
        if not members:
            raise Conflict("capacity_pool_members_required")
        bindings, current = member_bindings(connection, approval)
        if any(r["approval_hash"] != approval["approval_hash"] or r["member_id"] not in members for r in bindings):
            raise Conflict("capacity_pool_member_identity_mismatch")
        ids = {r["intent_id"] for r in bindings}
        rows = list(connection.execute(select(instance_intents).where(
            instance_intents.c.pool == p["pool"])).mappings())
        if (any(r["state"] != "destroyed" and r["id"] not in ids for r in rows)
                or not ids <= {r["id"] for r in rows}):
            raise Conflict("capacity_pool_unknown_member")
        selected = [self.repo._instance_billing(connection, dict(r)) for r in rows if r["id"] in ids]
        observed = [(r["provider"], r["provider_instance_id"]) for r in selected if r["provider_instance_id"]]
        if len(observed) != len(set(observed)):
            raise Conflict("capacity_pool_duplicate_provider_instance")
        actions = {r["intent_id"]: dict(r) for r in connection.execute(select(scaler_actions).where(
            scaler_actions.c.intent_id.in_(ids))).mappings()}
        if any(r["provider"] != p["launch"]["provider"] or r["id"] not in actions
                or actions[r["id"]]["pool"] != p["pool"]
                or actions[r["id"]]["launch_spec"] != p["launch"] for r in selected):
            raise Conflict("capacity_pool_member_action_mismatch")
        return selected, actions, {key: row["intent_id"] for key, row in current.items()}

    def _demand(self, connection, approval, *, replacement=False):
        p, now = approval["payload"], self.repo.clock()
        if (not approval["enabled"] or min(approval["expires_at"], p["qualification_expires_at"],
                p["quote_expires_at"], p["scale_policy"]["hard_deadline"]) <= now
                or self.approval_guard(p) is not True):
            return False
        rows = connection.execute(select(jobs, capacity_waiters.c.deadline.label("wait_deadline"))
            .join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id).where(
                capacity_waiters.c.approval_id == approval["id"],
                capacity_waiters.c.approval_hash == approval["approval_hash"],
                capacity_waiters.c.intent_id.is_(None), jobs.c.tenant_id == approval["tenant_id"],
                jobs.c.pool == p["pool"],
                # Unknown/cancel/collection still block idle destruction, but
                # do not themselves finance an unused original member.
                jobs.c.status.in_(("waiting_capacity", "queued", "claimed", "submitting", "running")))
            .order_by(jobs.c.id).limit(4097)).mappings()
        values = list(rows)
        if len(values) > 4096:
            raise Conflict("capacity_pool_demand_window_exceeded")
        for job in values:
            execution = job["execution_plan"]
            if (execution.get("capacity_binding") != "pool-members-v1"
                    or execution.get("capacity_approval_id") != approval["id"]
                    or execution.get("capacity_approval_hash") != approval["approval_hash"]
                    or self.job_guard(job) is not True):
                continue
            if job["status"] in ("waiting_capacity", "queued") and job["wait_deadline"] <= now:
                continue
            if replacement and (job["status"] not in ("waiting_capacity", "queued")
                    or job["attempt_no"] != 0 or job["current_attempt_id"] is not None
                    or job["lease_worker_id"] is not None or job["lease_expires_at"] is not None
                    or job["not_before"] > now):
                continue
            return True
        return False

    def before_create(self, lease, approval_id, member_id, intent_id):
        """A bounded read before the sole POST; never renew or reacquire a lease."""
        try:
            with self.repo.transaction() as connection:
                self.scaler._leader(connection, lease)
                approval = connection.execute(select(capacity_approvals).where(
                    capacity_approvals.c.id == approval_id)).mappings().one()
                _, _, mapping = self.managed(connection, approval)
                _, current = member_bindings(connection, approval)
                return (approval["pool"] == lease.pool and mapping.get(member_id) == intent_id
                    and self._demand(connection, approval, replacement=current[member_id]["generation"] > 0))
        except Exception:
            return False

    def create_once(self, lease, approval_id, member_id):
        """Reserve/bind/action/barrier commit precedes exactly one provider call.

        An unknown A still consumes its reservation while separately approved B
        can proceed. New generations require the complete replacement gate.
        Foreign/unbound live intents fail closed before any reservation.
        """
        if self.scaler.enabled is not True or self.scaler.provider.enabled is not True:
            return {"state": "disabled"}
        with self.repo.transaction() as connection:
            self.scaler._leader(connection, lease)
            self.repo._lock_capacity(connection)
            approval = self.repo._locked(connection, select(capacity_approvals).where(
                capacity_approvals.c.id == approval_id))
            if approval is None or approval["pool"] != lease.pool:
                raise Conflict("capacity_pool_approval_mismatch")
            p = approval["payload"]
            if member_id not in pool_member_ids(p):
                raise Conflict("capacity_pool_member_not_approved")
            _, _, mapping = self.managed(connection, approval)
            previous = None
            if member_id in mapping:
                gate = self.replacement_status(connection, approval, member_id)
                if gate["state"] != "replacement_eligible":
                    return gate
                previous = gate
            if not self._demand(connection, approval, replacement=previous is not None):
                return {"state": "no_confirmed_pool_demand"}
            launch = LaunchSpec(**p["launch"])
            if launch.provider != getattr(self.scaler.provider, "provider_id", None):
                raise Conflict("scaler_provider_identity_mismatch")
            validate = getattr(self.scaler.provider, "validate_launch", None)
            if validate is not None:
                validate(launch, physical_gpus=1, slots=1,
                    reserved_cost_microusd=p["scale_policy"]["instance_reservation_microusd"],
                    hard_deadline=min(p["scale_policy"]["hard_deadline"], approval["expires_at"]))
            if previous is None:
                intent = reserve_capacity_member(self.repo, approval_id, member_id, connection=connection)
            else:
                generation = previous["generation"]+1
                intent = self.repo.reserve_instance_intent(Scope(**p["budget_scope"]), p["pool"],
                    "member-"+request_hash({"approval_id": approval_id, "member_id": member_id, "generation": generation}),
                    physical_gpus=1, slots=1, reserved_cost_microusd=p["scale_policy"]["instance_reservation_microusd"],
                    hard_deadline=min(p["scale_policy"]["hard_deadline"], approval["expires_at"]),
                    budget_account_ids=p["budget_account_ids"], dry_run=False, provider=launch.provider, connection=connection)
                if not intent["created"]:
                    raise Conflict("capacity_member_generation_unbound")
                connection.execute(insert(capacity_member_generations).values(approval_id=approval_id, member_id=member_id,
                    generation=generation, approval_hash=approval["approval_hash"], previous_intent_id=previous["intent_id"],
                    intent_id=intent["id"], created_at=self.repo.clock()))
            if not intent["created"]:
                raise Conflict("capacity_pool_member_action_missing")
            # reserve_capacity_member already holds the ordered account locks.
            # Check the post-reservation balance in this same transaction so a
            # service ceiling below the account limit cannot be exceeded by a
            # sibling/racing spender. Failure rolls back every new member row.
            accounts = list(connection.execute(select(budget_accounts).where(
                budget_accounts.c.id.in_(p["budget_account_ids"]))).mappings())
            if len(accounts) != len(p["budget_account_ids"]) or any(
                    a["spent_microusd"]+a["reserved_microusd"] > min(effective_budget_limit(a), self.budget_ceiling_microusd)
                    for a in accounts):
                raise BudgetExceeded("capacity_pool_service_budget_exceeded")
            connection.execute(insert(scaler_actions).values(intent_id=intent["id"], pool=p["pool"],
                launch_spec=canonical(asdict(launch)), create_started_at=self.repo.clock()))
            if self.scaler.preparation_timeout_s is not None:
                self.scaler._preparation_receipt(connection, intent, "awaiting_provider")
            self.repo.update_instance(intent["id"], "creating", connection=connection)
        fact, observed_at = self.scaler._call(intent, "create", launch=launch,
            before_create=lambda: self.before_create(lease, approval_id, member_id, intent["id"]))
        self.scaler._apply(lease, intent["id"], fact, observed_at)
        return {"state": "creation_observed", "intent_id": intent["id"], "provider_state": fact.state}

    def replacement_status(self, connection, approval, member_id):
        """Read-only gate; create_once repeats it under leader/capacity locks."""
        history, current = member_bindings(connection, approval)
        binding = current.get(member_id)
        if binding is None:
            return {"state": "original_member_unbound", "member_id": member_id}
        result = {"state": "member_already_bound", "intent_id": binding["intent_id"]}
        policy = replacement_policy(approval["payload"])
        if policy is None:
            return result
        row = connection.execute(select(instance_intents).where(instance_intents.c.id == binding["intent_id"])).mappings().one()
        result.update(member_id=member_id, generation=binding["generation"], max_replacements=policy["max_replacements"],
                      retry_at=None, automatic_rerent_allowed=False)
        if row["state"] != "destroyed":
            return result
        result["state"] = "replacement_held"
        try:
            if binding["generation"] >= policy["max_replacements"]:
                raise Conflict("member_replacement_limit")
            retirement_ledger(connection, approval, row)
            if self.retirement_guard is None or self.retirement_guard(connection, approval, row, binding) is not True:
                raise Conflict("member_replacement_local_stop_unconfirmed")
            failures = 0
            chain = [r for r in history if r["member_id"] == member_id]
            for prior in reversed(chain):
                if successful_generation(connection, prior["intent_id"]):
                    break
                prior_row = connection.execute(select(instance_intents).where(
                    instance_intents.c.id == prior["intent_id"])).mappings().one()
                phase = self.scaler.preparation(connection, prior_row)
                if one_receipt(connection, prior["intent_id"], "member_quarantine") is not None or (
                        phase is not None and phase.get("phase") == "retiring_unused"):
                    failures += 1
            result.update(consecutive_failures=failures, failure_limit=policy["failure_limit"])
            if failures >= policy["failure_limit"]:
                raise Conflict("member_replacement_failure_limit")
            retry_at = row["updated_at"]+policy["backoff_s"]*2**min(max(failures-1, 0), 4)
            result["retry_at"] = retry_at
            if self.repo.clock() < retry_at:
                raise Conflict("member_replacement_backoff")
            if not self._demand(connection, approval, replacement=True):
                raise Conflict("member_replacement_no_waiting_demand")
            result.update(state="replacement_eligible", automatic_rerent_allowed=True)
        except Conflict as error:
            result["reason"] = str(error)
        return result


# Imported after the pure transaction primitives to keep the controller entry
# point's lazy import free of an on-demand/controller cycle.
from .production_scaler import FiniteController, ScalerError, MODEL, save


class PoolServiceCycle(FiniteController):
    """Two members with immutable opt-in replacement generations.

    Each node retains its own ProductionBoot and original worker/attempts. A
    failed peer is quarantined locally. Global policy expiry/revocation still
    drains the pool. Omitted replacement policy retains original-pair semantics;
    a subsequent pair requires complete prior retirement in either mode.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .service_policy import service_member_ids
        self.member_ids = service_member_ids(self.config)
        if len(self.member_ids) != 2:
            raise ScalerError("capacity_pool_members_required")
        self.members = MemberLaunchCoordinator(self.scaler,
            approval_guard=self.approval_current, job_guard=self.job_allowed,
            budget_ceiling_microusd=self.config.service_policy["budget_ceiling_microusd"],
            retirement_guard=self._local_retirement_confirmed)
        self.scaler.unused_preparation_guard = self._unused_provider_preparation
        self.scaler.unsubmitted_retirement_guard = self._member_retirement_allowed

    def approval_current(self, payload):
        c = self.config
        return bool(not self.stopping() and payload.get("pool_controller") == "continuing-two-members-v1"
            and pool_member_ids(payload) == self.member_ids
            and payload["tenant_id"] == c.tenant and payload["pool"] == c.pool
            and payload["configuration_id"] == c.configuration_id and payload["recipe_ids"] == list(c.recipe_ids)
            and payload["policy_hash"] == c.execution_policy_sha256
            and payload["budget_scope"] == asdict(c.scope)
            and sorted(payload["budget_account_ids"]) == sorted(c.budget_account_ids)
            and payload["scale_policy"] == c.scale_policy and payload["launch"] == c.launches[0]
            and payload.get("member_replacement") == c.service_policy.get("member_replacement")
            and self.policies.capacity_approval_current(payload))

    def _approval(self, connection):
        return connection.execute(select(capacity_approvals).where(
            capacity_approvals.c.id == self.config.capacity_approval_id)).mappings().first()

    def _managed(self):
        with self.repo.engine.connect() as connection:
            grant = self._approval(connection)
            if grant is None:
                rows = self.repo.list_instance_intents(pool=self.config.pool)
                if any(row["state"] != "destroyed" for row in rows):
                    raise ScalerError("capacity_pool_unknown_member")
                return [], {}
            rows, actions, _ = self.members.managed(connection, grant)
            return rows, actions

    def initialize(self):
        from .autoscale import ScalePolicy
        from .execution_policy import read_policy
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        path = c.work_dir/"cycle-state.json"
        if path.exists():
            if json.loads(path.read_text()).get("config_hash") != c.fingerprint():
                raise ScalerError("ondemand_cycle_configuration_changed")
        else:
            self._managed()
            if not c.created_at <= self.repo.clock() < c.stop_claiming_at:
                raise ScalerError("ondemand_service_approval_expired")
            save(path, {"config_hash": c.fingerprint(), "ports": {}, "created_at": self.repo.clock()})
        self.remaining_budget()
        with self.repo.engine.connect() as connection:
            existing = self._approval(connection)
        if existing:
            if existing["enabled"] != 1 or not self.approval_current(existing["payload"]):
                self.request_drain()
            return
        policy = read_policy(self.settings.execution_policy_file)
        engine = ({"backend": c.execution_backend, "engine_manifest_digest": c.engine_manifest_digest}
            if c.execution_backend == "wangp-worker" else {})
        if c.output_delivery:
            engine["output_delivery"] = c.output_delivery
        self.repo.approve_capacity(c.capacity_approval_id, tenant_id=c.tenant, pool=c.pool,
            model_id=MODEL, configuration_id=c.configuration_id, recipe_ids=list(c.recipe_ids),
            policy_hash=c.execution_policy_sha256, qualification_evidence_id=c.qualification_evidence_id,
            qualification_expires_at=policy["qualification"]["expires_at"], quote_expires_at=policy["reservation"]["expires_at"],
            expires_at=min(c.stop_claiming_at, policy["qualification"]["expires_at"], policy["reservation"]["expires_at"]),
            launch=LaunchSpec(**c.launches[0]), scale_policy=ScalePolicy(**c.scale_policy),
            budget_scope=c.scope, budget_account_ids=c.budget_account_ids, enabled=True,
            pool_members={"version": 1, "member_ids": list(self.member_ids)},
            pool_controller="continuing-two-members-v1",
            **({"member_replacement": c.service_policy["member_replacement"]}
               if "member_replacement" in c.service_policy else {}), **engine)

    def port_for(self, intent_id):
        return port_for_member(self.config, self.repo, intent_id)

    def _local_retirement_confirmed(self, connection, approval, intent, binding):
        """Read exact owned closure or positive irreversible never-started proof."""
        c = self.config
        expected = {"version": 1, "intent_id": intent["id"], "instance_id": intent["provider_instance_id"],
            "worker_id": "lium-"+intent["id"].replace("-", ""), "config_hash": c.fingerprint(),
            "sources": c.source_sha256,
            "local_port": c.port_start+2*binding["generation"]+self.member_ids.index(binding["member_id"])}
        proof = one_receipt(connection, intent["id"], "member_local_closed")
        if proof is not None:
            identity = {"intent_id": intent["id"], "instance_id": intent["provider_instance_id"],
                        "configuration_id": c.configuration_id, "sources": c.source_sha256}
            if c.execution_backend == "wangp-worker":
                identity.update(backend="wangp-worker", engine_manifest_digest=c.engine_manifest_digest)
            if c.output_delivery:
                identity["output_delivery"] = c.output_delivery
            if (not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items())
                    or proof.get("local_transport_closed") is not True or proof.get("bootstrap_identity") != identity):
                return False
            if proof.get("kind") == "owned_fleet_exited":
                children = proof.get("children")
                return (isinstance(proof.get("fleet_hash"), str) and len(proof["fleet_hash"]) == 64
                    and isinstance(children, list) and len(children) == 1
                    and children[0].get("worker_id") == expected["worker_id"]
                    and type(children[0].get("pid")) is int and children[0]["pid"] > 0
                    and type(children[0].get("exit_code")) is int)
            if proof.get("kind") == "owned_preparation_never_registered":
                return (proof.get("phase") in ("bootstrap_failed", "staging_failed", "staging_cancelled", "qualification_failed")
                    and connection.execute(select(registered_workers.c.id).where(
                        registered_workers.c.id == expected["worker_id"])).first() is None)
            return False
        # An irreversible provider start barrier proves bootstrap was never
        # allowed. This is not an inference from absent files or a PENDING label.
        preparation = self.scaler.preparation(connection, intent)
        never_created = no_rent_proven(connection, intent)
        unused = preparation is not None and preparation["phase"] == "retiring_unused"
        no_boot = never_created and (preparation is None or preparation["phase"] == "awaiting_provider")
        if (not (unused or no_boot)
                or intent["id"] in self.boots
                or connection.execute(select(registered_workers.c.id).where(
                    registered_workers.c.id == expected["worker_id"])).first() is not None):
            return False
        for path in (c.work_dir/"boot"/intent["id"], c.work_dir/("lifetime-"+intent["id"]+".json")):
            if path.exists() or path.is_symlink():
                return False
        return True

    def member_hold(self, intent, connection=None):
        path = self.config.work_dir/"member-holds"/(intent["id"]+".json")
        with self.repo.engine.connect() if connection is None else nullcontext(connection) as conn:
            recorded = list(conn.execute(select(scaler_receipts.c.facts).where(
                scaler_receipts.c.intent_id == intent["id"], scaler_receipts.c.operation == "member_quarantine")
                .limit(2)).scalars())
        file_value = json.loads(path.read_text()) if path.exists() else None
        if len(recorded) > 1 or recorded and file_value is not None and recorded[0] != file_value:
            raise ScalerError("capacity_pool_member_hold_mismatch")
        value = recorded[0] if recorded else file_value
        if value is not None and (not isinstance(value, dict) or value.get("config_hash") != self.config.fingerprint()
                or value.get("intent_id") != intent["id"] or value.get("instance_id") != intent["provider_instance_id"]
                or value.get("sources") != self.config.source_sha256):
            raise ScalerError("capacity_pool_member_hold_mismatch")
        return value

    def _boot_failure(self, intent, state):
        # This is a quarantine, not proof of stopped inference or empty devices.
        # Existing worker recovery and provider idle proofs still gate deletion.
        directory = self.config.work_dir/"member-holds"
        directory.mkdir(exist_ok=True)
        # Quarantine and claim fence commit together in the existing ledger.
        # The optional private diagnostic copy is never retirement authority.
        value = self._drain_member(intent, reason=state["state"])
        if not (directory/(intent["id"]+".json")).exists():
            save(directory/(intent["id"]+".json"), value)

    def _drain_member(self, intent, *, reason=None):
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            row = self.repo._locked(connection, select(instance_intents).where(instance_intents.c.id == intent["id"]))
            if row is None or row["pool"] != self.config.pool or row["provider_instance_id"] != intent["provider_instance_id"]:
                raise ScalerError("capacity_pool_member_hold_mismatch")
            value = self.member_hold(intent, connection)
            if value is None:
                if reason is None:
                    raise ScalerError("capacity_pool_member_hold_missing")
                value = {"config_hash": self.config.fingerprint(), "intent_id": intent["id"],
                    "instance_id": intent["provider_instance_id"], "sources": self.config.source_sha256,
                    "reason": reason, "observed_at": self.repo.clock()}
            if connection.execute(select(scaler_receipts.c.id).where(scaler_receipts.c.intent_id == intent["id"],
                    scaler_receipts.c.operation == "member_quarantine")).first() is None:
                connection.execute(insert(scaler_receipts).values(id=str(uuid.uuid4()), intent_id=intent["id"],
                    operation="member_quarantine", observed_at=self.repo.clock(), facts=canonical(value)))
            if row["state"] in ("starting", "ready", "busy"):
                self.repo.update_instance(row["id"], "draining", connection=connection)
            connection.execute(update(registered_workers).where(registered_workers.c.provider == row["provider"],
                registered_workers.c.instance_id == row["provider_instance_id"],
                registered_workers.c.state != "retired").values(drain_requested=1, state="draining", updated_at=self.repo.clock()))
        if intent["id"] in self.boots:
            self.boots[intent["id"]].request_drain()
        return value

    def _sync_provider_preparation(self):
        # Replay existing durable holds before activation or boot observation.
        # This also handles a receipt from an interrupted older write ordering.
        rows, _ = self._managed()
        for row in rows:
            if self.member_hold(row) is not None:
                self._drain_member(row)

    def _unused_provider_preparation(self, connection, intent):
        grant = self._approval(connection)
        if grant is None:
            return False
        _, _, mapping = self.members.managed(connection, grant)
        if intent["id"] not in mapping.values() or intent["id"] in self.boots:
            return False
        # The scaler already requires the exact committed awaiting-provider
        # barrier, fresh provider fact and absence of any registered worker.
        for path in (self.config.work_dir/"boot"/intent["id"],
                self.config.work_dir/("lifetime-"+intent["id"]+".json")):
            if path.exists() or path.is_symlink():
                return False
        return True

    def _member_retirement_allowed(self, connection, intent):
        grant = self._approval(connection)
        if grant is None:
            return False
        _, _, mapping = self.members.managed(connection, grant)
        return intent["id"] in mapping.values() and self.member_hold(intent, connection) is not None

    def _bootstrap_start_allowed(self, intent_id, lease):
        if self.stopping():
            return False
        try:
            with self.repo.transaction() as connection:
                self.scaler._leader(connection, lease)
                grant = self._approval(connection)
                rows, _, mapping = self.members.managed(connection, grant)
                row = next(r for r in rows if r["id"] == intent_id)
                return bool(intent_id in mapping.values() and row["state"] in ("starting", "ready", "busy")
                    and row["hard_deadline"]-self.repo.clock() >= self.config.drain_margin_s
                    and self.member_hold(row) is None and self.members._demand(connection, grant))
        except Exception:
            return False

    def _capacity_decision(self, instances, actions, stopping):
        from .autoscale import ScalePolicy
        from .scaler import LeaseLost
        c = self.config
        # The same scaler reconciles every original action and performs proven
        # idle/TTL retirement. Creation is exclusively the member transaction.
        policy = ScalePolicy(**{**c.scale_policy, "max_instances": 0, "max_physical_gpus": 0})
        decision = self.scaler.tick(self.leader_id, c.scope, c.pool, (), (), policy=policy,
            launch=None, budget_account_ids=c.budget_account_ids)
        if stopping:
            return decision
        lease = self.scaler.acquire(c.pool, self.leader_id)
        if lease is None:
            return {"state": "not_leader"}
        for member in self.member_ids:
            try:
                result = self.members.create_once(lease, c.capacity_approval_id, member)
                if result["state"] == "creation_observed":
                    decision = result
                elif result["state"] == "replacement_held":
                    decision = {"state": "blocked", "reason": result["reason"]}
            except BudgetExceeded:
                return {"state": "blocked", "reason": "ledger_capacity_or_budget_limit"}
            except LeaseLost:
                return {"state": "leader_changed_reconcile_required"}
        rows, _ = self._managed()
        repair = self._repair_members(rows)
        if (repair and not any(row["state"] in ("starting", "ready", "busy") and row["id"] not in repair for row in rows)
                and not (decision.get("reason") or "").startswith("member_replacement_")):
            decision = {"state": "blocked", "reason": "queued_task_repair_required"}
        return decision

    def _repair_members(self, rows):
        held = set()
        with self.repo.engine.connect() as connection:
            for row in rows:
                preparation = self.scaler.preparation(connection, row)
                if self.member_hold(row, connection) is not None or preparation and preparation["phase"] == "retiring_unused":
                    held.add(row["id"])
        return held

    def request_rollover(self):
        # Only whole-pair retirement uses this; one member's TTL is local.
        (self.config.work_dir/"rollover.flag").touch()
        self.request_drain()

    def preparation_hold(self):
        return None  # Member holds must never revoke a healthy peer's approval.

    def provider_retirement(self):
        return None  # Legacy pool-wide provider-retry streak does not apply.

    def rotation_allowed(self):
        rows, _ = self._managed()
        repair_rows = rows
        if self.config.service_policy.get("member_replacement") is not None:
            with self.repo.engine.connect() as connection:
                approval = self._approval(connection)
                _, _, current = self.members.managed(connection, approval)
            repair_rows = [r for r in rows if r["id"] in current.values()]
        if not rows or self._repair_members(repair_rows):
            return False
        return all(row["state"] == "destroyed" and row["billing_status"] == "settled" for row in rows)

    def _member_readiness(self):
        from .member_readiness import STATES, project_member
        now = self.repo.clock()
        stopping = self.stopping()
        values = []
        with self.repo.engine.connect() as connection:
            grant = self._approval(connection)
            if grant is None:
                rows, mapping = [], {}
                approval_available = False
            else:
                rows, _, mapping = self.members.managed(connection, grant)
                p = grant["payload"]
                approval_available = bool(grant["enabled"] and min(grant["expires_at"],
                    p["qualification_expires_at"], p["quote_expires_at"],
                    p["scale_policy"]["hard_deadline"]) > now and self.approval_current(p))
            by_id = {row["id"]: row for row in rows}
            # Only the current exact binding is projected. Historical workers
            # stay in the ledger; no fallback to an older generation's slot.
            for member in self.member_ids:
                row = by_id.get(mapping.get(member))
                worker = preparation = job = None
                held, unsafe = False, ()
                if row is not None:
                    worker_id = "lium-"+row["id"].replace("-", "")
                    worker = connection.execute(select(registered_workers).where(
                        registered_workers.c.id == worker_id)).mappings().first()
                    preparation = self.scaler.preparation(connection, row)
                    held = self.member_hold(row, connection) is not None
                    if row["provider_instance_id"]:
                        unsafe = tuple(connection.execute(self._unsafe_attempts(
                            instance_id=row["provider_instance_id"]).distinct().limit(2)).scalars())
                    if worker and worker["current_job_id"]:
                        job = connection.execute(select(jobs.c.id, jobs.c.pool, jobs.c.status).where(
                            jobs.c.id == worker["current_job_id"])).mappings().first()
                values.append(project_member(self.config, member, row, worker,
                    now=now, model_id=MODEL, held=held, preparation=preparation, job=job,
                    unsafe_jobs=unsafe, stopping=stopping))
        return {"observed_at": now, "approval_available": approval_available,
            "members": values, "counts": {state: sum(v["state"] == state for v in values) for state in STATES}}

    def _readiness_reason(self, reason, projection):
        from .member_readiness import PREPARING_REASONS, readiness_reason
        if not projection["approval_available"] and (reason is None or reason in PREPARING_REASONS):
            return "capacity_approval_or_cycle_conflict"
        return readiness_reason(reason, projection["members"])

    def _wait_reason(self, decision, instances, boot_status):
        from .member_readiness import PREPARING_REASONS
        reason = super()._wait_reason(decision, instances, boot_status)
        # A legacy preparing label must not erase a specific budget/repair or
        # authority failure from the current decision. Rental/TTL uncertainty
        # also remains explicit even when a healthy sibling is ready.
        explicit = decision.get("reason")
        if explicit is not None and explicit not in PREPARING_REASONS:
            reason = explicit
        return self._readiness_reason(reason, self._member_readiness())

    def _record_wait_reason(self, reason):
        # This says nothing about an individual job's recipe or TTL fit. The
        # existing admission path alone can activate it and clear its error.
        super()._record_wait_reason("matching_slot_pending" if reason == "gpu_ready" else reason)

    def _project_status(self, value):
        projection = self._member_readiness()
        value["member_readiness"] = projection
        if not self.stopping():
            value["reason"] = self._readiness_reason(value.get("reason"), projection)
        return value

    def status(self, *, fresh_ledger_only=False, **extra):
        rows, _ = self._managed()
        extra["member_holds"] = [{"intent_id": intent_id, "state": "repair_required"}
            for intent_id in sorted(self._repair_members(rows))]
        if self.config.service_policy.get("member_replacement") is not None:
            with self.repo.engine.connect() as connection:
                approval = self._approval(connection)
                extra["member_replacements"] = [self.members.replacement_status(connection, approval, member)
                                                for member in self.member_ids] if approval else []
        return super().status(fresh_ledger_only=fresh_ledger_only, **extra)
