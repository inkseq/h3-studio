"""Explicit reviewed financial reconciliation; imports and inspection have no I/O.

No provider operations, guessed invoices, rental retries or schema creation.
An invoice_pending reservation retains its amount for Settings/test projections,
but no longer occupies a legacy account's admission reservation counter.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import uuid
from pathlib import Path

from sqlalchemy import select, update

from .repository import (Repository, Scope, Conflict, attempts, budget_accounts,
    budget_reservations, instance_intents, jobs, outbox, registered_workers,
    request_hash, money, manually_reviewed_inactive)
from .operator_capacity import ACTIVE_COMMANDS, operator_commands, operator_nodes

TERMINAL = {"succeeded", "failed", "cancelled"}
SHA = re.compile(r"[0-9a-f]{64}\Z")


def require(ok, code):
    if not ok:
        raise Conflict(code)


def snapshot(repo, conn, tenant, kind, identity, *, reviewed=False):
    require(kind in {"job", "instance"}, "settlement_reference_invalid")
    table = jobs if kind == "job" else instance_intents
    row = repo._locked(conn, select(table).where(table.c.id == identity))
    require(row is not None, "settlement_reference_missing")
    rows = [dict(r) for r in conn.execute(select(budget_reservations).where(
        budget_reservations.c.reference_type == kind,
        budget_reservations.c.reference_id == identity).order_by(
            budget_reservations.c.account_id).with_for_update()).mappings()]
    require(bool(rows), "settlement_reservations_missing")
    accounts = []
    for r in rows:
        account = conn.execute(select(budget_accounts).where(budget_accounts.c.id == r["account_id"])).mappings().one()
        require(account["tenant_id"] == tenant, "settlement_tenant_mismatch")
        accounts.append(dict(account))
    value = {"reference": dict(row), "reservations": rows}
    if kind == "job":
        require(row["tenant_id"] == tenant, "settlement_tenant_mismatch")
        value["attempts"] = [dict(r) for r in conn.execute(select(attempts).where(
            attempts.c.job_id == identity).order_by(attempts.c.id).with_for_update()).mappings()]
    else:
        node = repo._locked(conn, select(operator_nodes).where(operator_nodes.c.intent_id == identity))
        require(node is not None, "settlement_node_scope_mismatch")
        legacy_review = manually_reviewed_inactive(conn, row) if reviewed else None
        scoped = node["payload"].get("tenant_id") == tenant
        # Old nodes predate tenant metadata. Only their already audited manual
        # review plus every ORIGINAL account's tenant can provide this fallback.
        require(scoped or (reviewed and node["payload"].get("tenant_id") is None
            and node["payload"].get("capacity_backend") is None and legacy_review
            and node["runtime_state"] == "manually_reviewed" and node["desired_state"] == "stopped"),
            "settlement_node_scope_mismatch")
        value["node"] = dict(node)
        value["workers"] = [dict(r) for r in conn.execute(select(registered_workers).where(
            registered_workers.c.provider == row["provider"],
            registered_workers.c.instance_id == row["provider_instance_id"]).order_by(registered_workers.c.id)
            .with_for_update()).mappings()]
        worker_ids = [w["id"] for w in value["workers"]]
        value["attempts"] = [dict(r) for r in conn.execute(select(attempts).where(
            attempts.c.worker_id.in_(worker_ids)).order_by(attempts.c.id).with_for_update()).mappings()] if worker_ids else []
        if reviewed:
            command = repo._locked(conn, select(operator_commands).where(operator_commands.c.id == node["command_id"]))
            require(command is not None and command["kind"] in {"start", "dstack_start"},
                "settlement_start_command_missing")
            siblings = [dict(r) for r in conn.execute(select(operator_nodes).where(
                operator_nodes.c.command_id == command["id"]).order_by(operator_nodes.c.intent_id)
                .with_for_update()).mappings()]
            sibling_intents = [dict(r) for r in conn.execute(select(instance_intents).where(
                instance_intents.c.id.in_([n["intent_id"] for n in siblings])).order_by(instance_intents.c.id)
                .with_for_update()).mappings()]
            job_ids = sorted({a["job_id"] for a in value["attempts"]})
            value.update(accounts=accounts, legacy_review=legacy_review, command=dict(command),
                siblings=siblings, sibling_intents=sibling_intents,
                linked_jobs=[dict(r) for r in conn.execute(select(jobs).where(jobs.c.id.in_(job_ids))
                    .order_by(jobs.c.id).with_for_update()).mappings()] if job_ids else [])
    return value


def inspect_reference(repo, tenant, kind, identity, *, version=1):
    require(type(version) is int and version in {1, 2} and (version == 1 or kind == "instance"),
        "settlement_request_invalid")
    with repo.transaction() as conn:
        value = snapshot(repo, conn, tenant, kind, identity, reviewed=version == 2)
        row = value["reference"]
        return {"reference_type": kind, "reference_id": identity,
            "expected_sha256": request_hash(value), "state": row.get("status", row.get("state")),
            "reservations": [{k: r[k] for k in ("account_id", "amount_microusd", "state", "actual_cost_microusd")}
                             for r in value["reservations"]]}


def closed_attempt(attempt):
    """Fenced pre-submit histories own no upstream task; submitted unknowns do."""
    return (attempt["status"] in TERMINAL and attempt["upstream_stopped"] == 1) or (
        attempt["status"] in {"lease_expired", "deferred"}
        and attempt["submission_started_at"] is None
        and attempt["upstream_task_id"] is None)


def validate_terminal(value, kind, proof, now):
    row = value["reference"]
    require(all(closed_attempt(a) for a in value["attempts"]),
            "settlement_upstream_unresolved")
    if kind == "job":
        require(row["status"] in TERMINAL, "settlement_job_not_terminal")
        require((row.get("current_attempt_id") is None and row["status"] == "cancelled")
                or any(a["id"] == row.get("current_attempt_id") for a in value["attempts"]),
                "settlement_current_attempt_missing")
        require(not row.get("lease_worker_id") and not row.get("lease_expires_at"), "settlement_job_lease_active")
        require(proof is None, "settlement_unexpected_provider_proof")
        return
    require(row["state"] in {"destroying", "creation_unknown", "destroyed"}, "settlement_instance_not_terminal")
    require(all(w["current_job_id"] is None for w in value["workers"]), "settlement_worker_busy")
    identity = row["provider_instance_id"]
    # Identity-less uncertain creation cannot be explained by one absent ID.
    require(bool(identity) and isinstance(proof, dict), "settlement_absence_unconfirmed")
    require(set(proof) == {"provider", "provider_instance_id", "complete", "instance_ids", "observed_at", "evidence_sha256"},
            "settlement_absence_invalid")
    ids, at = proof["instance_ids"], proof["observed_at"]
    require(proof["provider"] == row["provider"] and proof["provider_instance_id"] == identity
        and proof["complete"] is True and isinstance(ids, list) and len(ids) <= 4096
        and all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids) and identity not in ids
        and type(at) in (int, float) and math.isfinite(at) and 0 <= now-at <= 120
        and isinstance(proof["evidence_sha256"], str) and SHA.fullmatch(proof["evidence_sha256"]),
        "settlement_absence_unconfirmed")


def fresh(at, now):
    return type(at) in (int, float) and math.isfinite(at) and 0 <= now-at <= 120


def validate_reviewed_instance(value, proof, now, accounts, selected, excluded):
    """Private reviewed evidence, never an automatic absence/zero-cost rule.

    The operator verifies original provider-account provenance outside the DB
    transaction. Its allowlisted SHA and exact owned-run binding are part of
    the immutable request and audit; no provider client is called here.
    """
    row, node = value["reference"], value["node"]
    require(row["state"] in {"creation_unknown", "destroying", "destroyed"} or (
        row["state"] == "draining" and value["legacy_review"] is not None),
        "settlement_instance_not_terminal")
    require(all(closed_attempt(a) for a in value["attempts"]), "settlement_upstream_unresolved")
    require(all(w["current_job_id"] is None and w["state"] == "retired" for w in value["workers"])
        and all(j["status"] in TERMINAL and not j["lease_worker_id"] and not j["lease_expires_at"]
                for j in value["linked_jobs"]), "settlement_worker_busy")
    require(isinstance(proof, dict) and set(proof) == {"kind", "provider", "provider_instance_id",
        "account_binding_sha256", "complete", "instance_ids", "observed_at", "evidence_sha256", "owned_run"},
        "settlement_absence_invalid")
    require(proof["kind"] == "reviewed_account_empty" and proof["provider"] == row["provider"]
        and proof["provider_instance_id"] == row["provider_instance_id"] and proof["complete"] is True
        and proof["instance_ids"] == [] and fresh(proof["observed_at"], now)
        and row["provider"] in accounts and isinstance(proof["account_binding_sha256"], str)
        and proof["account_binding_sha256"] == accounts[row["provider"]]
        and isinstance(proof["evidence_sha256"], str) and SHA.fullmatch(proof["evidence_sha256"]),
        "settlement_absence_unconfirmed")
    require(row["provider_instance_id"] not in excluded, "settlement_protected_instance")
    # Never finish a multi-node start command while an omitted sibling can
    # still own capacity. Every unfinished sibling must pass this same batch.
    require(all(s["state"] == "destroyed" or s["id"] in selected for s in value["sibling_intents"]),
        "settlement_command_sibling_unresolved")
    require(all(s["provider_instance_id"] not in excluded for s in value["sibling_intents"]),
        "settlement_protected_instance")
    if node["payload"].get("capacity_backend") == "dstack-v1":
        binding, run = node["payload"].get("dstack"), proof["owned_run"]
        require(isinstance(binding, dict) and isinstance(run, dict) and set(run) == {
            "run_id", "run_name", "binding_sha256", "original_binding_matched", "status", "observed_at", "evidence_sha256"},
            "settlement_owned_run_unconfirmed")
        require(binding.get("backend") == "vastai" and row["provider"] == "vast"
            and isinstance(binding.get("run_id"), str) and binding["run_id"]
            and binding.get("intent_id") == row["id"]
            and binding.get("provider_instance_id") not in excluded
            and run["run_id"] == binding["run_id"] and run["run_name"] == binding.get("run_name")
            and run["binding_sha256"] == request_hash(binding) and run["original_binding_matched"] is True
            and run["status"] in {"failed", "terminated", "aborted"} and fresh(run["observed_at"], now)
            and isinstance(run["evidence_sha256"], str) and SHA.fullmatch(run["evidence_sha256"]),
            "settlement_owned_run_unconfirmed")
    else:
        require(row["provider"] in {"lium", "targon"} and value["legacy_review"] is not None
            and node["desired_state"] == "stopped" and node["runtime_state"] == "manually_reviewed"
            and proof["owned_run"] is None, "settlement_legacy_review_required")


def reviewed_instance_fenced(conn, intent, node):
    """Only a committed original recovery audit can fence later observations."""
    fence = node["payload"].get("settlement_recovery", {})
    if not (intent["state"] == "destroyed" and isinstance(fence, dict) and fence.get("version") == 2
            and fence.get("intent_id") == intent["id"] and fence.get("command_id") == node["command_id"]
            and node["desired_state"] == "stopped" and node["runtime_state"] == "stopped"):
        return False
    audit = conn.execute(select(outbox.c.payload).where(outbox.c.event_type == "budget.recovery_completed",
        outbox.c.aggregate_id == fence.get("operation_id"))).scalar()
    return (isinstance(audit, dict) and audit.get("state") == "applied" and audit.get("source") == fence.get("source")
        and any(entry.get("reference_type") == "instance" and entry.get("reference_id") == intent["id"]
            and entry.get("actual_cost_microusd") is None and entry.get("billing_status") == "pending"
            and entry.get("evidence_sha256") == fence.get("evidence_sha256")
            and entry.get("capacity_fence", {}).get("command_id") == node["command_id"]
            for entry in audit.get("entries", [])))


def fence_reviewed_instance(repo, conn, value, request):
    """Close original capacity journals atomically with releasing their hold."""
    row, node = value["reference"], value["node"]
    if row["state"] == "draining":
        repo.update_instance(row["id"], "destroying", connection=conn)
    repo.update_instance(row["id"], "destroyed", destruction_confirmed=True, connection=conn)
    proof = {"version": 2, "intent_id": row["id"], "command_id": node["command_id"],
        "operation_id": request["operation_id"], "source": request["source"],
        "evidence_sha256": request["evidence_sha256"], "billing_status": "pending", "actual_cost_microusd": None}
    conn.execute(update(operator_nodes).where(operator_nodes.c.intent_id == row["id"]).values(
        desired_state="stopped", runtime_state="stopped", updated_at=repo.clock(),
        payload={**node["payload"], "settlement_recovery": proof}))
    if value["command"]["state"] in ACTIVE_COMMANDS:
        conn.execute(update(operator_commands).where(operator_commands.c.id == node["command_id"]).values(
            state="completed", reason_code="settlement_ended_capacity_reviewed", updated_at=repo.clock()))


def release_pending_invoice(repo, conn, kind, identity):
    """Internal terminal-path transition; never forgives an invoice."""
    rows = list(conn.execute(select(budget_reservations).where(
        budget_reservations.c.reference_type == kind, budget_reservations.c.reference_id == identity)
        .order_by(budget_reservations.c.account_id).with_for_update()).mappings())
    released = []
    for r in rows:
        if r["state"] != "reserved":
            continue
        require(r["actual_cost_microusd"] is None, "settlement_reservation_invalid")
        repo._locked(conn, select(budget_accounts).where(budget_accounts.c.id == r["account_id"]))
        conn.execute(update(budget_accounts).where(budget_accounts.c.id == r["account_id"]).values(
            reserved_microusd=budget_accounts.c.reserved_microusd-r["amount_microusd"]))
        conn.execute(update(budget_reservations).where(budget_reservations.c.id == r["id"]).values(state="invoice_pending"))
        released.append({"reservation_id": r["id"], "account_id": r["account_id"], "amount_microusd": r["amount_microusd"]})
    if released:
        repo._emit(conn, "budget.terminal_hold_released", identity,
            {"reference_type": kind, "reference_id": identity, "billing_status": "pending",
             "actual_cost_microusd": None, "reservations": released})


def release_terminal_job(repo, conn, job_id):
    row = repo._job(conn, job_id, lock=True)
    if row["status"] not in TERMINAL:
        return
    history = list(conn.execute(select(attempts).where(attempts.c.job_id == job_id).with_for_update()).mappings())
    if any(not closed_attempt(a) for a in history):
        return  # A historical uncertain attempt still owns its original hold.
    release_pending_invoice(repo, conn, "job", job_id)


def release_removed_instance(repo, conn, identity):
    row = repo._locked(conn, select(instance_intents).where(instance_intents.c.id == identity))
    if row is None or row["state"] != "destroyed" or not row["provider_instance_id"]:
        return
    workers = list(conn.execute(select(registered_workers).where(
        registered_workers.c.provider == row["provider"],
        registered_workers.c.instance_id == row["provider_instance_id"]).with_for_update()).mappings())
    if any(w["current_job_id"] is not None for w in workers):
        return
    ids = [w["id"] for w in workers]
    history = list(conn.execute(select(attempts).where(attempts.c.worker_id.in_(ids)).with_for_update()).mappings()) if ids else []
    if any(not closed_attempt(a) for a in history):
        return
    release_pending_invoice(repo, conn, "instance", identity)


def reconcile(repo, request, *, apply=False):
    """One audited transaction, exact replay returns its original receipt.

    The operator must independently verify the evidence SHA and provider-account
    provenance before supplying this private request. This is not a public API.
    """
    fields = {"version", "operation_id", "tenant_id", "actor", "source", "evidence_sha256", "entries", "excluded_provider_instance_ids"}
    require(type(apply) is bool and isinstance(request, dict) and type(request.get("version")) is int
        and request["version"] in {1, 2}, "settlement_request_invalid")
    reviewed = request["version"] == 2
    require(set(request) == fields | ({"reviewed_provider_accounts"} if reviewed else set()),
        "settlement_request_invalid")
    accounts = request.get("reviewed_provider_accounts", {})
    if reviewed:
        require(isinstance(accounts, dict) and bool(accounts) and set(accounts) <= {"vast", "lium", "targon"}
            and all(isinstance(v, str) and SHA.fullmatch(v) for v in accounts.values()),
            "settlement_provider_account_invalid")
    require(str(uuid.UUID(request["operation_id"])) == request["operation_id"], "settlement_operation_invalid")
    require(all(isinstance(request[k], str) and 0 < len(request[k]) <= 200 for k in ("tenant_id", "actor")), "settlement_actor_invalid")
    require(isinstance(request["source"], str) and re.fullmatch(r"https://github.com/inkseq/h3-studio/issues/[0-9]+(?:#issuecomment-[0-9]+)?", request["source"]), "settlement_source_invalid")
    require(isinstance(request["evidence_sha256"], str) and SHA.fullmatch(request["evidence_sha256"]), "settlement_evidence_invalid")
    excluded = request["excluded_provider_instance_ids"]
    require(isinstance(excluded, list) and all(isinstance(i, str) and i for i in excluded)
            and len(set(excluded)) == len(excluded), "settlement_exclusions_invalid")
    entries = request["entries"]
    require(isinstance(entries, list) and 1 <= len(entries) <= 500, "settlement_entries_invalid")
    require(all(isinstance(e, dict) and set(e) == {"reference_type", "reference_id", "expected_sha256", "actual_cost_microusd", "provider_absence"} for e in entries), "settlement_entries_invalid")
    require(len({(e["reference_type"], e["reference_id"]) for e in entries}) == len(entries), "settlement_duplicate_reference")
    if reviewed:
        require(all(e["reference_type"] == "instance" and e["actual_cost_microusd"] is None for e in entries),
            "settlement_reviewed_invoice_must_remain_unknown")
    selected = {e["reference_id"] for e in entries}
    def validate(value, entry):
        if reviewed:
            validate_reviewed_instance(value, entry["provider_absence"], repo.clock(), accounts, selected, excluded)
        else:
            validate_terminal(value, entry["reference_type"], entry["provider_absence"], repo.clock())
    digest = request_hash(request)
    with repo.transaction() as conn:
        # Serialize with capacity reconciliation and other recovery batches.
        repo._lock_capacity(conn)
        prior = conn.execute(select(outbox).where(outbox.c.event_type == "budget.recovery_completed",
            outbox.c.aggregate_id == request["operation_id"])).mappings().first()
        if prior:
            require(prior["payload"]["request_hash"] == digest, "settlement_operation_conflict")
            return prior["payload"]
        checked = []
        for e in sorted(entries, key=lambda e: (e["reference_type"], e["reference_id"])):
            value = snapshot(repo, conn, request["tenant_id"], e["reference_type"], e["reference_id"], reviewed=reviewed)
            require(request_hash(value) == e["expected_sha256"], "settlement_snapshot_changed")
            if e["reference_type"] == "instance":
                require(value["reference"]["provider_instance_id"] not in excluded, "settlement_protected_instance")
            validate(value, e)
            if e["actual_cost_microusd"] is not None:
                money(e["actual_cost_microusd"])
            require(all(r["state"] in {"reserved", "invoice_pending"} and r["actual_cost_microusd"] is None for r in value["reservations"]), "settlement_already_accounted")
            checked.append((e, value))
        # Match ordinary settlement's stable account order across the WHOLE
        # batch, not just within each reference.
        for account_id in sorted({r["account_id"] for _, value in checked for r in value["reservations"]}):
            account = repo._locked(conn, select(budget_accounts).where(budget_accounts.c.id == account_id))
            if reviewed:
                releasing = sum(r["amount_microusd"] for _, value in checked for r in value["reservations"]
                    if r["account_id"] == account_id and r["state"] == "reserved")
                require(account["reserved_microusd"] >= releasing, "settlement_counter_underflow")
        # All contended row/account locks have been acquired. Recheck freshness
        # now; a slow batch must not turn an expired inventory into authority.
        for e, value in checked:
            if reviewed:
                # V2 also fences the original command, sibling journals and
                # account counters. Re-read after all account locks, before
                # any mutation, to reject intervening ledger/account changes.
                require(request_hash(snapshot(repo, conn, request["tenant_id"], e["reference_type"],
                    e["reference_id"], reviewed=True)) == e["expected_sha256"], "settlement_snapshot_changed")
            validate(value, e)
        results = []
        for e, value in checked:
            kind, identity, cost = e["reference_type"], e["reference_id"], e["actual_cost_microusd"]
            before = [{k:r[k] for k in ("id", "account_id", "amount_microusd", "state")} for r in value["reservations"]]
            if apply:
                if kind == "instance":
                    if reviewed:
                        fence_reviewed_instance(repo, conn, value, request)
                    else:
                        repo.update_instance(identity, "destroyed", destruction_confirmed=True, connection=conn)
                if cost is not None:
                    if kind == "job":
                        row = value["reference"]
                        repo.settle_completed_job(Scope(row["tenant_id"], row["owner_id"], row["project_id"]), identity,
                            actual_cost_microusd=cost, connection=conn)
                    else:
                        repo.settle_instance_cost(identity, actual_cost_microusd=cost, connection=conn)
                else:
                    release_pending_invoice(repo, conn, kind, identity)
            results.append({"reference_type": kind, "reference_id": identity, "before": before,
                "billing_status": "pending" if cost is None else "settled", "actual_cost_microusd": cost,
                "evidence_sha256": request["evidence_sha256"]})
            if reviewed:
                results[-1]["capacity_fence"] = {"command_id": value["command"]["id"],
                    "original_command_state": value["command"]["state"],
                    "original_intent_state": value["reference"]["state"],
                    "original_runtime_state": value["node"]["runtime_state"],
                    "desired_state": "stopped", "runtime_state": "stopped"}
        receipt = {"operation_id": request["operation_id"], "request_hash": digest, "actor": request["actor"],
            "source": request["source"], "state": "applied" if apply else "dry_run", "entries": results}
        if apply:
            repo._emit(conn, "budget.recovery_completed", request["operation_id"], receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed exact request; default is read-only dry run")
    args = parser.parse_args(argv)
    from .settings import database_url_from_environment
    repo = Repository(database_url_from_environment())
    try:
        print(json.dumps(reconcile(repo, json.loads(args.request.read_text(encoding="utf-8")), apply=args.apply), sort_keys=True))
    finally:
        repo.close()


if __name__ == "__main__":
    main()
