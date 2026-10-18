"""Operator-only transfer of one idle, already rented GPU and one original job.

This module makes no provider, SSH, inference or credential calls. The host must
first freeze the exact old controller and separately reconcile every synthetic
submission. An empty pod list is never evidence here: the same physical pod must
be positively running and idle. Activation is a separate step after the old host
has retired and the new runtime receipt has been prepared.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import sys
import uuid

from sqlalchemy import insert, select, update

from .capacity import ColdStartCoordinator
from .execution_policy import read_policy, reservation_for_duration, validate_policy
from .on_demand_scaler import cycle_config, read_config
from .qualification_profiles import (
    MIN_MULTIMODAL_JOB_RUNTIME_S,
    MULTIMODAL_PROFILE,
    QUEUED_TASK_PROFILE,
)
from .repository import (
    Conflict,
    Repository,
    attempts,
    budget_accounts,
    budget_reservations,
    capacity_approvals,
    capacity_cycles,
    capacity_waiters,
    instance_intents,
    jobs,
    plans,
    pool_limits,
    registered_workers,
    request_hash,
    scaler_actions,
    scaler_leaders,
    scaler_receipts,
)
from .settings import Settings

HASH = re.compile(r"[0-9a-f]{64}\Z")
OPERATION = "operator-live-handoff"
TERMINAL = ("succeeded", "failed", "cancelled")
# Only these operational bindings change. No user generation options change.
EXECUTION_BINDINGS = frozenset(
    (
        "pool",
        "configuration_id",
        "policy_revision",
        "policy_hash",
        "qualification_evidence_id",
        "qualification_expires_at",
        "quote_expires_at",
        "capacity_approval_id",
        "capacity_approval_hash",
    )
)
SAFE_PROOF_FIELDS = (
    "version",
    "phase",
    "operation_id",
    "evidence_sha256",
    "sequence",
    "old_config_hash",
    "new_config_hash",
    "old_pool",
    "new_pool",
    "previous_approval_id",
    "next_approval_id",
    "intent_id",
    "provider_instance_id",
    "previous_approval_hash",
    "next_approval_hash",
    "created_at",
    "physical_deadline",
    "waiter_deadline",
    "fenced_leader_id",
    "old_fence",
    "new_fence",
    "job_id",
    "job_invariant_sha256",
    "plan_sha256",
    "request_sha256",
    "job_request_hash",
    "budget_sha256",
    "reservation_sha256",
    "instance_invariant_sha256",
    "old_action_sha256",
    "new_action_sha256",
    "new_execution_sha256",
    "job_fence",
    "cycle_created_at",
    "source_sha256",
)


def require(value, code):
    if not value:
        raise Conflict(code)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _job_invariant(job):
    return {
        k: v
        for k, v in job.items()
        if k not in ("pool", "execution_plan", "fence", "updated_at")
    }


def _instance_invariant(intent):
    return {k: v for k, v in intent.items() if k not in ("pool", "updated_at")}


def _validate_configs(old, new, sequence):
    require(
        type(sequence) is int and 1 <= sequence <= 8, "live_handoff_sequence_invalid"
    )
    suffix = "-" + str(sequence).zfill(3)
    require(
        old.capacity_approval_id.endswith(suffix)
        and new.capacity_approval_id.endswith(suffix)
        and old.cycle_id.endswith(suffix)
        and new.cycle_id.endswith(suffix),
        "live_handoff_current_sequence_mismatch",
    )
    require(
        old.tenant == new.tenant == "sixnine"
        and old.allowed_owners == new.allowed_owners == ["superdan", "supervan"]
        and old.qualification_profile == MULTIMODAL_PROFILE
        and new.qualification_profile == QUEUED_TASK_PROFILE
        and old.pool != new.pool
        and old.configuration_id != new.configuration_id
        and old.capacity_approval_id != new.capacity_approval_id,
        "live_handoff_explicit_strategy_transition_required",
    )
    exact = (
        "owner",
        "project_id",
        "budget_account_ids",
        "created_at",
        "data_dir",
        "source_dir",
        "source_sha256",
        "ssh_key_file",
        "secret_arn",
        "secret_version_id",
        "drain_margin_s",
        "collection_margin_s",
    )
    require(
        all(getattr(old, k) == getattr(new, k) for k in exact)
        and old.recipe_ids == new.recipe_ids,
        "live_handoff_original_scope_or_source_changed",
    )
    extension = new.hard_deadline - old.hard_deadline
    require(
        extension == 0 or extension == 18000 and new.authorization_extension_s == 18000,
        "live_handoff_window_extension_not_authorized",
    )
    a, b = old.scale_policy, new.scale_policy
    require(
        a["max_instances"] == b["max_instances"] == 1
        and a["max_physical_gpus"] == b["max_physical_gpus"] == 1
        and all(a[k] == b[k] for k in a if k not in ("hard_deadline", "cold_start_s"))
        and 0 <= b["cold_start_s"] <= a["cold_start_s"],
        "live_handoff_budget_or_scale_limits_changed",
    )
    require(
        len(old.launches) == len(new.launches) == 1
        and {k: v for k, v in old.launches[0].items() if k != "configuration_id"}
        == {k: v for k, v in new.launches[0].items() if k != "configuration_id"}
        and len(old.manifests) == len(new.manifests) == 1
        and {
            k: v
            for k, v in old.manifests[0].items()
            if k not in ("configuration_id", "approved_until")
        }
        == {
            k: v
            for k, v in new.manifests[0].items()
            if k not in ("configuration_id", "approved_until")
        },
        "live_handoff_physical_launch_or_ttl_changed",
    )


def _validate_policy(config, value, now):
    validate_policy(value)
    q, quote = value["qualification"], value["reservation"]
    require(
        value["enabled"] is True
        and request_hash(value) == config.execution_policy_sha256
        and value["pool"] == config.pool
        and value["configuration_id"] == config.configuration_id
        and value["recipe_ids"] == list(config.recipe_ids)
        and q["status"] == "runtime_required"
        and q["profile"] == config.qualification_profile
        and q["evidence_id"] == config.qualification_evidence_id
        and q["verified_at"] <= now < q["expires_at"] <= config.hard_deadline
        and now < quote["expires_at"] <= config.hard_deadline,
        "live_handoff_policy_identity_invalid",
    )
    envelope = value["envelope"]
    expected_controls = {
        "sampler_name": ["res_multistep"],
        "scheduler": ["auto"],
        "video_decode": ["tiled"],
        "audio_decode": ["normal"],
        "encoder_device": ["cpu"],
        "ref_image_size": ["max"],
    }
    maximum_duration = (
        362 / 24 if config.qualification_profile == QUEUED_TASK_PROFILE else 6
    )
    require(
        envelope["controls"] == expected_controls
        and envelope["max_steps"] <= 50
        and envelope["max_duration_seconds"] <= maximum_duration
        and envelope["max_reference_files"] <= 3
        and envelope["max_guides"] <= 1
        and quote["expected_runtime_s"] >= MIN_MULTIMODAL_JOB_RUNTIME_S,
        "live_handoff_policy_outside_runtime_scope",
    )


def _validate_workers(conn, old, new, instance_id):
    # Older cycles remain in the same service pool. Their retired registrations
    # are history, not live runtimes; the current pod must have no registration.
    workers = list(
        conn.execute(
            select(registered_workers)
            .where(registered_workers.c.pool.in_((old.pool, new.pool)))
            .with_for_update()
        ).mappings()
    )
    require(
        all(
            w["pool"] == old.pool
            and w["instance_id"] != instance_id
            and w["state"] == "retired"
            and w["current_job_id"] is None
            for w in workers
        ),
        "live_handoff_registered_worker_requires_reconciliation",
    )


def _validate_grant(config, policy, grant, *, enabled):
    p = grant["payload"]
    require(
        grant["id"] == config.capacity_approval_id
        and grant["enabled"] == enabled
        and grant["approval_hash"] == request_hash(p)
        and grant["tenant_id"] == p["tenant_id"] == config.tenant
        and grant["pool"] == p["pool"] == config.pool
        and grant["configuration_id"]
        == p["configuration_id"]
        == config.configuration_id
        and p["model_id"] == policy["model_id"]
        and p["recipe_ids"] == list(config.recipe_ids)
        and p["policy_hash"] == config.execution_policy_sha256
        and p["qualification_evidence_id"] == config.qualification_evidence_id
        and p["qualification_expires_at"] == policy["qualification"]["expires_at"]
        and p["quote_expires_at"] == policy["reservation"]["expires_at"]
        and grant["expires_at"] == p["expires_at"]
        and p["expires_at"] <= min(p["qualification_expires_at"], p["quote_expires_at"])
        and p["budget_scope"] == asdict(config.scope)
        and p["budget_account_ids"] == sorted(config.budget_account_ids)
        and p["launch"] == config.launches[0]
        and p["scale_policy"] == config.scale_policy,
        "live_handoff_grant_identity_invalid",
    )


def _validate_proof(old, new, proof, intent, leader, now):
    require(
        isinstance(proof, dict) and proof.get("version") == 1,
        "live_handoff_proof_invalid",
    )
    try:
        uuid.UUID(proof["operation_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Conflict("live_handoff_operation_id_invalid") from None
    require(
        proof.get("old_config_hash") == old.fingerprint()
        and proof.get("new_config_hash") == new.fingerprint()
        and proof.get("intent_id") == intent["id"]
        and proof.get("provider_instance_id") == intent["provider_instance_id"]
        and proof.get("leader")
        == {"leader_id": leader["leader_id"], "fence": leader["fence"]},
        "live_handoff_frozen_identity_mismatch",
    )
    f, r = proof.get("frozen", {}), proof.get("runtime", {})
    require(
        f.get("config_hash") == old.fingerprint()
        and f.get("running") is True
        and f.get("paused") is True
        and f.get("continuous_paused") is True
        and f.get("children_paused") is True
        and f.get("restart_count") == 0
        and type(f.get("pid")) is int
        and f["pid"] > 0
        and finite(f.get("started_at"))
        and f["started_at"] <= intent["created_at"]
        and finite(f.get("frozen_at"))
        and finite(f.get("observed_at"))
        and f["frozen_at"] <= f["observed_at"] <= now
        and 0 <= now - f["observed_at"] <= 120,
        "live_handoff_exact_controller_not_frozen",
    )
    require(
        r.get("provider") == "lium"
        and r.get("provider_running") is True
        and r.get("instance_id") == intent["provider_instance_id"]
        and r.get("configuration_id") == old.configuration_id
        and r.get("source_sha256") == old.source_sha256
        and r.get("safe_deadline") == intent["hard_deadline"]
        and r.get("queue_running") == 0
        and type(r.get("queue_running")) is int
        and r.get("queue_pending") == 0
        and type(r.get("queue_pending")) is int
        and r.get("known_smokes_terminal") is True
        and r.get("unknown_submission") is False
        and isinstance(r.get("smoke_inventory_sha256"), str)
        and HASH.fullmatch(r["smoke_inventory_sha256"])
        and isinstance(r.get("terminal_smokes_sha256"), str)
        and HASH.fullmatch(r["terminal_smokes_sha256"])
        and finite(r.get("observed_at"))
        and f["frozen_at"] <= r["observed_at"] <= now
        and now - r["observed_at"] <= 120,
        "live_handoff_runtime_not_positively_idle",
    )


def _budget_ids(old, old_policy, proof, conn):
    job = (
        conn.execute(select(jobs).where(jobs.c.id == proof.get("job_id")))
        .mappings()
        .first()
    )
    require(job is not None, "live_handoff_original_job_missing")
    scope = {k: job[k] for k in ("tenant_id", "owner_id", "project_id")}
    return sorted(
        set(old.budget_account_ids)
        | {value.format(**scope) for value in old_policy["budget_accounts"]}
    )


def prepare(repo, old, new, proof, *, old_policy, new_policy, apply=False):
    """Validate by default; atomically change operational bindings on apply.

    The new approval stays disabled and both leader leases stay fenced. The
    caller must persist the returned receipt before retiring the frozen host.
    An interrupted response can recover the identical receipt with lookup().
    """
    require(isinstance(proof, dict), "live_handoff_proof_invalid")
    _validate_configs(old, new, proof.get("sequence"))
    _validate_policy(old, old_policy, repo.clock())
    _validate_policy(new, new_policy, repo.clock())
    require(
        old_policy["model_id"] == new_policy["model_id"]
        and old_policy["budget_accounts"] == new_policy["budget_accounts"]
        and {
            k: v
            for k, v in old_policy["envelope"].items()
            if k != "max_duration_seconds"
        }
        == {
            k: v
            for k, v in new_policy["envelope"].items()
            if k != "max_duration_seconds"
        }
        and old_policy["envelope"]["max_duration_seconds"]
        <= new_policy["envelope"]["max_duration_seconds"],
        "live_handoff_user_input_envelope_changed",
    )
    with repo.transaction() as conn:
        bids = _budget_ids(old, old_policy, proof, conn)
        accounts = []
        for bid in bids:
            row = repo._locked(
                conn, select(budget_accounts).where(budget_accounts.c.id == bid)
            )
            require(
                row is not None and row["tenant_id"] == old.tenant,
                "live_handoff_budget_missing",
            )
            accounts.append(dict(row))
        repo._lock_capacity(conn)
        usage = repo._global_usage(conn)
        require(
            usage["instances"] == usage["physical_gpus"] == 1,
            "live_handoff_other_global_rental_requires_reconciliation",
        )
        leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == old.pool)
        )
        require(leader is not None, "live_handoff_old_leader_missing")
        target_leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == new.pool)
        )
        require(target_leader is None, "live_handoff_new_pool_already_controlled")
        rows = [
            dict(r)
            for r in conn.execute(
                select(instance_intents)
                .where(
                    instance_intents.c.pool.in_((old.pool, new.pool)),
                    instance_intents.c.state != "destroyed",
                )
                .with_for_update()
            ).mappings()
        ]
        require(
            len(rows) == 1
            and rows[0]["id"] == proof.get("intent_id")
            and rows[0]["pool"] == old.pool
            and rows[0]["state"] == "starting"
            and rows[0]["provider"] == "lium"
            and rows[0]["provider_instance_id"] is not None
            and rows[0]["physical_gpus"] == rows[0]["slots"] == 1,
            "live_handoff_instance_requires_reconciliation",
        )
        intent = rows[0]
        require(
            not conn.execute(
                select(scaler_receipts.c.id).where(
                    scaler_receipts.c.intent_id == intent["id"],
                    scaler_receipts.c.operation == OPERATION,
                )
            ).first(),
            "live_handoff_already_applied_use_receipt",
        )
        action = repo._locked(
            conn,
            select(scaler_actions).where(scaler_actions.c.intent_id == intent["id"]),
        )
        require(
            action is not None
            and action["pool"] == old.pool
            and action["launch_spec"] == old.launches[0]
            and action["create_started_at"] is not None
            and action["destroy_started_at"] is None
            and isinstance(action["last_observation"], dict)
            and action["last_observation"].get("state") == "running"
            and action["last_observation"].get("instance_id")
            == intent["provider_instance_id"],
            "live_handoff_allocated_action_not_confirmed",
        )
        _validate_proof(old, new, proof, intent, leader, repo.clock())
        target_pool = (
            conn.execute(select(pool_limits).where(pool_limits.c.pool == new.pool))
            .mappings()
            .first()
        )
        require(
            target_pool
            and target_pool["max_instances"] == target_pool["max_physical_gpus"] == 1,
            "live_handoff_new_pool_not_approved",
        )
        _validate_workers(conn, old, new, intent["provider_instance_id"])
        pending = [
            dict(r)
            for r in conn.execute(
                select(jobs)
                .where(
                    jobs.c.pool.in_((old.pool, new.pool)),
                    jobs.c.status.not_in(TERMINAL),
                )
                .order_by(jobs.c.id)
                .with_for_update()
            ).mappings()
        ]
        require(
            len(pending) == 1 and pending[0]["id"] == proof.get("job_id"),
            "live_handoff_explicit_single_job_required",
        )
        job = pending[0]
        require(
            job["tenant_id"] == old.tenant
            and job["owner_id"] in old.allowed_owners
            and job["pool"] == old.pool
            and job["status"] == "waiting_capacity"
            and job["attempt_no"] == 0
            and job["current_attempt_id"] is None
            and job["lease_worker_id"] is None
            and job["lease_expires_at"] is None
            and not conn.execute(
                select(attempts.c.id).where(attempts.c.job_id == job["id"])
            ).first(),
            "live_handoff_job_submission_requires_reconciliation",
        )
        require(
            ColdStartCoordinator._source_current(conn, job),
            "live_handoff_original_source_changed",
        )
        plan = (
            conn.execute(select(plans).where(plans.c.id == job["plan_id"]))
            .mappings()
            .one()
        )
        require(
            plan["request"] == job["request"]
            and plan["request_hash"] == request_hash(job["request"])
            and job["request_hash"]
            == request_hash(
                {
                    "request_hash": plan["request_hash"],
                    "plan_hash": plan["plan_hash"],
                    "estimated_cost_microusd": plan["estimated_cost_microusd"],
                }
            )
            and plan["execution_plan"] == job["execution_plan"]
            and plan["estimated_cost_microusd"] == job["estimated_cost_microusd"],
            "live_handoff_original_plan_changed",
        )
        grants = {}
        for aid in sorted((old.capacity_approval_id, new.capacity_approval_id)):
            row = repo._locked(
                conn, select(capacity_approvals).where(capacity_approvals.c.id == aid)
            )
            require(row is not None, "live_handoff_grants_missing")
            grants[aid] = dict(row)
        a, b = grants[old.capacity_approval_id], grants[new.capacity_approval_id]
        _validate_grant(old, old_policy, a, enabled=1)
        _validate_grant(new, new_policy, b, enabled=0)
        execution = job["execution_plan"]
        require(
            execution.get("pool") == old.pool
            and execution.get("configuration_id") == old.configuration_id
            and execution.get("policy_hash") == old.execution_policy_sha256
            and execution.get("capacity_approval_id") == a["id"]
            and execution.get("capacity_approval_hash") == a["approval_hash"]
            and execution.get("qualification_evidence_id")
            == old.qualification_evidence_id
            and execution.get("backend") == "comfy-worker"
            and execution.get("enabled") is True
            and execution.get("quote_known") is True
            and execution.get("admission_state") == "waiting_capacity"
            and execution.get("expected_runtime_s") == job["expected_runtime_s"]
            and job["request"]["recipe_id"] in new.recipe_ids
            and job["request"]["request"]["model"] == new_policy["model_id"],
            "live_handoff_original_job_binding_mismatch",
        )
        duration = job["request"]["output_spec"]["actual_duration"]
        require(
            all(
                reservation_for_duration(p, duration)["cost_microusd"]
                == job["estimated_cost_microusd"]
                and reservation_for_duration(p, duration)["expected_runtime_s"]
                == job["expected_runtime_s"]
                for p in (old_policy, new_policy)
            ),
            "live_handoff_original_allowance_would_change",
        )
        link = repo._locked(
            conn,
            select(capacity_cycles).where(capacity_cycles.c.approval_id == a["id"]),
        )
        require(
            link
            and link["intent_id"] == intent["id"]
            and not conn.execute(
                select(capacity_cycles).where(capacity_cycles.c.approval_id == b["id"])
            ).first(),
            "live_handoff_cycle_binding_mismatch",
        )
        waiter = repo._locked(
            conn, select(capacity_waiters).where(capacity_waiters.c.job_id == job["id"])
        )
        require(
            waiter
            and waiter["approval_id"] == a["id"]
            and waiter["approval_hash"] == a["approval_hash"]
            and waiter["intent_id"] == intent["id"]
            and waiter["state"] == "waiting_capacity",
            "live_handoff_waiter_binding_mismatch",
        )
        deadline = min(
            waiter["deadline"],
            b["expires_at"],
            b["payload"]["quote_expires_at"],
            b["payload"]["qualification_expires_at"],
            intent["hard_deadline"] - job["expected_runtime_s"] - 120,
        )
        require(
            repo.clock() < deadline
            and repo.clock() + job["expected_runtime_s"] + 120
            < intent["hard_deadline"],
            "live_handoff_original_physical_deadline_unsafe",
        )
        reservations = [
            dict(r)
            for r in conn.execute(
                select(budget_reservations)
                .where(
                    (
                        (budget_reservations.c.reference_type == "job")
                        & (budget_reservations.c.reference_id == job["id"])
                    )
                    | (
                        (budget_reservations.c.reference_type == "instance")
                        & (budget_reservations.c.reference_id == intent["id"])
                    )
                )
                .order_by(budget_reservations.c.id)
                .with_for_update()
            ).mappings()
        ]
        require(
            reservations
            and all(
                r["state"] == "reserved"
                and r["actual_cost_microusd"] is None
                and r["account_id"] in bids
                for r in reservations
            )
            and sorted(
                r["account_id"] for r in reservations if r["reference_type"] == "job"
            )
            == sorted(execution["budget_account_ids"])
            and sorted(
                r["account_id"]
                for r in reservations
                if r["reference_type"] == "instance"
            )
            == sorted(old.budget_account_ids)
            and all(
                r["amount_microusd"]
                == (
                    job["estimated_cost_microusd"]
                    if r["reference_type"] == "job"
                    else intent["reserved_cost_microusd"]
                )
                for r in reservations
            ),
            "live_handoff_original_reservations_not_held",
        )
        next_execution = {
            **execution,
            "pool": new.pool,
            "configuration_id": new.configuration_id,
            "policy_revision": new_policy["revision"],
            "policy_hash": new.execution_policy_sha256,
            "qualification_evidence_id": new.qualification_evidence_id,
            "qualification_expires_at": new_policy["qualification"]["expires_at"],
            "quote_expires_at": new_policy["reservation"]["expires_at"],
            "capacity_approval_id": b["id"],
            "capacity_approval_hash": b["approval_hash"],
        }
        next_action = {**dict(action), "pool": new.pool, "launch_spec": new.launches[0]}
        fenced_id = "live-handoff-" + proof["operation_id"]
        receipt = dict(
            version=1,
            phase="ledger_transferred" if apply else "dry_run",
            operation_id=proof["operation_id"],
            evidence_sha256=request_hash(proof),
            sequence=proof["sequence"],
            old_config_hash=old.fingerprint(),
            new_config_hash=new.fingerprint(),
            old_pool=old.pool,
            new_pool=new.pool,
            previous_approval_id=a["id"],
            next_approval_id=b["id"],
            intent_id=intent["id"],
            previous_approval_hash=a["approval_hash"],
            next_approval_hash=b["approval_hash"],
            provider_instance_id=intent["provider_instance_id"],
            created_at=old.created_at,
            physical_deadline=intent["hard_deadline"],
            waiter_deadline=deadline,
            fenced_leader_id=fenced_id,
            old_fence=leader["fence"] + 1,
            new_fence=1,
            job_id=job["id"],
            job_invariant_sha256=request_hash(_job_invariant(job)),
            plan_sha256=request_hash(dict(plan)),
            request_sha256=request_hash(job["request"]),
            job_request_hash=job["request_hash"],
            budget_sha256=request_hash(accounts),
            reservation_sha256=request_hash(reservations),
            instance_invariant_sha256=request_hash(_instance_invariant(intent)),
            old_action_sha256=request_hash(dict(action)),
            new_action_sha256=request_hash(next_action),
            new_execution_sha256=request_hash(next_execution),
            job_fence=job["fence"] + 1,
            cycle_created_at=link["created_at"],
            source_sha256=dict(old.source_sha256),
        )
        if apply:
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == old.pool)
                .values(
                    leader_id=fenced_id,
                    fence=receipt["old_fence"],
                    expires_at=intent["hard_deadline"],
                )
            )
            conn.execute(
                insert(scaler_leaders).values(
                    pool=new.pool,
                    leader_id=fenced_id,
                    fence=1,
                    expires_at=intent["hard_deadline"],
                    policy_hash=None,
                    consecutive_breaches=0,
                    sequence=0,
                    last_scale_at=None,
                    last_observed_at=None,
                )
            )
            conn.execute(
                update(capacity_approvals)
                .where(capacity_approvals.c.id == a["id"])
                .values(enabled=0)
            )
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent["id"])
                .values(pool=new.pool)
            )
            conn.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == intent["id"])
                .values(pool=new.pool, launch_spec=new.launches[0])
            )
            conn.execute(
                update(capacity_cycles)
                .where(capacity_cycles.c.approval_id == a["id"])
                .values(approval_id=b["id"])
            )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    pool=new.pool,
                    execution_plan=next_execution,
                    fence=job["fence"] + 1,
                    updated_at=repo.clock(),
                )
            )
            conn.execute(
                update(capacity_waiters)
                .where(capacity_waiters.c.job_id == job["id"])
                .values(
                    approval_id=b["id"],
                    approval_hash=b["approval_hash"],
                    deadline=deadline,
                )
            )
            conn.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=intent["id"],
                    operation=OPERATION,
                    observed_at=repo.clock(),
                    facts=receipt,
                )
            )
            repo._emit(
                conn,
                "job.capacity_runtime_handoff",
                job["id"],
                {
                    "job_id": job["id"],
                    "status": "waiting_capacity",
                    "generation_resubmitted": False,
                    "reason": "operator_queued_task_runtime_transition",
                },
            )
        return receipt


def lookup(repo, operation_id):
    """Recover the safe durable receipt after losing an apply response."""
    with repo.engine.connect() as conn:
        rows = list(
            conn.execute(
                select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.operation == OPERATION,
                    scaler_receipts.c.facts["operation_id"].as_string() == operation_id,
                )
            ).scalars()
        )
    require(len(rows) == 1, "live_handoff_receipt_not_unique")
    return {k: rows[0][k] for k in SAFE_PROOF_FIELDS}


def verify(repo, old, new, receipt, *, release_leader=False):
    """Inspect bindings; activation requires retired host and prepared adoption.

    release_leader atomically enables the existing new approval and releases
    only its leader fence. It never creates a lease or resets the physical TTL.
    Call this before launching the new controller, whose initialization requires
    an enabled grant. Original job and reservation hashes must still match.
    """
    require(
        isinstance(receipt, dict)
        and receipt.get("version") == 1
        and receipt.get("phase") == "ledger_transferred"
        and receipt.get("old_config_hash") == old.fingerprint()
        and receipt.get("new_config_hash") == new.fingerprint(),
        "live_handoff_receipt_identity_mismatch",
    )
    _validate_configs(old, new, receipt.get("sequence"))
    durable = lookup(repo, receipt.get("operation_id"))
    require(
        all(receipt.get(k) == durable[k] for k in SAFE_PROOF_FIELDS),
        "live_handoff_receipt_changed",
    )
    with repo.transaction() as conn:
        ids = [
            r["account_id"]
            for r in conn.execute(
                select(budget_reservations.c.account_id).where(
                    budget_reservations.c.reference_id.in_(
                        (receipt["job_id"], receipt["intent_id"])
                    )
                )
            ).mappings()
        ]
        accounts = []
        for bid in sorted(set(ids)):
            row = repo._locked(
                conn, select(budget_accounts).where(budget_accounts.c.id == bid)
            )
            require(row is not None, "live_handoff_budget_missing")
            accounts.append(dict(row))
        require(
            request_hash(accounts) == receipt["budget_sha256"],
            "live_handoff_budget_changed",
        )
        repo._lock_capacity(conn)
        leaders = {}
        for pool, fence in (
            (old.pool, receipt["old_fence"]),
            (new.pool, receipt["new_fence"]),
        ):
            leader = repo._locked(
                conn, select(scaler_leaders).where(scaler_leaders.c.pool == pool)
            )
            require(
                leader
                and leader["leader_id"] == receipt["fenced_leader_id"]
                and leader["fence"] == fence,
                "live_handoff_leader_changed",
            )
            leaders[pool] = dict(leader)
        job = repo._job(conn, receipt["job_id"], lock=True)
        require(
            job["pool"] == new.pool
            and request_hash(_job_invariant(job)) == receipt["job_invariant_sha256"]
            and request_hash(job["execution_plan"]) == receipt["new_execution_sha256"]
            and job["fence"] == receipt["job_fence"]
            and job["attempt_no"] == 0
            and not conn.execute(
                select(attempts.c.id).where(attempts.c.job_id == job["id"])
            ).first()
            and ColdStartCoordinator._source_current(conn, job),
            "live_handoff_original_job_changed",
        )
        plan = (
            conn.execute(select(plans).where(plans.c.id == job["plan_id"]))
            .mappings()
            .one()
        )
        require(
            request_hash(dict(plan)) == receipt["plan_sha256"],
            "live_handoff_original_plan_changed",
        )
        intent = repo._locked(
            conn,
            select(instance_intents).where(
                instance_intents.c.id == receipt["intent_id"]
            ),
        )
        require(
            intent
            and intent["pool"] == new.pool
            and request_hash(_instance_invariant(dict(intent)))
            == receipt["instance_invariant_sha256"],
            "live_handoff_original_instance_changed",
        )
        require(
            repo.clock() < receipt["waiter_deadline"]
            and repo.clock() + job["expected_runtime_s"] + 120
            < intent["hard_deadline"],
            "live_handoff_original_physical_deadline_unsafe",
        )
        action = (
            conn.execute(
                select(scaler_actions).where(scaler_actions.c.intent_id == intent["id"])
            )
            .mappings()
            .one()
        )
        require(
            request_hash(dict(action)) == receipt["new_action_sha256"],
            "live_handoff_action_changed",
        )
        reservations = [
            dict(r)
            for r in conn.execute(
                select(budget_reservations)
                .where(
                    (
                        (budget_reservations.c.reference_type == "job")
                        & (budget_reservations.c.reference_id == job["id"])
                    )
                    | (
                        (budget_reservations.c.reference_type == "instance")
                        & (budget_reservations.c.reference_id == intent["id"])
                    )
                )
                .order_by(budget_reservations.c.id)
            ).mappings()
        ]
        require(
            request_hash(reservations) == receipt["reservation_sha256"],
            "live_handoff_reservations_changed",
        )
        a = repo._locked(
            conn,
            select(capacity_approvals).where(
                capacity_approvals.c.id == receipt["previous_approval_id"]
            ),
        )
        b = repo._locked(
            conn,
            select(capacity_approvals).where(
                capacity_approvals.c.id == receipt["next_approval_id"]
            ),
        )
        activations = list(
            conn.execute(
                select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.operation == "operator-live-activate",
                    scaler_receipts.c.intent_id == intent["id"],
                    scaler_receipts.c.facts["operation_id"].as_string()
                    == receipt["operation_id"],
                )
            ).scalars()
        )
        already_activated = bool(
            len(activations) == 1
            and a
            and b
            and a["enabled"] == 0
            and b["enabled"] == 1
            and leaders[new.pool]["expires_at"] == 0
            and activations[0].get("new_config_hash") == new.fingerprint()
            and activations[0].get("host_retirement_confirmed") is True
        )
        require(
            a
            and b
            and a["enabled"] == 0
            and (b["enabled"] == 0 and not activations or already_activated),
            "live_handoff_admission_changed",
        )
        require(
            a["approval_hash"]
            == request_hash(a["payload"])
            == receipt["previous_approval_hash"]
            and b["approval_hash"]
            == request_hash(b["payload"])
            == receipt["next_approval_hash"]
            and job["execution_plan"]["capacity_approval_hash"] == b["approval_hash"],
            "live_handoff_grant_changed",
        )
        link = (
            conn.execute(
                select(capacity_cycles).where(capacity_cycles.c.approval_id == b["id"])
            )
            .mappings()
            .first()
        )
        waiter = (
            conn.execute(
                select(capacity_waiters).where(capacity_waiters.c.job_id == job["id"])
            )
            .mappings()
            .one()
        )
        require(
            link
            and link["intent_id"] == intent["id"]
            and link["created_at"] == receipt["cycle_created_at"]
            and waiter["approval_id"] == b["id"]
            and waiter["approval_hash"] == b["approval_hash"]
            and waiter["intent_id"] == intent["id"]
            and waiter["deadline"] == receipt["waiter_deadline"]
            and waiter["state"] == "waiting_capacity",
            "live_handoff_waiter_changed",
        )
        _validate_workers(conn, old, new, intent["provider_instance_id"])
        if release_leader and not already_activated:
            r = receipt.get("runtime_adoption", {})
            require(
                receipt.get("host_retirement_confirmed") is True,
                "live_handoff_old_host_retirement_required",
            )
            require(
                r.get("prepared") is True
                and r.get("config_hash") == new.fingerprint()
                and r.get("intent_id") == intent["id"]
                and r.get("instance_id") == intent["provider_instance_id"]
                and r.get("source_sha256") == new.source_sha256
                and r.get("profile") == QUEUED_TASK_PROFILE
                and r.get("generation_verified") is False
                and r.get("synthetic_receipts_archived") is True
                and r.get("no_bootstrap_restart") is True
                and r.get("safe_deadline") == intent["hard_deadline"]
                and r.get("queue_running") == 0
                and type(r.get("queue_running")) is int
                and r.get("queue_pending") == 0
                and type(r.get("queue_pending")) is int
                and isinstance(r.get("proof_sha256"), str)
                and HASH.fullmatch(r["proof_sha256"])
                and r.get("ledger_handoff_sha256") == request_hash(durable)
                and finite(r.get("observed_at"))
                and 0 <= repo.clock() - r["observed_at"] <= 120,
                "live_handoff_prepared_runtime_adoption_required",
            )
            conn.execute(
                update(capacity_approvals)
                .where(capacity_approvals.c.id == b["id"])
                .values(enabled=1)
            )
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == new.pool)
                .values(expires_at=0)
            )
            conn.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=intent["id"],
                    operation="operator-live-activate",
                    observed_at=repo.clock(),
                    facts={
                        "operation_id": receipt["operation_id"],
                        "new_config_hash": new.fingerprint(),
                        "runtime_adoption_sha256": request_hash(r),
                        "proof_sha256": r["proof_sha256"],
                        "host_retirement_confirmed": True,
                    },
                )
            )
    return {
        "verified": True,
        "activated": release_leader or already_activated,
        "already_activated": already_activated,
        "job_id": receipt["job_id"],
        "intent_id": receipt["intent_id"],
        "generation_resubmitted": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-config", type=Path, required=True)
    parser.add_argument("--new-config", type=Path, required=True)
    parser.add_argument("--old-policy", type=Path)
    parser.add_argument("--new-policy", type=Path)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument(
        "--action", choices=("prepare", "verify", "activate", "lookup"), required=True
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    repo = None
    try:
        old = cycle_config(read_config(args.old_config), args.sequence)
        new = cycle_config(read_config(args.new_config), args.sequence)
        raw = sys.stdin.buffer.read(131073)
        require(len(raw) <= 131072, "live_handoff_stdin_invalid")

        def unique(pairs):
            result = {}
            for key, item in pairs:
                require(key not in result, "live_handoff_duplicate_fields")
                result[key] = item
            return result

        value = json.loads(raw, object_pairs_hook=unique)
        require(
            isinstance(value, dict) and len(json.dumps(value)) <= 131072,
            "live_handoff_stdin_invalid",
        )
        repo = Repository(Settings.from_environment().database_url)
        if args.action == "prepare":
            result = prepare(
                repo,
                old,
                new,
                value,
                old_policy=read_policy(args.old_policy),
                new_policy=read_policy(args.new_policy),
                apply=args.apply,
            )
        elif args.action == "lookup":
            result = lookup(repo, value.get("operation_id"))
        else:
            require(
                args.action != "activate" or args.apply,
                "live_handoff_explicit_apply_required",
            )
            result = verify(
                repo, old, new, value, release_leader=args.action == "activate"
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Conflict as exc:
        print(json.dumps({"verified": False, "safe_error": str(exc)}))
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "verified": False,
                    "safe_error": "live_handoff_configuration_or_evidence_invalid",
                }
            )
        )
        return 1
    finally:
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
