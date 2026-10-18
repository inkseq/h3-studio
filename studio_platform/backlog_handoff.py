"""Explicit operator handoff for a frozen controller with an unsubmitted backlog.

No provider calls, schema creation, task cancellation or new budget approval.
The caller must collect the authenticated account audit after freezing the exact
old container. This is a privileged recovery tool, never an HTTP/API endpoint.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import uuid

from sqlalchemy import insert, select, update

from .capacity import proven_unsubmitted_capacity_job
from .on_demand_scaler import cycle_config, read_config
from .repository import (
    Conflict,
    Repository,
    budget_accounts,
    budget_reservations,
    attempts,
    capacity_approvals,
    capacity_cycles,
    capacity_waiters,
    instance_intents,
    jobs,
    registered_workers,
    request_hash,
    scaler_actions,
    scaler_leaders,
    scaler_receipts,
)
from .scaler import ProviderFact
from .settings import Settings


def require(value, code):
    if not value:
        raise Conflict(code)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def snapshot(repo, config, connection):
    """Digest private requests instead of returning media, prompts or results."""
    rows = [
        dict(r)
        for r in connection.execute(
            select(instance_intents)
            .where(instance_intents.c.pool == config.pool)
            .order_by(instance_intents.c.id)
        ).mappings()
    ]
    pending = [
        dict(r)
        for r in connection.execute(
            select(jobs)
            .where(
                jobs.c.pool == config.pool,
                jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
            )
            .order_by(jobs.c.id)
            .with_for_update()
        ).mappings()
    ]
    require(0 < len(pending) <= 4096, "handoff_backlog_missing_or_too_large")
    require(
        all(
            j["tenant_id"] == config.tenant
            and j["owner_id"] in config.allowed_owners
            and j["execution_plan"].get("configuration_id") == config.configuration_id
            and proven_unsubmitted_capacity_job(connection, j)
            for j in pending
        ),
        "handoff_backlog_submission_requires_reconciliation",
    )
    workers = list(
        connection.execute(
            select(registered_workers)
            .where(registered_workers.c.pool == config.pool)
            .with_for_update()
        ).mappings()
    )
    require(
        all(w["state"] == "retired" and w["current_job_id"] is None for w in workers),
        "handoff_worker_not_retired",
    )
    require(
        not connection.execute(
            select(attempts.c.id)
            .join(jobs, jobs.c.id == attempts.c.job_id)
            .where(
                jobs.c.pool == config.pool,
                (
                    (attempts.c.submission_started_at.is_not(None))
                    | (attempts.c.upstream_task_id.is_not(None))
                ),
                attempts.c.status != "completed",
                attempts.c.upstream_stopped != 1,
            )
        ).first(),
        "handoff_unresolved_attempt_requires_reconciliation",
    )
    job_ids = [j["id"] for j in pending]
    reservations = [
        dict(r)
        for r in connection.execute(
            select(budget_reservations)
            .where(
                budget_reservations.c.reference_type == "job",
                budget_reservations.c.reference_id.in_(job_ids),
            )
            .order_by(budget_reservations.c.id)
        ).mappings()
    ]
    require(
        all(r["state"] == "reserved" for r in reservations),
        "handoff_job_reservation_not_held",
    )
    return (
        rows,
        pending,
        {
            "job_hashes": {j["id"]: request_hash(j) for j in pending},
            "job_reservations_sha256": request_hash(reservations),
        },
    )


def validate_proof(config, intent, action, proof, now):
    require(
        isinstance(proof, dict) and proof.get("version") == 1, "handoff_proof_invalid"
    )
    frozen, audit = proof.get("frozen", {}), proof.get("audit", {})
    require(
        frozen.get("config_hash") == config.fingerprint()
        and frozen.get("running") is True
        and frozen.get("paused") is True
        and frozen.get("restart_count") == 0
        and type(frozen.get("pid")) is int
        and frozen["pid"] > 0
        and frozen.get("process_count") == 1
        and frozen.get("boot_children") == 0
        and finite(frozen.get("started_at"))
        and frozen["started_at"] <= intent["created_at"]
        and finite(frozen.get("frozen_at"))
        and 0 <= now - frozen["frozen_at"] <= 180,
        "handoff_exact_controller_not_frozen",
    )
    require(
        audit.get("service") == "lium"
        and audit.get("profile") == "lium--rig-root"
        and audit.get("base_url") == "https://lium.io/api"
        and audit.get("http_status") == 200
        and audit.get("items") == []
        and audit.get("next_cursor") is None
        and audit.get("request_filter")
        == {
            "method": "POST",
            "route": "/executors/{executor_uuid}/rent",
            "executor_uuid": action["launch_spec"]["offer_id"],
        }
        and audit.get("pod_tag") == "sixnine-" + intent["id"]
        and audit.get("live_tag_matches") == 0
        and audit.get("billed_tag_matches") == 0
        and finite(audit.get("since"))
        and audit["since"] <= action["create_started_at"] - 30
        and finite(audit.get("observed_at"))
        and frozen["frozen_at"] <= audit["observed_at"] <= now
        and now - audit["observed_at"] <= 120
        and action["create_started_at"]
        <= audit.get("first_unknown_at", -1)
        <= frozen["frozen_at"] - 60,
        "handoff_account_audit_incomplete_or_stale",
    )
    try:
        uuid.UUID(audit["account_id"])
        uuid.UUID(audit["api_key_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Conflict("handoff_account_identity_unverified") from None
    controls = audit.get("positive_controls", [])
    require(
        isinstance(controls, list)
        and len(controls) >= 2
        and len({v.get("pod_id") for v in controls if isinstance(v, dict)}) >= 2
        and all(
            isinstance(v, dict)
            and v.get("action") == "pod.create"
            and v.get("method") == "POST"
            and v.get("status_code") == 200
            and v.get("route") == "/executors/{executor_uuid}/rent"
            and v.get("actor_key_id") == audit["api_key_id"]
            for v in controls
        )
        and any(
            v.get("executor_uuid") == action["launch_spec"]["offer_id"]
            for v in controls
        ),
        "handoff_account_audit_positive_controls_missing",
    )
    require(proof.get("intent_id") == intent["id"], "handoff_intent_identity_mismatch")


def prepare(repo, config, proof, *, apply=False):
    """Freeze lease ownership, reconcile one evidenced no-rent and keep jobs exact.

    Only the instance reservation can settle to zero. Job reservations, budget
    limits/spend, original window and the accepted request are never replaced.
    """
    require(
        config.service_mode == "on-demand"
        and config.allowed_owners == ["superdan", "supervan"],
        "handoff_on_demand_scope_required",
    )
    sequence = proof.get("sequence") if isinstance(proof, dict) else None
    require(
        type(sequence) is int and 1 <= sequence < config.max_cycles,
        "handoff_cycle_limit",
    )
    cycle = cycle_config(config, sequence)
    with repo.transaction() as conn:
        # Match normal scaler order: account -> global capacity -> leader/jobs.
        accounts = []
        for bid in sorted(config.budget_account_ids):
            account = repo._locked(
                conn, select(budget_accounts).where(budget_accounts.c.id == bid)
            )
            require(account is not None, "handoff_budget_missing")
            accounts.append(dict(account))
        repo._lock_capacity(conn)
        leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == config.pool)
        )
        require(leader is not None, "handoff_leader_missing")
        rows, pending, private_digests = snapshot(repo, config, conn)
        active = [r for r in rows if r["state"] != "destroyed"]
        require(
            len(active) == 1
            and active[0]["id"] == proof.get("intent_id")
            and active[0]["state"] == "creation_unknown"
            and active[0]["provider"] == "lium"
            and active[0]["provider_instance_id"] is None,
            "handoff_live_instance_requires_reconciliation",
        )
        intent = active[0]
        action = repo._locked(
            conn,
            select(scaler_actions).where(scaler_actions.c.intent_id == intent["id"]),
        )
        require(
            action is not None
            and action["launch_spec"] in config.launches
            and action["destroy_started_at"] is None,
            "handoff_action_identity_mismatch",
        )
        validate_proof(config, intent, action, proof, repo.clock())
        unknown = conn.execute(
            select(scaler_receipts.c.observed_at)
            .where(
                scaler_receipts.c.intent_id == intent["id"],
                scaler_receipts.c.operation == "create",
                scaler_receipts.c.facts["state"].as_string() == "unknown",
            )
            .order_by(scaler_receipts.c.observed_at)
        ).scalar()
        require(
            unknown is not None and unknown == proof["audit"]["first_unknown_at"],
            "handoff_first_create_observation_mismatch",
        )
        grant = repo._locked(
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
            grant is not None
            and link is not None
            and link["intent_id"] == intent["id"]
            and grant["payload"]["policy_hash"] == config.execution_policy_sha256,
            "handoff_capacity_identity_mismatch",
        )
        require(
            all(
                j["execution_plan"].get("capacity_approval_id")
                == cycle.capacity_approval_id
                for j in pending
            ),
            "handoff_backlog_grant_mismatch",
        )
        require(
            repo.clock() + 300 < config.stop_claiming_at,
            "handoff_original_window_expired",
        )
        evidence_hash = request_hash(proof)
        fenced_id = "handoff-" + evidence_hash[:24]
        result = {
            "version": 1,
            "phase": "ledger_fenced" if apply else "dry_run",
            "old_config_hash": config.fingerprint(),
            "cycle_id": config.cycle_id,
            "pool": config.pool,
            "created_at": config.created_at,
            "hard_deadline": config.hard_deadline,
            "previous_sequence": sequence,
            "next_sequence": sequence + 1,
            "previous_approval_id": cycle.capacity_approval_id,
            "intent_id": intent["id"],
            "evidence_sha256": evidence_hash,
            "fenced_leader_id": fenced_id,
            "fence": leader["fence"] + 1,
            **private_digests,
            "budget_limits_and_spend": {
                r["id"]: {k: r[k] for k in ("limit_microusd", "spent_microusd")}
                for r in accounts
            },
        }
        if apply:
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == config.pool)
                .values(
                    leader_id=fenced_id,
                    fence=result["fence"],
                    expires_at=config.hard_deadline,
                )
            )
            fact = asdict(
                ProviderFact(
                    "not_created", actual_cost_microusd=0, absence_confirmed=True
                )
            )
            audited_fact = {
                **fact,
                "handoff_verified": True,
                "old_config_hash": config.fingerprint(),
                "configuration_id": config.configuration_id,
                "model_id": action["launch_spec"]["model_id"],
                "launch_spec_sha256": request_hash(action["launch_spec"]),
                "evidence_sha256": evidence_hash,
            }
            conn.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=intent["id"],
                    operation="operator-audit",
                    observed_at=repo.clock(),
                    facts=audited_fact,
                )
            )
            repo.update_instance(
                intent["id"],
                "destroyed",
                destruction_confirmed=True,
                actual_cost_microusd=0,
                connection=conn,
            )
            conn.execute(
                update(capacity_approvals)
                .where(capacity_approvals.c.id == cycle.capacity_approval_id)
                .values(enabled=0)
            )
            conn.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == intent["id"])
                .values(last_observation=fact, last_observed_at=repo.clock())
            )
            require(
                snapshot(repo, config, conn)[2] == private_digests,
                "handoff_backlog_changed",
            )
        return result


def verify(repo, config, receipt, *, release_leader=False):
    """Validate the unchanged handoff ledger; release only after host retirement."""
    require(
        receipt.get("phase") == "ledger_fenced"
        and receipt.get("version") == 1
        and receipt.get("old_config_hash") == config.fingerprint()
        and receipt.get("created_at") == config.created_at
        and receipt.get("hard_deadline") == config.hard_deadline,
        "handoff_receipt_identity_mismatch",
    )
    with repo.transaction() as conn:
        for bid in sorted(config.budget_account_ids):
            row = repo._locked(
                conn, select(budget_accounts).where(budget_accounts.c.id == bid)
            )
            require(
                row
                and {k: row[k] for k in ("limit_microusd", "spent_microusd")}
                == receipt["budget_limits_and_spend"].get(bid),
                "handoff_budget_changed",
            )
        repo._lock_capacity(conn)
        leader = repo._locked(
            conn, select(scaler_leaders).where(scaler_leaders.c.pool == config.pool)
        )
        require(
            leader
            and leader["leader_id"] == receipt["fenced_leader_id"]
            and leader["fence"] == receipt["fence"],
            "handoff_leader_changed",
        )
        rows, _, digests = snapshot(repo, config, conn)
        require(
            all(r["state"] == "destroyed" for r in rows)
            and any(
                r["id"] == receipt["intent_id"] and r["provider_instance_id"] is None
                for r in rows
            ),
            "handoff_instances_changed",
        )
        require(
            digests["job_hashes"] == receipt["job_hashes"]
            and digests["job_reservations_sha256"]
            == receipt["job_reservations_sha256"],
            "handoff_backlog_changed",
        )
        grant = (
            conn.execute(
                select(capacity_approvals).where(
                    capacity_approvals.c.id == receipt["previous_approval_id"]
                )
            )
            .mappings()
            .first()
        )
        require(grant and grant["enabled"] == 0, "handoff_old_admission_open")
        if release_leader:
            require(
                receipt.get("host_retirement_confirmed") is True,
                "handoff_host_retirement_required",
            )
            conn.execute(
                update(scaler_leaders)
                .where(scaler_leaders.c.pool == config.pool)
                .values(expires_at=0)
            )
    return {
        "verified": True,
        "leader_released": release_leader,
        "job_ids": sorted(digests["job_hashes"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--action", choices=("prepare", "verify", "release-leader"), required=True
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    repo = None
    try:
        config = read_config(args.config)
        value = json.loads(sys.stdin.buffer.read(131073))
        require(
            isinstance(value, dict) and len(json.dumps(value)) <= 131072,
            "handoff_stdin_invalid",
        )
        repo = Repository(Settings.from_environment().database_url)
        if args.action == "prepare":
            result = prepare(repo, config, value, apply=args.apply)
        else:
            require(
                args.action != "release-leader" or args.apply,
                "handoff_explicit_apply_required",
            )
            result = verify(
                repo, config, value, release_leader=args.action == "release-leader"
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
                    "safe_error": "handoff_configuration_or_evidence_invalid",
                }
            )
        )
        return 1
    finally:
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
