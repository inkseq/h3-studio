"""Operator-only recovery of held, never-executed bootstrap_failed failures.

No provider calls, task resubmission, budget adjustment or automatic restart.
The protected host freezes the exact old controller before fencing its leader,
then proves retirement and stages the next cycle before releasing that fence.
staging_failed/staging_cancelled and any attempted inference remain unsupported;
they require their own evidence-based recovery and are never treated as idle.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import uuid

from sqlalchemy import insert, select, update

from .backlog_handoff import finite, require
from .capacity import _matches_engine
from .on_demand_scaler import cycle_config, json_config, read_config
from .repository import (
    Conflict,
    Repository,
    artifacts,
    attempts,
    budget_accounts,
    budget_reservations,
    capacity_approvals,
    capacity_cycles,
    capacity_waiters,
    instance_intents,
    jobs,
    plans,
    registered_workers,
    request_hash,
    scaler_actions,
    scaler_leaders,
    scaler_receipts,
)
from .service_policy import cycle_sequence_allowed
from .settings import Settings

OPERATION = "preparation-hold-repair"


def validate_delta(previous, target):
    a, b = json_config(previous), json_config(target)
    require(
        previous.service_mode == target.service_mode == "on-demand",
        "repair_on_demand_required",
    )
    require(
        set(a) == set(b) and all(a[k] == b[k] for k in a if k != "source_sha256"),
        "repair_accepted_contract_changed",
    )
    allowed = (
        {"wangp-bootstrap.py", "wangp-package.tar.gz", "wangp-runtime.json"}
        if previous.execution_backend == "wangp-worker"
        else {"bootstrap_cloud.py"}
    )
    old, new = a["source_sha256"], b["source_sha256"]
    changed = sorted(k for k in old if old[k] != new.get(k))
    require(
        set(old) == set(new)
        and changed
        and set(changed) <= allowed
        and all(
            isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
            for v in new.values()
        ),
        "repair_source_delta_invalid",
    )
    return changed


def validate_source_binding(previous, target, proof):
    changed = validate_delta(previous, target)
    if "wangp-package.tar.gz" in changed or "wangp-runtime.json" in changed:
        require(
            {"wangp-package.tar.gz", "wangp-runtime.json"} <= set(changed),
            "repair_package_binding_required",
        )
        docs = proof.get("runtime_source_binding", {})
        a, b = docs.get("previous"), docs.get("target")
        require(
            isinstance(a, dict)
            and isinstance(b, dict)
            and a.get("source_bundle_sha256")
            == previous.source_sha256["wangp-package.tar.gz"]
            and b.get("source_bundle_sha256")
            == target.source_sha256["wangp-package.tar.gz"]
            and {k: v for k, v in a.items() if k != "source_bundle_sha256"}
            == {k: v for k, v in b.items() if k != "source_bundle_sha256"}
            and docs.get("previous_file_sha256")
            == previous.source_sha256["wangp-runtime.json"]
            and docs.get("target_file_sha256")
            == target.source_sha256["wangp-runtime.json"],
            "repair_runtime_semantics_changed",
        )
    return changed


def _json(path):
    require(
        not path.is_symlink() and path.is_file() and path.stat().st_size <= 131072,
        "repair_runtime_evidence_missing",
    )
    return json.loads(path.read_text())


def _evidence(previous, sequence, intent, staged_target=None):
    cycle = cycle_config(previous, sequence)
    state = _json(previous.work_dir / "service-state.json")
    original_state = {
        "version": 1,
        "config_hash": previous.fingerprint(),
        "sequence": sequence,
        "created_at": previous.created_at,
        "transfer_from": None,
    }
    expected_state = (
        original_state
        if staged_target is None
        else {
            "version": 1,
            "config_hash": staged_target.fingerprint(),
            "sequence": sequence + 1,
            "created_at": previous.created_at,
            "transfer_from": cycle.capacity_approval_id,
        }
    )
    require(state == expected_state, "repair_service_state_changed")
    require(
        not (previous.work_dir / "drain.flag").exists(),
        "repair_service_already_stopping",
    )
    hold = _json(cycle.work_dir / "preparation-hold.json")
    require(
        hold.get("version") == 1
        and hold.get("reason") == "bootstrap_repair_required"
        and hold.get("config_hash") == cycle.fingerprint()
        and hold.get("sources") == previous.source_sha256
        and hold.get("intent_id") == intent["id"]
        and hold.get("instance_id") == intent["provider_instance_id"],
        "repair_hold_identity_mismatch",
    )
    directory = cycle.work_dir / "boot" / intent["id"]
    boot = _json(directory / "bootstrap-state.json")
    identity = {
        "intent_id": intent["id"],
        "instance_id": intent["provider_instance_id"],
        "configuration_id": previous.configuration_id,
        "sources": previous.source_sha256,
    }
    if previous.execution_backend == "wangp-worker":
        identity.update(
            backend="wangp-worker",
            engine_manifest_digest=previous.engine_manifest_digest,
        )
    if getattr(previous, "output_delivery", ""):
        identity["output_delivery"] = previous.output_delivery
    require(
        boot.get("identity") == identity
        and boot.get("phase") == "bootstrap_failed"
        and boot.get("smoke_submission_started") is None
        and not any(k in boot for k in ("fleet_process_protocol", "fleet_config_hash"))
        and not (directory / "fleet.json").exists()
        and not (directory / "fleet").exists(),
        "repair_boot_not_proven_preexecution",
    )
    return {
        "service_state_sha256": request_hash(original_state),
        "hold_sha256": request_hash(hold),
        "boot_sha256": request_hash(boot),
    }


def _snapshot(repo, conn, previous, sequence, intent_id, staged_target=None):
    require(
        type(sequence) is int
        and sequence >= 1
        and cycle_sequence_allowed(previous, sequence + 1)
        and repo.clock() + 300 < previous.stop_claiming_at,
        "repair_original_window_or_cycle_expired",
    )
    cycle = cycle_config(previous, sequence)
    workers = list(
        conn.execute(
            select(registered_workers)
            .where(registered_workers.c.pool == previous.pool)
            .order_by(registered_workers.c.id)
            .with_for_update()
        ).mappings()
    )
    require(
        all(w["state"] == "retired" and w["current_job_id"] is None for w in workers),
        "repair_worker_obligation_present",
    )
    instances = [
        dict(r)
        for r in conn.execute(
            select(instance_intents)
            .where(instance_intents.c.pool == previous.pool)
            .order_by(instance_intents.c.id)
            .with_for_update()
        ).mappings()
    ]
    require(
        instances
        and all(
            r["state"] == "destroyed"
            and repo._instance_billing(conn, r)["billing_status"] == "settled"
            for r in instances
        ),
        "repair_rental_or_billing_unresolved",
    )
    selected = [r for r in instances if r["id"] == intent_id]
    require(
        len(selected) == 1 and selected[0]["provider_instance_id"],
        "repair_intent_identity_invalid",
    )
    intent = selected[0]
    require(
        conn.execute(
            select(scaler_receipts.c.id).where(
                scaler_receipts.c.intent_id == intent_id,
                scaler_receipts.c.operation.in_(("destroy", "reconcile")),
                scaler_receipts.c.facts["state"].as_string() == "destroyed",
            )
        ).first(),
        "repair_destroy_receipt_required",
    )
    local = _evidence(previous, sequence, intent, staged_target)
    approval = repo._locked(
        conn,
        select(capacity_approvals).where(
            capacity_approvals.c.id == cycle.capacity_approval_id
        ),
    )
    link = (
        conn.execute(
            select(capacity_cycles).where(
                capacity_cycles.c.approval_id == cycle.capacity_approval_id
            )
        )
        .mappings()
        .first()
    )
    require(
        approval
        and approval["enabled"] == 0
        and link
        and link["intent_id"] == intent_id
        and approval["approval_hash"] == request_hash(approval["payload"]),
        "repair_original_approval_changed",
    )
    p = approval["payload"]
    require(
        p["tenant_id"] == previous.tenant
        and p["pool"] == previous.pool
        and p["configuration_id"] == previous.configuration_id
        and p["policy_hash"] == previous.execution_policy_sha256
        and all(
            v > repo.clock()
            for v in (
                approval["expires_at"],
                p["qualification_expires_at"],
                p["quote_expires_at"],
                p["scale_policy"]["hard_deadline"],
            )
        ),
        "repair_original_approval_expired_or_mismatched",
    )
    pending = list(
        conn.execute(
            select(jobs)
            .where(
                jobs.c.pool == previous.pool,
                jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
            )
            .order_by(jobs.c.id)
            .limit(4097)
            .with_for_update()
        ).mappings()
    )
    require(0 < len(pending) <= 4096, "repair_pending_jobs_required")
    # This preparation-only operation cannot adopt even a deferred attempt.
    # Historical terminal jobs are harmless only when their attempts are stopped.
    require(
        not conn.execute(
            select(attempts.c.id)
            .join(jobs, jobs.c.id == attempts.c.job_id)
            .where(jobs.c.pool == previous.pool, attempts.c.upstream_stopped != 1)
        ).first(),
        "repair_pool_attempt_unresolved",
    )
    waiters, reservations, account_ids = [], [], set(previous.budget_account_ids)
    for job in pending:
        e = job["execution_plan"]
        require(
            job["status"] == "waiting_capacity"
            and job["tenant_id"] == previous.tenant
            and job["owner_id"] in previous.allowed_owners
            and job["attempt_no"] == 0
            and job["current_attempt_id"] is None
            and job["lease_worker_id"] is None
            and job["lease_expires_at"] is None
            and job["cancel_from_status"] is None
            and job["result"] is None
            and e.get("enabled") is True
            and e.get("capacity_approval_id") == approval["id"]
            and e.get("capacity_approval_hash") == approval["approval_hash"]
            and e.get("configuration_id") == previous.configuration_id
            and e.get("policy_hash") == previous.execution_policy_sha256
            and _matches_engine(p, e)
            and e.get("quote_known") is True
            and e.get("qualification_evidence_id") == p["qualification_evidence_id"]
            and job["request"].get("recipe_id") in p["recipe_ids"]
            and job["request"].get("request", {}).get("model") == p["model_id"]
            and not conn.execute(
                select(attempts.c.id).where(attempts.c.job_id == job["id"])
            ).first()
            and not conn.execute(
                select(artifacts.c.id).where(artifacts.c.job_id == job["id"])
            ).first(),
            "repair_job_not_unsubmitted",
        )
        plan = (
            conn.execute(select(plans).where(plans.c.id == job["plan_id"]))
            .mappings()
            .first()
        )
        require(
            plan
            and plan["request"] == job["request"]
            and plan["estimated_cost_microusd"] == job["estimated_cost_microusd"]
            and job["request_hash"]
            == request_hash(
                {
                    "request_hash": plan["request_hash"],
                    "plan_hash": plan["plan_hash"],
                    "estimated_cost_microusd": plan["estimated_cost_microusd"],
                }
            ),
            "repair_original_request_changed",
        )
        waiter = repo._locked(
            conn, select(capacity_waiters).where(capacity_waiters.c.job_id == job["id"])
        )
        require(
            waiter
            and waiter["state"] == "waiting_capacity"
            and waiter["approval_id"] == approval["id"]
            and waiter["approval_hash"] == approval["approval_hash"]
            and waiter["intent_id"] in (None, intent_id)
            and repo.clock() < waiter["deadline"],
            "repair_original_wait_deadline_expired",
        )
        waiters.append(dict(waiter))
        held = [
            dict(r)
            for r in conn.execute(
                select(budget_reservations)
                .where(
                    budget_reservations.c.reference_type == "job",
                    budget_reservations.c.reference_id == job["id"],
                )
                .order_by(budget_reservations.c.id)
                .with_for_update()
            ).mappings()
        ]
        require(
            held
            and sorted(r["account_id"] for r in held)
            == sorted(e.get("budget_account_ids", []))
            and all(
                r["state"] == "reserved"
                and r["actual_cost_microusd"] is None
                and r["amount_microusd"] == job["estimated_cost_microusd"]
                for r in held
            ),
            "repair_job_reservation_changed",
        )
        reservations.extend(held)
        account_ids.update(r["account_id"] for r in held)
    accounts = [
        dict(r)
        for r in conn.execute(
            select(budget_accounts)
            .where(budget_accounts.c.id.in_(account_ids))
            .order_by(budget_accounts.c.id)
            .with_for_update()
        ).mappings()
    ]
    require({r["id"] for r in accounts} == account_ids, "repair_budget_account_missing")
    actions = [
        dict(r)
        for r in conn.execute(
            select(scaler_actions)
            .where(scaler_actions.c.intent_id.in_([r["id"] for r in instances]))
            .order_by(scaler_actions.c.intent_id)
        ).mappings()
    ]
    return {
        **local,
        "job_hashes": {j["id"]: request_hash(dict(j)) for j in pending},
        "waiter_deadlines": {w["job_id"]: w["deadline"] for w in waiters},
        "waiters_sha256": request_hash(waiters),
        "reservations_sha256": request_hash(reservations),
        "accounts_sha256": request_hash(accounts),
        "instances_sha256": request_hash(instances),
        "actions_sha256": request_hash(actions),
        "approval_sha256": request_hash(dict(approval)),
    }


def prepare(repo, previous, target, proof, *, apply=False):
    require(
        isinstance(proof, dict) and proof.get("version") == 1, "repair_proof_invalid"
    )
    changed = validate_source_binding(previous, target, proof)
    frozen = proof.get("frozen", {})
    require(
        frozen.get("config_hash") == previous.fingerprint()
        and frozen.get("running") is True
        and frozen.get("paused") is True
        and frozen.get("restart_count") == 0
        and frozen.get("process_count") == 1
        and frozen.get("boot_children") == 0
        and finite(frozen.get("frozen_at"))
        and 0 <= repo.clock() - frozen["frozen_at"] <= 180,
        "repair_exact_controller_not_frozen",
    )
    require(
        proof.get("reviewed_source_files") == changed
        and proof.get("target_config_hash") == target.fingerprint()
        and all(
            isinstance(proof.get(k), str) and re.fullmatch(r"[0-9a-f]{40}", proof[k])
            for k in ("previous_commit", "target_commit")
        )
        and proof["previous_commit"] != proof["target_commit"],
        "repair_reviewed_release_required",
    )
    rid = str(
        uuid.uuid5(uuid.NAMESPACE_URL, OPERATION + ":" + proof.get("intent_id", ""))
    )
    with repo.transaction() as conn:
        repo._lock_capacity(conn)
        require(
            not conn.execute(
                select(scaler_receipts.c.id).where(scaler_receipts.c.id == rid)
            ).first(),
            "repair_already_prepared",
        )
        snapshot = _snapshot(
            repo, conn, previous, proof.get("sequence"), proof.get("intent_id")
        )
        require(
            sorted(snapshot["job_hashes"]) == proof.get("job_ids"),
            "repair_explicit_job_set_changed",
        )
        leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == previous.pool)
        )
        require(leader is not None, "repair_original_leader_missing")
        receipt = {
            "version": 1,
            "phase": "fenced",
            "receipt_id": rid,
            "intent_id": proof["intent_id"],
            "pool": previous.pool,
            "old_config_hash": previous.fingerprint(),
            "target_config_hash": target.fingerprint(),
            "previous_commit": proof["previous_commit"],
            "target_commit": proof["target_commit"],
            "previous_sequence": proof["sequence"],
            "next_sequence": proof["sequence"] + 1,
            "transfer_from": cycle_config(
                previous, proof["sequence"]
            ).capacity_approval_id,
            "fenced_leader_id": "repair-" + rid,
            "fence": leader["fence"] + 1,
            "proof_sha256": request_hash(proof),
            "snapshot": snapshot,
        }
        # Reject oversized receipts before fencing; a transport limit must
        # never turn an otherwise completed transaction into an unknown result.
        require(
            len(json.dumps(receipt, sort_keys=True, allow_nan=False).encode()) <= 49152,
            "repair_receipt_exceeds_safe_transport_limit",
        )
        if apply:
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == previous.pool)
                .values(
                    leader_id=receipt["fenced_leader_id"],
                    fence=receipt["fence"],
                    expires_at=previous.hard_deadline,
                )
            )
            conn.execute(
                insert(scaler_receipts).values(
                    id=rid,
                    intent_id=proof["intent_id"],
                    operation=OPERATION,
                    observed_at=repo.clock(),
                    facts=receipt,
                )
            )
        return receipt if apply else {**receipt, "phase": "dry_run"}


def verify(repo, previous, target, receipt, *, release_leader=False):
    validate_delta(previous, target)
    require(
        receipt.get("version") == 1
        and receipt.get("phase") == "fenced"
        and receipt.get("old_config_hash") == previous.fingerprint()
        and receipt.get("target_config_hash") == target.fingerprint(),
        "repair_receipt_invalid",
    )
    with repo.transaction() as conn:
        repo._lock_capacity(conn)
        marker = (
            conn.execute(
                select(scaler_receipts).where(
                    scaler_receipts.c.id == receipt.get("receipt_id"),
                    scaler_receipts.c.operation == OPERATION,
                )
            )
            .mappings()
            .first()
        )
        require(
            marker
            and marker["facts"]
            == {k: v for k, v in receipt.items() if k != "host_stage_confirmed"},
            "repair_receipt_not_authoritative",
        )
        require(
            _snapshot(
                repo,
                conn,
                previous,
                receipt["previous_sequence"],
                receipt["intent_id"],
                target if receipt.get("host_stage_confirmed") is True else None,
            )
            == receipt["snapshot"],
            "repair_ledger_or_runtime_changed",
        )
        leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == previous.pool)
        )
        require(
            leader
            and leader["leader_id"] == receipt["fenced_leader_id"]
            and leader["fence"] == receipt["fence"]
            and leader["expires_at"] == previous.hard_deadline,
            "repair_leader_fence_changed",
        )
        if release_leader:
            require(
                receipt.get("host_stage_confirmed") is True,
                "repair_host_stage_required",
            )
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == previous.pool)
                .values(expires_at=0)
            )
            conn.execute(
                update(scaler_receipts)
                .where(scaler_receipts.c.id == receipt["receipt_id"])
                .values(facts={**marker["facts"], "phase": "released"})
            )
    return {
        "verified": True,
        "leader_released": release_leader,
        "job_ids": sorted(receipt["snapshot"]["job_hashes"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-config", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument(
        "--action", choices=("prepare", "verify", "release-leader"), required=True
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    repo = None
    try:
        previous, target = (
            read_config(args.previous_config),
            read_config(args.target_config),
        )
        raw = sys.stdin.buffer.read(131073)
        require(len(raw) <= 131072, "repair_proof_too_large")
        value = json.loads(raw)
        repo = Repository(Settings.from_environment().database_url)
        if args.action == "prepare":
            result = prepare(repo, previous, target, value, apply=args.apply)
        else:
            require(
                args.action != "release-leader" or args.apply,
                "repair_explicit_apply_required",
            )
            result = verify(
                repo,
                previous,
                target,
                value,
                release_leader=args.action == "release-leader",
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
                    "safe_error": "repair_configuration_or_evidence_invalid",
                }
            )
        )
        return 1
    finally:
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
