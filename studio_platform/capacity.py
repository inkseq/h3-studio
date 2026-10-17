"""Explicit, revocable cold-start approvals and the durable waiting-capacity loop.

No boot agent, credential loader or cloud daemon is configured here. Only an
explicitly enabled, injected scaler/provider and current operator-policy guard
can create an instance. Provider running is never model qualification.
"""
from dataclasses import asdict, replace
from contextlib import nullcontext
import math
import re

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from .budget_policy import effective_budget_limit
from .autoscale import Demand, ScalePolicy, ScaleState, Slot, recommend
from .repository import (BudgetExceeded, Conflict, NotFound, Scope, budget_accounts, budget_reservations,
    capacity_approvals, capacity_cycles, capacity_gate, capacity_waiters, capacity_pool_members, canonical,
    documents, instance_intents, jobs, pool_limits, registered_workers, request_hash, attempts, scaler_receipts)
from .scaler import LaunchSpec, _safe_id
from .inference.outputs import validate_delivery_policy


CAPACITY_WAIT_CODES = {
    "provider_inventory_unavailable": "capacity_no_matching_gpu",
    "provider_inventory_unconfirmed": "capacity_inventory_check_failed",
    "creation_needs_reconciliation": "capacity_rental_reconciliation",
    "provider_manifest_or_reservation_mismatch": "capacity_configuration_unavailable",
    "ledger_capacity_or_budget_limit": "capacity_budget_or_limit",
    "capacity_approval_or_cycle_conflict": "capacity_configuration_unavailable",
    "gpu_starting": "capacity_gpu_starting",
    "provider_preparing": "capacity_provider_preparing",
    "provider_configuring_ssh": "capacity_provider_configuring_ssh",
    "provider_preparation_timeout": "capacity_provider_preparation_timeout",
    "provider_preparation_failed": "capacity_provider_preparation_failed",
    "provider_ttl_unconfirmed": "capacity_provider_ttl_unconfirmed",
    "provider_preparation_retry_limit": "capacity_provider_preparation_retry_limit",
    "gpu_busy": "capacity_gpu_busy",
    "matching_slot_pending": "capacity_matching_slot_pending",
    "worker_readiness_unconfirmed": "capacity_worker_readiness_unconfirmed",
    "searching": "capacity_searching_gpu",
    "bootstrap_repair_required": "capacity_bootstrap_repair_required",
    "queued_task_repair_required": "capacity_queued_task_repair_required",
    **{code: "capacity_"+code for code in (
        "member_replacement_removal_unconfirmed", "member_replacement_billing_unconfirmed",
        "member_replacement_worker_unretired", "member_replacement_attempt_unresolved",
        "member_replacement_local_stop_unconfirmed", "member_replacement_limit",
        "member_replacement_failure_limit", "member_replacement_backoff", "member_replacement_no_waiting_demand")},
}


def _future(value, now):
    return type(value) in (float, int) and math.isfinite(value) and now < value <= 1e12


def _engine_identity(payload):
    """Missing engine fields denote the immutable historical Comfy approval."""
    backend = payload.get("backend", "comfy-worker")
    digest = payload.get("engine_manifest_digest", "")
    delivery = payload.get("output_delivery", "")
    validate_delivery_policy(backend, delivery)
    if backend == "wangp-worker":
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("capacity_engine_manifest_required")
    elif backend != "comfy-worker" or digest != "":
        raise ValueError("invalid_capacity_engine")
    return backend, digest, delivery


def _matches_engine(payload, binding):
    try:
        backend, digest, delivery = _engine_identity(payload)
        return (binding.get("backend") == backend
            and binding.get("engine_manifest_digest", "") == digest
            and binding.get("output_delivery", "") == delivery)
    except (ValueError, AttributeError, TypeError):
        return False


def pool_member_ids(payload):
    """Absent means the unchanged single-intent contract; no inferred adoption."""
    if "pool_members" not in payload:
        return ()
    value = payload["pool_members"]
    if (not isinstance(value, dict) or set(value) != {"version", "member_ids"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["member_ids"], list) or len(value["member_ids"]) != 2
            or any(not isinstance(v, str) for v in value["member_ids"])
            or len(set(value["member_ids"])) != 2):
        raise ValueError("invalid_capacity_pool_members")
    for member in value["member_ids"]:
        _safe_id(member)
    return tuple(sorted(value["member_ids"]))


def reserve_capacity_member(repo, approval_id, member_id, *, connection=None):
    """Reserve and bind one immutable member in the existing ledger transaction.

    No provider request or action is created here. E2 must integrate these same
    intents with the sole controller; calling the legacy cold tick is forbidden.
    A bound member cannot be replaced or rebound, even after confirmed removal.
    """
    _safe_id(approval_id), _safe_id(member_id)
    with repo.transaction() if connection is None else nullcontext(connection) as conn:
        repo._lock_capacity(conn)
        approval = repo._locked(conn, select(capacity_approvals).where(capacity_approvals.c.id == approval_id))
        if approval is None or member_id not in pool_member_ids(approval["payload"]):
            raise Conflict("capacity_pool_member_not_approved")
        p = approval["payload"]
        _approval_live(repo, conn, approval)
        existing = conn.execute(select(capacity_pool_members).where(
            capacity_pool_members.c.approval_id == approval_id,
            capacity_pool_members.c.member_id == member_id)).mappings().first()
        if existing:
            if existing["approval_hash"] != approval["approval_hash"]:
                raise Conflict("capacity_pool_member_identity_mismatch")
            row = conn.execute(select(instance_intents).where(instance_intents.c.id == existing["intent_id"])).mappings().one()
            return {**row, "created": False, "dry_run": False}
        policy = p["scale_policy"]
        intent = repo.reserve_instance_intent(Scope(**p["budget_scope"]), p["pool"],
            "member-"+request_hash({"approval_id": approval_id, "member_id": member_id}),
            physical_gpus=1, slots=1, reserved_cost_microusd=policy["instance_reservation_microusd"],
            hard_deadline=min(policy["hard_deadline"], approval["expires_at"]),
            budget_account_ids=p["budget_account_ids"], dry_run=False,
            provider=p["launch"]["provider"], connection=conn)
        if not intent["created"]:
            raise Conflict("capacity_pool_member_unbound_intent")
        conn.execute(insert(capacity_pool_members).values(approval_id=approval_id, member_id=member_id,
            approval_hash=approval["approval_hash"], intent_id=intent["id"], created_at=repo.clock()))
        return intent


