"""One-use operator recovery after an evidenced pre-inference boot failure.

No cloud requests, schema changes, budget approvals or controller startup. The
host supplies a fresh protected retirement proof; the database independently
proves the original jobs, zero generation charges and paid-instance settlement.
This module is intentionally not available through the public HTTP API.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

from .budget_policy import effective_budget_limit, fixed_cap_retired

from sqlalchemy import insert, select, update

from .backlog_handoff import finite, require
from .on_demand_scaler import cycle_config, json_config, read_config
from .repository import (
    Conflict,
    Repository,
    attempts,
    artifacts,
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
from .settings import Settings

OPERATION = "operator-job-recovery"
FAILURE = "capacity_approval_expired_or_revoked"


def _repair_identity(previous, target, proof):
    a, b = json_config(previous), json_config(target)
    require(
        previous.service_mode == target.service_mode == "on-demand"
        and previous.allowed_owners
        == target.allowed_owners
        == ["superdan", "supervan"],
        "preparation_recovery_scope_mismatch",
    )
    # A repaired runtime/source may change; the approved purchase, deadlines,
    # settings, identities and original service creation must remain exact.
    mutable = {"source_dir", "source_sha256"}
    require(
        all(a[k] == b[k] for k in a if k not in mutable),
        "preparation_recovery_original_approval_changed",
    )
    repair = proof.get("repair", {})
    require(
        repair.get("target_config_hash") == target.fingerprint()
        and repair.get("target_sources") == target.source_sha256
        and repair.get("acknowledge_legacy_cancel_gap") is True,
        "preparation_recovery_explicit_repair_required",
    )
    old_revision, new_revision = (
        repair.get("previous_runtime_revision"),
        repair.get("target_runtime_revision"),
    )
    require(
        all(
            isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v)
            for v in (old_revision, new_revision)
        )
        and old_revision != new_revision,
        "preparation_recovery_same_runtime_forbidden",
    )
    require(
        repair.get("reason") == "bootstrap_cache_storage_fix",
        "preparation_recovery_repair_reason_unapproved",
    )


def _proof(previous, target, proof, intent, cycle, now):
    require(
        proof.get("version") == 1
        and proof.get("old_config_hash") == previous.fingerprint(),
        "preparation_recovery_proof_identity_mismatch",
    )
    _repair_identity(previous, target, proof)
    retired, provider, boot = (
        proof.get(k, {}) for k in ("retired", "provider", "bootstrap_failure")
    )
    require(
        retired.get("controller_exited") is True
        and retired.get("no_restart") is True
        and retired.get("process_count") == 0
        and type(retired.get("process_count")) is int
        and retired.get("boot_children") == 0
        and type(retired.get("boot_children")) is int
        and finite(retired.get("observed_at"))
        and 0 <= now - retired["observed_at"] <= 180,
        "preparation_recovery_controller_not_retired",
    )
    require(
        provider.get("service") == "lium"
        and provider.get("profile") == "lium--rig-root"
        and provider.get("base_url") == "https://lium.io/api"
        and provider.get("http_status") == 200
        and provider.get("deleted_pod_id") == intent["provider_instance_id"]
        and provider.get("live_pod_ids") == []
        and provider.get("pagination_complete") is True
        and finite(provider.get("observed_at"))
        and 0 <= now - provider["observed_at"] <= 180,
        "preparation_recovery_provider_retirement_unconfirmed",
    )
    require(
        boot.get("intent_id") == intent["id"]
        and boot.get("instance_id") == intent["provider_instance_id"]
        and boot.get("config_hash") == cycle.fingerprint()
        and boot.get("sources") == previous.source_sha256
        and boot.get("phase") == "bootstrap_failed"
        and boot.get("no_qualification_submission") is True
        and boot.get("no_fleet") is True
        and boot.get("safe_error") == "InsufficientCacheDiskSpace",
        "preparation_recovery_boot_failure_unconfirmed",
    )


def _receipt_id(intent_id, job_id):
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "sixnine:preparation-recovery:" + intent_id + ":" + job_id,
        )
    )


def prepare(repo, previous, target, proof, *, apply=False):
    """Restore explicitly listed, never-submitted jobs under their original grant.

    The host must stage sequence+1 with transfer_from before releasing the
    fenced leader. No old controller may tick the disabled original grant.
    """
    require(isinstance(proof, dict), "preparation_recovery_proof_invalid")
    sequence, ids = proof.get("sequence"), proof.get("job_ids")
    require(
        type(sequence) is int and 1 <= sequence < previous.max_cycles,
        "preparation_recovery_cycle_limit",
    )
    require(
        isinstance(ids, list)
        and 1 <= len(ids) <= 64
        and ids == sorted(set(ids))
        and all(isinstance(v, str) and len(v) == 36 for v in ids),
        "preparation_recovery_explicit_job_ids_required",
    )
    cycle = cycle_config(previous, sequence)
    require(
        repo.clock() + 300 < previous.stop_claiming_at,
        "preparation_recovery_original_window_expired",
    )
    with repo.transaction() as conn:
        # Normal claims: capacity -> worker -> job; settlement: job ->
        # reservation -> account. Do not introduce the opposite lock order.
        repo._lock_capacity(conn)
        workers = list(
            conn.execute(
                select(registered_workers)
                .where(registered_workers.c.pool == previous.pool)
                .order_by(registered_workers.c.id)
                .with_for_update()
            ).mappings()
        )
        require(
            all(
                w["state"] == "retired" and w["current_job_id"] is None for w in workers
            ),
            "preparation_recovery_worker_not_retired",
        )
        pool_rows = [
            dict(r)
            for r in conn.execute(
                select(instance_intents)
                .where(instance_intents.c.pool == previous.pool)
                .order_by(instance_intents.c.id)
                .with_for_update()
            ).mappings()
        ]
        require(
            pool_rows
            and all(
                r["state"] == "destroyed"
                and repo._instance_billing(conn, r)["billing_status"] == "settled"
                for r in pool_rows
            ),
            "preparation_recovery_instances_not_destroyed_and_settled",
        )
        matching = [r for r in pool_rows if r["id"] == proof.get("intent_id")]
        require(
            len(matching) == 1
            and matching[0]["provider"] == "lium"
            and matching[0]["provider_instance_id"] is not None,
            "preparation_recovery_paid_intent_identity_mismatch",
        )
        intent = matching[0]
        action = (
            conn.execute(
                select(scaler_actions).where(scaler_actions.c.intent_id == intent["id"])
            )
            .mappings()
            .first()
        )
        require(
            action
            and action["launch_spec"] in previous.launches
            and conn.execute(
                select(scaler_receipts.c.id).where(
                    scaler_receipts.c.intent_id == intent["id"],
                    scaler_receipts.c.operation.in_(("destroy", "reconcile")),
                    scaler_receipts.c.facts["state"].as_string() == "destroyed",
                )
            ).first(),
            "preparation_recovery_destroy_receipt_missing",
        )
        _proof(previous, target, proof, intent, cycle, repo.clock())
        old = repo._locked(
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
            old
            and old["enabled"] == 0
            and link
            and link["intent_id"] == intent["id"]
            and old["payload"]["policy_hash"] == previous.execution_policy_sha256,
            "preparation_recovery_original_grant_mismatch",
        )
        p = old["payload"]
        require(
            old["approval_hash"] == request_hash(p)
            and p["tenant_id"] == previous.tenant
            and p["pool"] == previous.pool
            and p["configuration_id"] == previous.configuration_id
            and p["scale_policy"] == previous.scale_policy
            and p["launch"] in previous.launches
            and p["budget_account_ids"] == sorted(previous.budget_account_ids)
            and p["qualification_evidence_id"] == previous.qualification_evidence_id
            and p["budget_scope"]
            == {
                "tenant_id": previous.tenant,
                "owner_id": previous.owner,
                "project_id": previous.project_id,
                "actor_id": "browser",
            },
            "preparation_recovery_original_grant_configuration_changed",
        )
        require(
            all(
                v > repo.clock()
                for v in (
                    old["expires_at"],
                    p["quote_expires_at"],
                    p["qualification_expires_at"],
                    p["scale_policy"]["hard_deadline"],
                )
            ),
            "preparation_recovery_original_grant_expired",
        )
        require(
            not conn.execute(
                select(attempts.c.id)
                .join(jobs, jobs.c.id == attempts.c.job_id)
                .where(
                    jobs.c.pool == previous.pool,
                    (
                        (attempts.c.submission_started_at.is_not(None))
                        | (attempts.c.upstream_task_id.is_not(None))
                    ),
                    (
                        (attempts.c.upstream_stopped != 1)
                        | (
                            attempts.c.status.not_in(
                                ("succeeded", "failed", "cancelled")
                            )
                        )
                    ),
                )
            ).first(),
            "preparation_recovery_pool_attempt_unresolved",
        )
        existing = list(
            conn.execute(
                select(scaler_receipts).where(
                    scaler_receipts.c.operation == OPERATION,
                    scaler_receipts.c.facts["job_id"].as_string().in_(ids),
                )
            ).mappings()
        )
        require(not existing, "preparation_recovery_job_already_recovered")
        pending = list(
            conn.execute(
                select(jobs.c.id)
                .where(
                    jobs.c.pool == previous.pool,
                    jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
                )
                .limit(4097)
            ).scalars()
        )
        require(
            len(pending) <= 4096 and set(pending) <= set(ids),
            "preparation_recovery_unlisted_backlog",
        )
        selected, dry_reserved = [], {}
        for jid in ids:
            job = repo._job(conn, jid, lock=True)
            execution = job["execution_plan"]
            failed = job["status"] == "failed"
            require(
                job["status"] in ("failed", "waiting_capacity")
                and (not failed or job["error_code"] == FAILURE)
                and job["attempt_no"] == 0
                and job["current_attempt_id"] is None
                and job["lease_worker_id"] is None
                and job["lease_expires_at"] is None
                and job["cancel_from_status"] is None
                and not (job["result"] or {}).get("recovery_cancel_requested")
                and not conn.execute(
                    select(attempts.c.id).where(attempts.c.job_id == jid)
                ).first()
                and not conn.execute(
                    select(artifacts.c.id).where(artifacts.c.job_id == jid)
                ).first(),
                "preparation_recovery_job_not_unsubmitted_zero_charge",
            )
            require(
                job["tenant_id"] == previous.tenant
                and job["owner_id"] in previous.allowed_owners
                and job["pool"] == previous.pool
                and execution.get("configuration_id") == previous.configuration_id
                and execution.get("capacity_approval_id") == old["id"]
                and execution.get("capacity_approval_hash") == old["approval_hash"]
                and execution.get("policy_hash") == previous.execution_policy_sha256
                and execution.get("backend") == "comfy-worker"
                and execution.get("enabled") is True
                and execution.get("quote_known") is True
                and execution.get("qualification_evidence_id")
                == p["qualification_evidence_id"]
                and job["request"].get("recipe_id") in p["recipe_ids"]
                and job["request"].get("request", {}).get("model") == p["model_id"],
                "preparation_recovery_job_identity_mismatch",
            )
            plan = (
                conn.execute(select(plans).where(plans.c.id == job["plan_id"]))
                .mappings()
                .one()
            )
            require(
                plan["request"] == job["request"]
                and plan["estimated_cost_microusd"] == job["estimated_cost_microusd"]
                and job["request_hash"]
                == request_hash(
                    {
                        "request_hash": plan["request_hash"],
                        "plan_hash": plan["plan_hash"],
                        "estimated_cost_microusd": plan["estimated_cost_microusd"],
                    }
                )
                and (
                    job["result"] is None
                    or job["result"]
                    == {"billing_status": "settled", "actual_cost_microusd": 0}
                ),
                "preparation_recovery_original_request_or_charge_changed",
            )
            reservations = list(
                conn.execute(
                    select(budget_reservations)
                    .where(
                        budget_reservations.c.reference_type == "job",
                        budget_reservations.c.reference_id == jid,
                    )
                    .order_by(budget_reservations.c.account_id)
                    .with_for_update()
                ).mappings()
            )
            require(
                sorted(r["account_id"] for r in reservations)
                == sorted(execution.get("budget_account_ids", []))
                and reservations
                and all(
                    r["amount_microusd"] == job["estimated_cost_microusd"]
                    and (
                        r["state"] == "released" and r["actual_cost_microusd"] == 0
                        if failed
                        else r["state"] == "reserved"
                        and r["actual_cost_microusd"] is None
                    )
                    for r in reservations
                ),
                "preparation_recovery_original_job_reservation_mismatch",
            )
            for reservation in reservations:
                account = repo._locked(
                    conn,
                    select(budget_accounts).where(
                        budget_accounts.c.id == reservation["account_id"]
                    ),
                )
                require(
                    account
                    and account["tenant_id"] == job["tenant_id"]
                    and account["owner_id"] in (None, job["owner_id"])
                    and account["project_id"] in (None, job["project_id"]),
                    "preparation_recovery_budget_scope_mismatch",
                )
                if failed:
                    extra = 0 if apply else dry_reserved.get(account["id"], 0)
                    require(
                        account["spent_microusd"]
                        + account["reserved_microusd"]
                        + extra
                        + reservation["amount_microusd"]
                        <= effective_budget_limit(account),
                        "preparation_recovery_original_budget_unavailable",
                    )
                    dry_reserved[account["id"]] = extra + reservation["amount_microusd"]
                    if apply:
                        updated = conn.execute(
                            update(budget_accounts)
                            .where(
                                budget_accounts.c.id == account["id"],
                                budget_accounts.c.spent_microusd
                                + budget_accounts.c.reserved_microusd
                                + reservation["amount_microusd"]
                                <= effective_budget_limit(account),
                            )
                            .values(
                                reserved_microusd=budget_accounts.c.reserved_microusd
                                + reservation["amount_microusd"]
                            )
                        )
                        require(
                            updated.rowcount == 1, "preparation_recovery_budget_changed"
                        )
                        updated = conn.execute(
                            update(budget_reservations)
                            .where(
                                budget_reservations.c.id == reservation["id"],
                                budget_reservations.c.state == "released",
                                budget_reservations.c.actual_cost_microusd == 0,
                            )
                            .values(state="reserved", actual_cost_microusd=None)
                        )
                        require(
                            updated.rowcount == 1,
                            "preparation_recovery_reservation_changed",
                        )
            waiter = repo._locked(
                conn, select(capacity_waiters).where(capacity_waiters.c.job_id == jid)
            )
            require(
                waiter
                and waiter["approval_id"] == old["id"]
                and waiter["approval_hash"] == old["approval_hash"]
                and waiter["intent_id"] in (None, intent["id"])
                and waiter["state"] == ("failed" if failed else "waiting_capacity")
                and repo.clock()
                < waiter["deadline"]
                <= min(
                    old["expires_at"],
                    p["quote_expires_at"],
                    p["qualification_expires_at"],
                    p["scale_policy"]["hard_deadline"] - job["expected_runtime_s"],
                ),
                "preparation_recovery_original_wait_deadline_invalid",
            )
            selected.append(
                {
                    "job_id": jid,
                    "original_deadline": waiter["deadline"],
                    "original_state": job["status"],
                    "request_hash": job["request_hash"],
                    "original_reservations_sha256": request_hash(
                        [dict(r) for r in reservations]
                    ),
                }
            )
            if apply:
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == jid)
                    .values(
                        status="waiting_capacity",
                        error_code="capacity_bootstrap_repair_required",
                        result=None,
                        fence=job["fence"] + 1,
                        updated_at=repo.clock(),
                        not_before=repo.clock(),
                    )
                )
                conn.execute(
                    update(capacity_waiters)
                    .where(capacity_waiters.c.job_id == jid)
                    .values(state="waiting_capacity")
                )
                conn.execute(
                    insert(scaler_receipts).values(
                        id=_receipt_id(intent["id"], jid),
                        intent_id=intent["id"],
                        operation=OPERATION,
                        observed_at=repo.clock(),
                        facts={
                            **selected[-1],
                            "evidence_sha256": request_hash(proof),
                            "old_config_hash": previous.fingerprint(),
                            "target_config_hash": target.fingerprint(),
                            "previous_runtime_revision": proof["repair"][
                                "previous_runtime_revision"
                            ],
                            "target_runtime_revision": proof["repair"][
                                "target_runtime_revision"
                            ],
                        },
                    )
                )
                repo._emit(
                    conn,
                    "job.preparation_recovered",
                    jid,
                    {
                        "job_id": jid,
                        "status": "waiting_capacity",
                        "generation_resubmitted": False,
                        "reason": "bootstrap_repair_required",
                    },
                )
        leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == previous.pool)
        )
        require(leader is not None, "preparation_recovery_leader_missing")
        fenced = "preparation-recovery-" + request_hash(proof)[:20]
        if apply:
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == previous.pool)
                .values(
                    leader_id=fenced,
                    fence=leader["fence"] + 1,
                    expires_at=previous.hard_deadline,
                )
            )
        account_ids = set(previous.budget_account_ids) | {
            bid
            for j in selected
            for bid in repo._job(conn, j["job_id"])["execution_plan"][
                "budget_account_ids"
            ]
        }
        accounts = {
            r["id"]: {
                k: r[k]
                for k in ("limit_microusd", "spent_microusd", "reserved_microusd")
            }
            for r in conn.execute(
                select(budget_accounts)
                .where(budget_accounts.c.id.in_(account_ids))
                .order_by(budget_accounts.c.id)
            ).mappings()
        }
        return {
            "version": 1,
            "phase": "jobs_restored" if apply else "dry_run",
            "pool": previous.pool,
            "old_config_hash": previous.fingerprint(),
            "target_config_hash": target.fingerprint(),
            "target_runtime_revision": proof["repair"]["target_runtime_revision"],
            "created_at": previous.created_at,
            "hard_deadline": previous.hard_deadline,
            "previous_sequence": sequence,
            "next_sequence": sequence + 1,
            "previous_approval_id": old["id"],
            "next_approval_id": cycle_config(target, sequence + 1).capacity_approval_id,
            "intent_id": intent["id"],
            "evidence_sha256": request_hash(proof),
            "fenced_leader_id": fenced,
            "fence": leader["fence"] + 1,
            "jobs": selected,
            "budget_accounts": accounts,
            "restored_job_hashes": {
                jid: request_hash(repo._job(conn, jid)) for jid in ids
            },
            "restored_reservations_sha256": request_hash(
                [
                    dict(r)
                    for r in conn.execute(
                        select(budget_reservations)
                        .where(
                            budget_reservations.c.reference_type == "job",
                            budget_reservations.c.reference_id.in_(ids),
                        )
                        .order_by(budget_reservations.c.id)
                    ).mappings()
                ]
            ),
        }


def verify(repo, target, receipt, *, release_leader=False):
    """Recheck the receipt before host flags/state change and leader release."""
    sequence = receipt.get("previous_sequence")
    require(
        receipt.get("version") == 1
        and receipt.get("phase") == "jobs_restored"
        and receipt.get("target_config_hash") == target.fingerprint()
        and receipt.get("created_at") == target.created_at
        and receipt.get("hard_deadline") == target.hard_deadline
        and type(sequence) is int
        and 1 <= sequence < target.max_cycles
        and receipt.get("next_sequence") == sequence + 1
        and receipt.get("previous_approval_id")
        == cycle_config(target, sequence).capacity_approval_id
        and receipt.get("next_approval_id")
        == cycle_config(target, sequence + 1).capacity_approval_id
        and repo.clock() + 300 < target.stop_claiming_at,
        "preparation_recovery_receipt_identity_mismatch",
    )
    with repo.transaction() as conn:
        repo._lock_capacity(conn)
        rows = [
            dict(r)
            for r in conn.execute(
                select(instance_intents).where(instance_intents.c.pool == target.pool)
            ).mappings()
        ]
        require(
            rows
            and all(
                r["state"] == "destroyed"
                and repo._instance_billing(conn, r)["billing_status"] == "settled"
                for r in rows
            ),
            "preparation_recovery_instances_changed",
        )
        leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == target.pool)
        )
        require(
            leader
            and leader["leader_id"] == receipt.get("fenced_leader_id")
            and leader["fence"] == receipt.get("fence"),
            "preparation_recovery_leader_changed",
        )
        ids = sorted(receipt["restored_job_hashes"])
        markers = list(
            conn.execute(
                select(scaler_receipts).where(
                    scaler_receipts.c.operation == OPERATION,
                    scaler_receipts.c.facts["evidence_sha256"].as_string()
                    == receipt["evidence_sha256"],
                )
            ).mappings()
        )
        link = (
            conn.execute(
                select(capacity_cycles).where(
                    capacity_cycles.c.approval_id == receipt["previous_approval_id"]
                )
            )
            .mappings()
            .first()
        )
        require(
            link
            and link["intent_id"] == receipt["intent_id"]
            and markers
            and sorted(m["facts"]["job_id"] for m in markers) == ids
            and sorted(j["job_id"] for j in receipt["jobs"]) == ids,
            "preparation_recovery_receipt_job_set_changed",
        )
        for jid in ids:
            job = repo._job(conn, jid, lock=True)
            marker = (
                conn.execute(
                    select(scaler_receipts).where(
                        scaler_receipts.c.id == _receipt_id(receipt["intent_id"], jid),
                        scaler_receipts.c.operation == OPERATION,
                    )
                )
                .mappings()
                .first()
            )
            waiter = (
                conn.execute(
                    select(capacity_waiters).where(capacity_waiters.c.job_id == jid)
                )
                .mappings()
                .one()
            )
            require(
                marker
                and marker["facts"]["target_config_hash"] == target.fingerprint()
                and marker["facts"]["evidence_sha256"] == receipt["evidence_sha256"]
                and request_hash(job) == receipt["restored_job_hashes"][jid]
                and waiter["deadline"]
                == marker["facts"]["original_deadline"]
                > repo.clock()
                and waiter["state"] == "waiting_capacity",
                "preparation_recovery_restored_job_changed",
            )
        reservations = [
            dict(r)
            for r in conn.execute(
                select(budget_reservations)
                .where(
                    budget_reservations.c.reference_type == "job",
                    budget_reservations.c.reference_id.in_(ids),
                )
                .order_by(budget_reservations.c.id)
            ).mappings()
        ]
        require(
            request_hash(reservations) == receipt["restored_reservations_sha256"],
            "preparation_recovery_restored_reservations_changed",
        )
        for bid, value in receipt["budget_accounts"].items():
            account = repo._locked(
                conn, select(budget_accounts).where(budget_accounts.c.id == bid)
            )
            require(
                account and all(account[k] == v for k, v in value.items()),
                "preparation_recovery_budget_changed",
            )
        if release_leader:
            require(
                receipt.get("host_stage_confirmed") is True,
                "preparation_recovery_host_stage_required",
            )
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == target.pool)
                .values(expires_at=0)
            )
    return {"verified": True, "leader_released": release_leader, "job_ids": ids}


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
        require(len(raw) <= 131072, "preparation_recovery_proof_too_large")
        value = json.loads(raw)
        repo = Repository(Settings.from_environment().database_url)
        if args.action == "prepare":
            result = prepare(repo, previous, target, value, apply=args.apply)
        else:
            require(
                args.action != "release-leader" or args.apply,
                "preparation_recovery_explicit_apply_required",
            )
            result = verify(
                repo, target, value, release_leader=args.action == "release-leader"
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
                    "safe_error": "preparation_recovery_configuration_or_evidence_invalid",
                }
            )
        )
        return 1
    finally:
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