def member_matches_worker(repo, connection, approval, worker, job):
    """Check a ready member without binding the unsubmitted job to that node."""
    p = approval["payload"]
    member_ids = pool_member_ids(p)
    if not member_ids:
        return True
    execution = job["execution_plan"]
    now = repo.clock()
    if (approval["tenant_id"] != job["tenant_id"] or p["pool"] != job["pool"]
            or approval["id"] != execution.get("capacity_approval_id")
            or approval["approval_hash"] != execution.get("capacity_approval_hash")
            or p["configuration_id"] != worker["spec"]["configuration_id"]
            or p["model_id"] != worker["spec"]["model_id"]
            or job["request"].get("recipe_id") not in p["recipe_ids"]
            or approval["enabled"] != 1 or approval["expires_at"] <= now
            or min(p["quote_expires_at"], p["qualification_expires_at"], p["scale_policy"]["hard_deadline"]) <= now
            or not _matches_engine(p, worker["spec"])):
        return False
    from .pool_member_generations import bindings
    _, current = bindings(connection, approval)
    return connection.execute(select(instance_intents.c.id).where(
            instance_intents.c.id.in_([r["intent_id"] for r in current.values()]), instance_intents.c.pool == p["pool"],
            instance_intents.c.provider == worker["provider"],
            instance_intents.c.provider_instance_id == worker["instance_id"],
            instance_intents.c.state.in_(("starting", "ready", "busy")),
            instance_intents.c.hard_deadline > now+job["expected_runtime_s"]+120)).first() is not None


def capacity_member_claim_allowed(repo, connection, job, worker):
    binding = job["execution_plan"].get("capacity_binding")
    if binding is None:
        return True  # Historical claims do not acquire a new approval requirement.
    if binding != "pool-members-v1":
        return False
    approval_id = job["execution_plan"].get("capacity_approval_id")
    if not approval_id:
        return False
    approval = connection.execute(select(capacity_approvals).where(capacity_approvals.c.id == approval_id)).mappings().first()
    if approval is None:
        return False
    try:
        return bool(pool_member_ids(approval["payload"])) and member_matches_worker(repo, connection, approval, worker, job)
    except (ValueError, KeyError, TypeError, Conflict):
        return False


def pool_members_require_warm_binding(repo, tenant_id, pool, configuration_id):
    """E1 has no warm-plan binding path; never downgrade an opted-in pool.

    Revocation/expiry cannot convert these same slots back to unbound warm
    admission. A later lifecycle implementation must supply the full binding.
    """
    with repo.engine.connect() as connection:
        return connection.execute(select(capacity_approvals.c.id).where(
            capacity_approvals.c.tenant_id == tenant_id, capacity_approvals.c.pool == pool,
            capacity_approvals.c.configuration_id == configuration_id,
            capacity_approvals.c.payload["pool_members"].as_string().is_not(None)).limit(1)).first() is not None


def approve_capacity(repo, approval_id, *, tenant_id, pool, model_id, configuration_id,
        recipe_ids, policy_hash, qualification_evidence_id, qualification_expires_at,
        quote_expires_at, expires_at, launch, scale_policy, budget_scope,
        budget_account_ids, enabled=False, backend="comfy-worker", engine_manifest_digest="", output_delivery="",
        pool_members=None, pool_controller=None, member_replacement=None):
    """Operator-only immutable approval. Revoke separately; never mutate its quote.

    This is not a public API and does not reserve/create an instance. Legacy
    approvals bind one bootstrap intent; each opt-in member retains its original
    immutable binding. Explicit replacement policy admits new immutable
    generations without changing those original rows or existing job identity.
    """
    for value in (approval_id, tenant_id, pool, model_id, configuration_id, qualification_evidence_id):
        _safe_id(value)
    _engine_identity(dict(backend=backend, engine_manifest_digest=engine_manifest_digest, output_delivery=output_delivery))
    members = pool_member_ids({"pool_members": pool_members}) if pool_members is not None else ()
    if pool_controller is not None and (not members or pool_controller != "continuing-two-members-v1"):
        raise ValueError("capacity_pool_controller_invalid")
    if member_replacement is not None:
        from .service_policy import validate_member_replacement
        member_replacement = validate_member_replacement(member_replacement)
        if not members or pool_controller != "continuing-two-members-v1":
            raise ValueError("capacity_member_replacement_identity")
    if members and (not isinstance(scale_policy, ScalePolicy)
            or scale_policy.max_instances != 2 or scale_policy.max_physical_gpus != 2
            or scale_policy.new_instance_slots != 1 or scale_policy.new_instance_physical_gpus != 1):
        raise ValueError("capacity_pool_requires_two_single_gpu_members")
    now = repo.clock()
    if (type(enabled) is not bool or not isinstance(launch, LaunchSpec)
            or not isinstance(scale_policy, ScalePolicy) or not isinstance(budget_scope, Scope)
            or not isinstance(recipe_ids, (tuple, list)) or not 1 <= len(recipe_ids) <= 32
            or len(set(recipe_ids)) != len(recipe_ids)
            or not isinstance(policy_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", policy_hash)
            or budget_scope.tenant_id != tenant_id
            or launch.model_id != model_id or launch.configuration_id != configuration_id
            or any(not _future(value, now) for value in (expires_at, quote_expires_at, qualification_expires_at))
            or expires_at > min(quote_expires_at, qualification_expires_at)
            or scale_policy.dry_run is not False or scale_policy.max_instances < 1
            or scale_policy.max_physical_gpus < scale_policy.new_instance_physical_gpus
            or not _future(scale_policy.hard_deadline, now)
            or not isinstance(budget_account_ids, (tuple, list)) or not 1 <= len(budget_account_ids) <= 8
            or len(set(budget_account_ids)) != len(budget_account_ids)):
        raise ValueError("invalid_capacity_approval")
    from .autoscale import recommend
    recommend([], [], [], now=now, policy=scale_policy)  # pure complete numeric validation
    if (not scale_policy.instance_reservation_microusd
            or scale_policy.approved_remaining_microusd is None
            or scale_policy.instance_reservation_microusd > scale_policy.approved_remaining_microusd):
        raise ValueError("capacity_instance_budget_not_approved")
    if members and 2*scale_policy.instance_reservation_microusd > scale_policy.approved_remaining_microusd:
        raise ValueError("capacity_pool_budget_not_approved")
    for value in (*recipe_ids, *budget_account_ids):
        _safe_id(value)
    payload = canonical(dict(tenant_id=tenant_id, pool=pool, model_id=model_id,
        configuration_id=configuration_id, recipe_ids=sorted(recipe_ids), policy_hash=policy_hash,
        qualification_evidence_id=qualification_evidence_id,
        qualification_expires_at=qualification_expires_at, quote_expires_at=quote_expires_at,
        expires_at=expires_at, launch=asdict(launch), scale_policy=asdict(scale_policy),
        budget_scope=asdict(budget_scope), budget_account_ids=sorted(budget_account_ids),
        **(dict(backend=backend, engine_manifest_digest=engine_manifest_digest)
            if backend == "wangp-worker" else {}),
        **(dict(output_delivery=output_delivery) if output_delivery else {}),
        **({"pool_members": {"version": 1, "member_ids": list(members)}} if members else {}),
        **({"pool_controller": pool_controller} if pool_controller is not None else {}),
        **({"member_replacement": member_replacement} if member_replacement is not None else {})))
    digest = request_hash(payload)
    try:
        with repo.transaction() as connection:
            from .control import require_delivery_configuration
            repo._lock_capacity(connection)
            require_delivery_configuration(connection, backend, configuration_id, output_delivery)
            existing = repo._locked(connection, select(capacity_approvals).where(capacity_approvals.c.id == approval_id))
            if existing:
                if existing["approval_hash"] != digest:
                    raise Conflict("capacity_approval_immutable")
                return dict(existing)  # do not silently re-enable a revoked approval
            row = dict(id=approval_id, tenant_id=tenant_id, pool=pool, configuration_id=configuration_id,
                approval_hash=digest, payload=payload, enabled=int(enabled), expires_at=expires_at, created_at=now)
            connection.execute(insert(capacity_approvals).values(**row))
            return row
    except IntegrityError:
        raise Conflict("capacity_approval_conflict") from None


def _approval_live(repo, connection, row):
    now, p = repo.clock(), row["payload"]
    if (row["enabled"] != 1 or row["expires_at"] <= now or p["quote_expires_at"] <= now
            or p["qualification_expires_at"] <= now or p["scale_policy"]["hard_deadline"] <= now):
        raise Conflict("capacity_approval_unavailable")
    gate = connection.execute(select(capacity_gate).where(capacity_gate.c.id == "global")).mappings().first()
    pool = connection.execute(select(pool_limits).where(pool_limits.c.pool == row["pool"])).mappings().first()
    if gate is None or pool is None or min(gate["max_instances"], gate["max_physical_gpus"],
            pool["max_instances"], pool["max_physical_gpus"]) <= 0:
        raise BudgetExceeded("capacity_start_disabled")
    cycle = connection.execute(select(capacity_cycles).where(capacity_cycles.c.approval_id == row["id"])).mappings().first()
    members = pool_member_ids(p)
    if members:
        if cycle:
            raise Conflict("capacity_pool_has_legacy_cycle")
        from .pool_member_generations import bindings, replacement_policy
        _, current = bindings(connection, row)
        replacement = replacement_policy(p)
        bound = list(connection.execute(select(instance_intents).where(
            instance_intents.c.id.in_([r["intent_id"] for r in current.values()]))).mappings())
        if any(r["state"] not in ("destroyed", "destroying", "draining") and r["hard_deadline"] > now for r in bound):
            return None  # Waiters belong to the approval, never its first member.
        if len(bound) == len(members) and (replacement is None or all(
                r["generation"] >= replacement["max_replacements"] for r in current.values())):
            raise Conflict("capacity_pool_members_unavailable")
        # A never-bound original member is still eligible. Run the ordinary
        # capacity/account checks below; do not replace a failed bound member.
    if cycle:
        intent = connection.execute(select(instance_intents).where(instance_intents.c.id == cycle["intent_id"])).mappings().one()
        if intent["state"] in ("destroyed", "destroying", "draining") or intent["hard_deadline"] <= now:
            raise Conflict("capacity_cycle_unavailable")
        return dict(cycle)
    # Admission makes no promise to create beyond the existing shared gates.
    # Reservations themselves are atomically enforced later by the same ledger.
    policy = p["scale_policy"]
    usage = repo._global_usage(connection)
    active = list(connection.execute(select(instance_intents.c.state, instance_intents.c.physical_gpus).where(
        instance_intents.c.pool == row["pool"], instance_intents.c.state != "destroyed")).mappings())
    if (any(r["state"] in ("reserved", "creating", "creation_unknown") for r in active)
            or usage["instances"]+1 > gate["max_instances"]
            or usage["physical_gpus"]+policy["new_instance_physical_gpus"] > gate["max_physical_gpus"]
            or len(active)+1 > pool["max_instances"]
            or sum(r["physical_gpus"] for r in active)+policy["new_instance_physical_gpus"] > pool["max_physical_gpus"]):
        raise BudgetExceeded("capacity_start_limit")
    billing = Scope(**p["budget_scope"])
    for account_id in p["budget_account_ids"]:
        query = select(budget_accounts).where(budget_accounts.c.id == account_id)
        # A read is sufficient here: reserve_instance_intent locks/checks these
        # accounts before the only create. Do not lock in a different order.
        account = connection.execute(query).mappings().first()
        if (account is None or account["tenant_id"] != billing.tenant_id
                or account["owner_id"] is not None and account["owner_id"] != billing.owner_id
                or account["project_id"] is not None and account["project_id"] != billing.project_id
                or account["spent_microusd"]+account["reserved_microusd"]+policy["instance_reservation_microusd"] > effective_budget_limit(account)):
            raise BudgetExceeded("capacity_instance_budget_unavailable")
    return None


def find_capacity_approval(repo, scope, *, pool, model_id, configuration_id, recipe_id, policy_hash):
    with repo.engine.connect() as connection:
        candidates = list(connection.execute(select(capacity_approvals).where(capacity_approvals.c.tenant_id == scope.tenant_id,
            capacity_approvals.c.pool == pool, capacity_approvals.c.configuration_id == configuration_id,
            capacity_approvals.c.enabled == 1, capacity_approvals.c.expires_at > repo.clock()).limit(101)).mappings())
        if len(candidates) > 100:
            return None  # truncated operator configuration cannot prove uniqueness
        matching = []
        for row in candidates:
            p = row["payload"]
            if p["model_id"] != model_id or recipe_id not in p["recipe_ids"] or p["policy_hash"] != policy_hash:
                continue
            try:
                _approval_live(repo, connection, row)
            except (Conflict, BudgetExceeded):
                continue
            matching.append(dict(row))
        return matching[0] if len(matching) == 1 else None  # never pick the first conflicting approval


def admit_waiter(repo, connection, scope, job, plan):
    execution = plan["execution_plan"]
    approval = repo._locked(connection, select(capacity_approvals).where(
        capacity_approvals.c.id == execution.get("capacity_approval_id")))
    if approval is None:
        raise Conflict("capacity_approval_unavailable")
    p, request = approval["payload"], plan["request"]
    if (approval["tenant_id"] != scope.tenant_id or approval["approval_hash"] != execution.get("capacity_approval_hash")
            or p["policy_hash"] != execution.get("policy_hash") or p["pool"] != job["pool"]
            or p["model_id"] != request.get("request", {}).get("model")
            or request.get("recipe_id") not in p["recipe_ids"]
            or p["configuration_id"] != execution.get("configuration_id")
            or execution.get("capacity_binding") != ("pool-members-v1" if pool_member_ids(p) else None)
            or not _matches_engine(p, execution) or execution.get("enabled") is not True
            or execution.get("quote_known") is not True
            or p["qualification_evidence_id"] != execution.get("qualification_evidence_id")
            or job["attempt_no"] or job.get("current_attempt_id")):
        raise Conflict("capacity_plan_approval_mismatch")
    cycle = _approval_live(repo, connection, approval)
    # The 15-minute plan window governs confirmation of NEW work, not the
    # lifetime of an already confirmed cold-start wait. Large model boot may
    # take longer; its independent approval/quote/qualification/TTL still bound
    # waiting. Repeated HTTP retries retain the original approved job.
    deadline = min(approval["expires_at"], p["quote_expires_at"], p["qualification_expires_at"],
        p["scale_policy"]["hard_deadline"]-job["expected_runtime_s"])
    if deadline <= repo.clock():
        raise Conflict("capacity_wait_deadline_expired")
    connection.execute(insert(capacity_waiters).values(job_id=job["id"], approval_id=approval["id"],
        approval_hash=approval["approval_hash"], deadline=deadline,
        intent_id=cycle["intent_id"] if cycle else None, state="waiting_capacity", created_at=repo.clock()))


def proven_unadmitted_capacity_draft(connection, job):
    """A locked, never-admitted draft is irrelevant to a rental rollover.

    Callers hold the capacity gate and job lock. Labels alone cannot hide a
    previous reservation, lease, waiter or inference; the draft remains untouched.
    """
    return bool(job["status"] in ("planned", "blocked") and job["attempt_no"] == 0
        and job["current_attempt_id"] is None and job["lease_worker_id"] is None
        and job["lease_expires_at"] is None
        and not connection.execute(select(attempts.c.id).where(attempts.c.job_id == job["id"]).limit(1)).first()
        and not connection.execute(select(capacity_waiters.c.job_id).where(
            capacity_waiters.c.job_id == job["id"]).limit(1)).first()
        and not connection.execute(select(budget_reservations.c.id).where(
            budget_reservations.c.reference_type == "job",
            budget_reservations.c.reference_id == job["id"]).limit(1)).first())


def proven_unsubmitted_capacity_job(connection, job):
    """Never infer safe replay from a queued label; retain every old attempt."""
    if job["status"] not in ("queued", "waiting_capacity") or job["lease_worker_id"] is not None:
        return False
    history = list(connection.execute(select(attempts).where(attempts.c.job_id == job["id"])
        .order_by(attempts.c.number).limit(1001)).mappings())
    if (len(history) > 1000 or len(history) != job["attempt_no"]
            or any(row["submission_started_at"] is not None or row["upstream_task_id"] is not None for row in history)):
        return False
    if not history:
        return job["current_attempt_id"] is None
    # A deferred preparation turn never called the inference provider. Keep
    # the attempt ID/count intact and let the normal queue create its next turn.
    return bool(job["status"] == "queued" and history[-1]["id"] == job["current_attempt_id"]
        and all(row["status"] in ("deferred", "cancelled") for row in history))


def transfer_unsubmitted_capacity(repo, previous_id, next_id, *, allowed_owners,
                                  children_done_confirmed=False, limit=4096):
    """Move accepted unsubmitted jobs to a fresh one-use grant without rebilling.

    Operator-only. The old pod must already have an authoritative destroyed
    ledger state and its children must have naturally exited. Shared capacity,
    worker and job locks exclude an old claim/POST racing this handoff.
    """
    if (children_done_confirmed is not True
            or not isinstance(allowed_owners, (list, tuple)) or not 1 <= len(allowed_owners) <= 4096
            or any(not isinstance(owner, str) for owner in allowed_owners)
            or len(set(allowed_owners)) != len(allowed_owners)
            or previous_id == next_id or type(limit) is not int or not 1 <= limit <= 4096):
        raise Conflict("capacity_transfer_requires_confirmed_retirement")
    try:
        for owner in allowed_owners:
            _safe_id(owner)
    except ValueError:
        raise Conflict("capacity_transfer_requires_confirmed_retirement") from None
    with repo.transaction() as conn:
        repo._lock_capacity(conn)
        grants = {r["id"]: dict(r) for r in conn.execute(select(capacity_approvals).where(
            capacity_approvals.c.id.in_((previous_id, next_id)))).mappings()}
        if len(grants) != 2:
            raise Conflict("capacity_transfer_grants_missing")
        old, new = grants[previous_id], grants[next_id]
        a, b = old["payload"], new["payload"]
        member_transfer = bool(pool_member_ids(a) or pool_member_ids(b))
        if member_transfer and (pool_member_ids(a) != pool_member_ids(b)
                or a.get("pool_controller") != "continuing-two-members-v1"
                or b.get("pool_controller") != "continuing-two-members-v1"
                or a.get("member_replacement") != b.get("member_replacement")):
            raise Conflict("capacity_pool_transfer_not_implemented")
        exact = ("tenant_id", "pool", "model_id", "configuration_id", "recipe_ids", "policy_hash",
                 "qualification_evidence_id", "qualification_expires_at", "quote_expires_at",
                 "budget_scope", "budget_account_ids")
        if (old["tenant_id"] != a["tenant_id"] or new["tenant_id"] != b["tenant_id"]
                or _engine_identity(a) != _engine_identity(b)
                or old["enabled"] != 0 or new["enabled"] != 1 or any(a[k] != b[k] for k in exact)
                or new["expires_at"] <= repo.clock()):
            raise Conflict("capacity_transfer_grant_identity_mismatch")
        rows = list(conn.execute(select(instance_intents).where(instance_intents.c.pool == a["pool"])).mappings())
        if member_transfer:
            from .pool_member_generations import bindings as generation_bindings
            old_bindings, _ = generation_bindings(conn, old)
            if (not old_bindings or any(r["approval_hash"] != old["approval_hash"]
                    or r["member_id"] not in pool_member_ids(a) for r in old_bindings)
                    or any(repo._instance_billing(conn, row)["billing_status"] != "settled" for row in rows)):
                raise Conflict("capacity_pool_transfer_obligations_unsettled")
            old_ids = {r["intent_id"] for r in old_bindings}
        else:
            cycle = conn.execute(select(capacity_cycles).where(capacity_cycles.c.approval_id == previous_id)).mappings().one()
            old_ids = {cycle["intent_id"]}
        if (not old_ids <= {row["id"] for row in rows if row["state"] == "destroyed"}
                or any(row["state"] != "destroyed" for row in rows)):
            raise Conflict("capacity_transfer_old_instance_not_removed")
        workers = list(conn.execute(select(registered_workers).where(registered_workers.c.pool == a["pool"])
            .order_by(registered_workers.c.id).with_for_update()).mappings())
        if any(w["state"] != "retired" or w["current_job_id"] for w in workers):
            raise Conflict("capacity_transfer_worker_not_retired")
        candidates = list(conn.execute(select(jobs.c.id).where(jobs.c.tenant_id == a["tenant_id"],
            jobs.c.owner_id.in_(allowed_owners), jobs.c.pool == a["pool"],
            jobs.c.execution_plan["configuration_id"].as_string() == a["configuration_id"],
            jobs.c.status.not_in(("succeeded", "failed", "cancelled")))
            .order_by(jobs.c.id).limit(limit+1)).scalars())
        if len(candidates) > limit:
            raise Conflict("capacity_transfer_window_exceeded")
        moved = []
        for jid in candidates:
            job = repo._job(conn, jid, lock=True)
            if job["status"] in ("planned", "blocked"):
                if not proven_unadmitted_capacity_draft(conn, job):
                    raise Conflict("capacity_transfer_job_requires_reconciliation")
                continue  # Never accept/modify a draft during GPU rollover.
            execution = job["execution_plan"]
            existing = conn.execute(select(capacity_waiters).where(capacity_waiters.c.job_id == jid)).mappings().first()
            if execution.get("capacity_approval_id") == next_id and existing and existing["approval_id"] == next_id:
                if member_transfer and (execution.get("capacity_binding") != "pool-members-v1"
                        or execution.get("capacity_approval_hash") != new["approval_hash"]
                        or existing["approval_hash"] != new["approval_hash"] or existing["intent_id"] is not None):
                    raise Conflict("capacity_pool_transfer_binding_mismatch")
                continue  # Idempotent recovery after the transaction committed.
            if member_transfer and (execution.get("capacity_binding") != "pool-members-v1"
                    or execution.get("capacity_approval_id") != previous_id
                    or execution.get("capacity_approval_hash") != old["approval_hash"]
                    or existing is None or existing["approval_id"] != previous_id
                    or existing["approval_hash"] != old["approval_hash"] or existing["intent_id"] is not None):
                raise Conflict("capacity_pool_transfer_binding_mismatch")
            if (not proven_unsubmitted_capacity_job(conn, job) or execution.get("policy_hash") != a["policy_hash"]
                    or execution.get("capacity_approval_id") not in (None, previous_id)
                    or execution.get("capacity_approval_id") == previous_id
                        and execution.get("capacity_approval_hash") != old["approval_hash"]
                    or existing and existing["approval_id"] != previous_id
                    or existing and existing["approval_hash"] != old["approval_hash"]
                    or not _matches_engine(a, execution) or execution.get("enabled") is not True
                    or execution.get("qualification_evidence_id") != a["qualification_evidence_id"]
                    or job["request"].get("recipe_id") not in b["recipe_ids"]
                    or job["request"].get("request", {}).get("model") != b["model_id"]):
                raise Conflict("capacity_transfer_job_requires_reconciliation")
            deadline = min(existing["deadline"] if existing else float("inf"),
                new["expires_at"], b["quote_expires_at"], b["qualification_expires_at"],
                b["scale_policy"]["hard_deadline"]-job["expected_runtime_s"])
            if deadline <= repo.clock():
                raise Conflict("capacity_transfer_deadline_expired")
            execution = {**execution, "capacity_approval_id": next_id,
                "capacity_approval_hash": new["approval_hash"], "admission_state": "waiting_capacity"}
            state = "queued" if job["attempt_no"] else "waiting_capacity"
            conn.execute(update(jobs).where(jobs.c.id == jid).values(execution_plan=execution,
                status=state, fence=job["fence"]+1, not_before=repo.clock(), updated_at=repo.clock()))
            waiter = dict(approval_id=next_id, approval_hash=new["approval_hash"], deadline=deadline,
                intent_id=None, state="waiting_capacity", created_at=repo.clock())
            if existing:
                conn.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == jid).values(**waiter))
            else:
                conn.execute(insert(capacity_waiters).values(job_id=jid, **waiter))
            repo._emit(conn, "job.capacity_rollover", jid, {"job_id": jid, "status": state,
                "reason": "instance_lifetime_rollover", "generation_resubmitted": False})
            moved.append(jid)
        return moved


class ColdStartCoordinator:
    """Bounded trusted turn; default disabled and no current-policy guard means deny.

    A grant has exactly one bootstrap intent for its lifetime. Failed/unknown/TTL
    cycles cannot be recycled by a second leader or another waiting user.
    """
    def __init__(self, repository, *, scaler=None, enabled=False, approval_guard=None, activation_guard=None):
        if type(enabled) is not bool:
            raise ValueError("invalid_capacity_controller_switch")
        # No implicit adapter construction. The operational activation-only
        # CLI has no scaler at all; paid coordination needs explicit injection.
        self.repo, self.scaler = repository, scaler
        self.enabled = enabled
        self.approval_guard = approval_guard or (lambda payload: False)
        self.activation_guard = activation_guard or (lambda job: False)

    def _valid(self, approval):
        try:
            pool_member_ids(approval["payload"])
            return bool(approval["enabled"] == 1 and approval["expires_at"] > self.repo.clock()
                and approval["payload"]["quote_expires_at"] > self.repo.clock()
                and approval["payload"]["qualification_expires_at"] > self.repo.clock()
                and self.approval_guard(approval["payload"]) is True)
        except Exception:
            return False

    def _approval(self, approval_id):
        _safe_id(approval_id)
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(capacity_approvals).where(capacity_approvals.c.id == approval_id)).mappings().first()
            if row is None:
                raise NotFound("capacity_approval_not_found")
            return dict(row)

    def preview(self, approval_id):
        """Read-only aggregate proposal. No DDL, observation, provider or mutation."""
        approval = self._approval(approval_id)
        p = approval["payload"]
        with self.repo.engine.connect() as connection:
            approved = self._valid(approval)
            try:
                _approval_live(self.repo, connection, approval)
            except (Conflict, BudgetExceeded):
                approved = False
            rows = connection.execute(select(jobs.c.id, jobs.c.owner_id, jobs.c.created_at, jobs.c.expected_runtime_s)
                .join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id).where(
                    capacity_waiters.c.approval_id == approval_id, capacity_waiters.c.state == "waiting_capacity",
                    capacity_waiters.c.deadline > self.repo.clock(), jobs.c.status.in_(("waiting_capacity", "queued")))
                .order_by(jobs.c.created_at, jobs.c.id).limit(4096)).mappings()
            demands = [Demand(r["id"], r["owner_id"], r["created_at"], r["expected_runtime_s"], "operator-qualified") for r in rows]
        from .control import WorkerControl
        capacity = WorkerControl(self.repo).pool_status(p["pool"], model_id=p["model_id"],
            configuration_id=p["configuration_id"])
        # Busy slots have unknown remaining runtime here, so conservatively
        # omit them from the pure prediction rather than invent an ETA.
        slots = [Slot("ready-"+str(i)) for i in range(capacity["ready"])]
        decision = recommend(demands, slots, self.repo.list_instance_intents(pool=p["pool"]), now=self.repo.clock(),
            policy=replace(ScalePolicy(**p["scale_policy"]), dry_run=True), state=ScaleState())
        return {"state": "dry_run", "approval_current": approved, "waiting_count": len(demands),
            "healthy_ready_slots": capacity["ready"], "healthy_busy_slots": capacity["busy"],
            "recommendation": decision.action, "reason": decision.reason,
            "cloud_creation_enabled": False, "provider_calls_enabled": False}

    def advance_once(self, approval_id):
        """Local activation/rejection only; no scaler/provider access whatsoever."""
        if not self.enabled:
            return {"state": "disabled"}
        activated, failed = self._advance(self._approval(approval_id))
        return {"state": "advanced", "activated": len(activated), "failed": len(failed),
            "cloud_creation_enabled": False, "provider_calls_enabled": False}

    def _before_create(self, approval_id):
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(capacity_approvals).where(capacity_approvals.c.id == approval_id)).mappings().one()
            demand = connection.execute(select(jobs).join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id)
                .where(capacity_waiters.c.approval_id == approval_id, capacity_waiters.c.state == "waiting_capacity",
                    capacity_waiters.c.deadline > self.repo.clock(), jobs.c.status.in_(("waiting_capacity", "queued")))
                .limit(1)).mappings().first()
            return bool(demand and proven_unsubmitted_capacity_job(connection, demand) and self._valid(row))

    def _attach(self, connection, intent, approval_id):
        approval = self.repo._locked(connection, select(capacity_approvals).where(capacity_approvals.c.id == approval_id))
        if not self._valid(approval):
            raise Conflict("capacity_approval_unavailable")
        if pool_member_ids(approval["payload"]):
            raise Conflict("capacity_pool_controller_not_implemented")
        if connection.execute(select(capacity_cycles).where(capacity_cycles.c.approval_id == approval_id)).first():
            raise Conflict("capacity_cycle_already_started")
        connection.execute(insert(capacity_cycles).values(approval_id=approval_id, intent_id=intent["id"], created_at=self.repo.clock()))
        connection.execute(update(capacity_waiters).where(capacity_waiters.c.approval_id == approval_id,
            capacity_waiters.c.state == "waiting_capacity").values(intent_id=intent["id"]))

    @staticmethod
    def _source_current(connection, job):
        # Public HTTP plans always carry a server-owned source hash. The small
        # trusted ledger fixtures/clients that omit it have no mutable project
        # source to compare. This does not trust a browser-supplied version.
        expected = job["request"].get("server_source_hash")
        if expected is None:
            return True
        from .source_snapshot import source_snapshot, validate_source_ref
        try:
            ref = job["request"]["client_ref"]
            project = connection.execute(select(documents.c.payload).where(
                documents.c.tenant_id == job["tenant_id"], documents.c.owner_id == job["owner_id"],
                documents.c.project_id == "__projects", documents.c.kind == "project",
                documents.c.document_id == job["project_id"])).scalar_one_or_none()
            return bool(project is not None and ref["project_id"] == job["project_id"]
                and validate_source_ref(project, ref) and source_snapshot(project, ref["shot_id"]) == expected)
        except (KeyError, ValueError, TypeError):
            return False

    def _advance(self, approval, *, limit=100):
        """CAS each owned job; no budget reservation during activation.

        Job-before-waiter lock order matches cancellation. A cancelled/held job
        cannot reactivate even if a healthy worker registers later.
        """
        with self.repo.engine.connect() as connection:
            ids = list(connection.execute(select(capacity_waiters.c.job_id).where(
                capacity_waiters.c.approval_id == approval["id"], capacity_waiters.c.state == "waiting_capacity")
                .order_by(capacity_waiters.c.created_at, capacity_waiters.c.job_id).limit(limit)).scalars())
        activated, failed = [], []
        for jid in ids:
            with self.repo.transaction() as connection:
                job = self.repo._job(connection, jid, lock=True)
                waiter = connection.execute(select(capacity_waiters).where(capacity_waiters.c.job_id == jid)).mappings().one()
                if (job["status"] not in ("waiting_capacity", "queued") or waiter["state"] != "waiting_capacity"
                        or waiter["approval_id"] != approval["id"]):
                    continue
                grant = connection.execute(select(capacity_approvals).where(capacity_approvals.c.id == approval["id"])).mappings().one()
                members = pool_member_ids(grant["payload"])
                reason = None
                if not proven_unsubmitted_capacity_job(connection, job):
                    connection.execute(update(jobs).where(jobs.c.id == jid).values(status="recovery_hold",
                        error_code="capacity_waiter_has_attempt_requires_review", updated_at=self.repo.clock()))
                    connection.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == jid).values(state="recovery_hold"))
                    continue
                if not self._valid(grant):
                    reason = "capacity_approval_expired_or_revoked"
                elif not _matches_engine(grant["payload"], job["execution_plan"]):
                    reason = "capacity_plan_approval_mismatch"
                elif job["execution_plan"].get("capacity_binding") != ("pool-members-v1" if members else None):
                    reason = "capacity_plan_approval_mismatch"
                elif waiter["deadline"] <= self.repo.clock():
                    reason = "capacity_wait_deadline_expired"
                elif self.activation_guard(job) is not True:
                    reason = "execution_policy_unavailable_before_activation"
                elif not self._source_current(connection, job):
                    reason = "capacity_source_changed_before_activation"
                elif members and waiter["intent_id"] is not None:
                    reason = "capacity_pool_waiter_identity_mismatch"
                elif members and waiter["approval_hash"] != grant["approval_hash"]:
                    reason = "capacity_pool_waiter_identity_mismatch"
                intent = None
                if waiter["intent_id"]:
                    intent = connection.execute(select(instance_intents).where(instance_intents.c.id == waiter["intent_id"])).mappings().one()
                    if (reason is None and intent["state"] == "destroyed" and intent["provider_instance_id"] is None
                            and connection.execute(select(scaler_receipts.c.id).where(
                                scaler_receipts.c.intent_id == intent["id"],
                                scaler_receipts.c.operation == "create",
                                scaler_receipts.c.facts["state"].as_string() == "not_created",
                                scaler_receipts.c.facts["absence_confirmed"].as_boolean() == True,
                                scaler_receipts.c.facts["actual_cost_microusd"].as_integer() == 0)).first()):
                        # A failed pre-POST check is not failed user work. The
                        # on-demand controller transfers this same waiter into
                        # the next approved cycle; expiry/cancel still applies.
                        continue
                    if intent["state"] in ("destroyed", "draining", "destroying"):
                        reason = reason or "capacity_cycle_unavailable"
                    elif intent["hard_deadline"] <= self.repo.clock()+job["expected_runtime_s"]:
                        reason = reason or "capacity_instance_deadline_unsafe"
                if reason:
                    self.repo._settle(connection, "job", jid, 0)
                    connection.execute(update(jobs).where(jobs.c.id == jid).values(status="failed", error_code=reason,
                        fence=job["fence"]+1, updated_at=self.repo.clock()))
                    connection.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == jid).values(state="failed"))
                    self.repo._emit(connection, "job.failed", jid, {"job_id": jid, "status": "failed", "error_code": reason})
                    failed.append(jid)
                    continue
                # No cycle yet can accept a separately provisioned, qualified
                # exact slot. A linked cycle requires that precise instance.
                candidates = connection.execute(select(registered_workers).where(registered_workers.c.pool == job["pool"],
                    registered_workers.c.state == "ready", registered_workers.c.current_job_id.is_(None),
                    registered_workers.c.drain_requested == 0, registered_workers.c.expires_at > self.repo.clock())
                    .order_by(registered_workers.c.id).limit(128)).mappings()
                p = grant["payload"]
                match = next((w for w in candidates if _matches_engine(p, w["spec"])
                    and w["spec"]["model_id"] == p["model_id"] and w["spec"]["configuration_id"] == p["configuration_id"]
                    and job["request"]["recipe_id"] in w["spec"]["recipe_ids"]
                    and (not members or member_matches_worker(self.repo, connection, grant, w, job))
                    and (intent is None or w["provider"] == intent["provider"]
                         and w["instance_id"] == intent["provider_instance_id"])), None)
                if match is None:
                    continue
                # Activation locks approval before waiter, matching the
                # scaler's budget->approval->waiter order. Failure settles
                # budget before waiter above without holding an approval lock.
                grant = self.repo._locked(connection, select(capacity_approvals).where(capacity_approvals.c.id == approval["id"]))
                if not self._valid(grant):
                    continue  # the next bounded turn records zero-cost failure
                waiter = self.repo._locked(connection, select(capacity_waiters).where(capacity_waiters.c.job_id == jid))
                if waiter["state"] != "waiting_capacity":
                    continue
                connection.execute(update(jobs).where(jobs.c.id == jid).values(status="queued", error_code=None,
                    not_before=self.repo.clock(), updated_at=self.repo.clock()))
                connection.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == jid).values(state="activated"))
                self.repo._emit(connection, "job.queued", jid, {"job_id": jid, "status": "queued"})
                activated.append(jid)
        return activated, failed

    def record_wait_reason(self, approval_id, reason):
        """Publish bounded, non-secret capacity progress on the existing job DTO.

        Scope to this immutable approval and its tenant; never replace a job
        outcome or unrelated error. This is a waiting reason, not a failed job.
        """
        code = CAPACITY_WAIT_CODES.get(reason)
        if code is None:
            return
        approval = self._approval(approval_id)
        with self.repo.transaction() as connection:
            pending = select(capacity_waiters.c.job_id).where(
                capacity_waiters.c.approval_id == approval_id,
                capacity_waiters.c.state == "waiting_capacity")
            connection.execute(update(jobs).where(jobs.c.id.in_(pending),
                jobs.c.tenant_id == approval["tenant_id"], jobs.c.status == "waiting_capacity",
                or_(jobs.c.error_code.is_(None), jobs.c.error_code.in_(tuple(CAPACITY_WAIT_CODES.values()))),
                or_(jobs.c.error_code.is_(None), jobs.c.error_code != code))
                .values(error_code=code, updated_at=self.repo.clock()))

    def tick(self, leader_id, approval_id):
        if not self.enabled:
            return {"state": "disabled"}
        if self.scaler is None:
            return {"state": "cloud_controller_not_configured"}
        approval = self._approval(approval_id)
        if pool_member_ids(approval["payload"]):
            return {"state": "capacity_pool_controller_not_implemented"}
        activated, failed = self._advance(approval)
        with self.repo.engine.connect() as connection:
            cycle = connection.execute(select(capacity_cycles).where(capacity_cycles.c.approval_id == approval_id)).mappings().first()
            rows = connection.execute(select(jobs.c.id, jobs.c.owner_id, jobs.c.created_at, jobs.c.expected_runtime_s)
                .join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id).where(
                    capacity_waiters.c.approval_id == approval_id, capacity_waiters.c.state == "waiting_capacity",
                    capacity_waiters.c.deadline > self.repo.clock(),
                    jobs.c.status.in_(("waiting_capacity", "queued"))).order_by(jobs.c.created_at, jobs.c.id).limit(4096)).mappings()
            demands = [Demand(r["id"], r["owner_id"], r["created_at"], r["expected_runtime_s"], "operator-qualified") for r in rows]
        p = approval["payload"]
        policy = ScalePolicy(**p["scale_policy"])
        # Existing cycle is permanently single-use. Keep reconciliation and
        # drain active, but prevent a second proposal even after it is destroyed.
        if cycle or not self._valid(approval):
            policy = replace(policy, max_instances=0, max_physical_gpus=0)
        if not demands and cycle is None:
            return {"state": "no_waiters", "activated": len(activated), "failed": len(failed)}
        result = self.scaler.tick(leader_id, Scope(**p["budget_scope"]), p["pool"], demands, (),
            policy=policy, launch=LaunchSpec(**p["launch"]), budget_account_ids=p["budget_account_ids"],
            on_intent_reserved=lambda conn, intent: self._attach(conn, intent, approval_id),
            before_create=lambda: self._before_create(approval_id))
        newly_activated, newly_failed = self._advance(approval)
        return {**result, "activated": len(activated)+len(newly_activated), "failed": len(failed)+len(newly_failed)}
