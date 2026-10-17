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
    request_hash, money)
from .operator_capacity import operator_nodes

TERMINAL = {"succeeded", "failed", "cancelled"}
SHA = re.compile(r"[0-9a-f]{64}\Z")


def require(ok, code):
    if not ok:
        raise Conflict(code)


def snapshot(repo, conn, tenant, kind, identity):
    require(kind in {"job", "instance"}, "settlement_reference_invalid")
    table = jobs if kind == "job" else instance_intents
    row = repo._locked(conn, select(table).where(table.c.id == identity))
    require(row is not None, "settlement_reference_missing")
    rows = [dict(r) for r in conn.execute(select(budget_reservations).where(
        budget_reservations.c.reference_type == kind,
        budget_reservations.c.reference_id == identity).order_by(
            budget_reservations.c.account_id).with_for_update()).mappings()]
    require(bool(rows), "settlement_reservations_missing")
    for r in rows:
        account = conn.execute(select(budget_accounts).where(budget_accounts.c.id == r["account_id"])).mappings().one()
        require(account["tenant_id"] == tenant, "settlement_tenant_mismatch")
    value = {"reference": dict(row), "reservations": rows}
    if kind == "job":
        require(row["tenant_id"] == tenant, "settlement_tenant_mismatch")
        value["attempts"] = [dict(r) for r in conn.execute(select(attempts).where(
            attempts.c.job_id == identity).order_by(attempts.c.id).with_for_update()).mappings()]
    else:
        node = repo._locked(conn, select(operator_nodes).where(operator_nodes.c.intent_id == identity))
        require(node is not None and node["payload"].get("tenant_id") == tenant,
                "settlement_node_scope_mismatch")
        value["node"] = dict(node)
        value["workers"] = [dict(r) for r in conn.execute(select(registered_workers).where(
            registered_workers.c.provider == row["provider"],
            registered_workers.c.instance_id == row["provider_instance_id"]).order_by(registered_workers.c.id)
            .with_for_update()).mappings()]
        worker_ids = [w["id"] for w in value["workers"]]
        value["attempts"] = [dict(r) for r in conn.execute(select(attempts).where(
            attempts.c.worker_id.in_(worker_ids)).order_by(attempts.c.id).with_for_update()).mappings()] if worker_ids else []
    return value


def inspect_reference(repo, tenant, kind, identity):
    with repo.transaction() as conn:
        value = snapshot(repo, conn, tenant, kind, identity)
        row = value["reference"]
        return {"reference_type": kind, "reference_id": identity,
            "expected_sha256": request_hash(value), "state": row.get("status", row.get("state")),
            "reservations": [{k: r[k] for k in ("account_id", "amount_microusd", "state", "actual_cost_microusd")}
                             for r in value["reservations"]]}


def validate_terminal(value, kind, proof, now):
    row = value["reference"]
    require(all(a["status"] in TERMINAL and a["upstream_stopped"] == 1 for a in value["attempts"]),
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
    if any(a["status"] not in TERMINAL or a["upstream_stopped"] != 1 for a in history):
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
    if any(a["status"] not in TERMINAL or a["upstream_stopped"] != 1 for a in history):
        return
    release_pending_invoice(repo, conn, "instance", identity)


def reconcile(repo, request, *, apply=False):
    """One audited transaction, exact replay returns its original receipt.

    The operator must independently verify the evidence SHA and provider-account
    provenance before supplying this private request. This is not a public API.
    """
    require(type(apply) is bool and isinstance(request, dict) and set(request) == {
        "version", "operation_id", "tenant_id", "actor", "source", "evidence_sha256", "entries", "excluded_provider_instance_ids"}, "settlement_request_invalid")
    require(type(request["version"]) is int and request["version"] == 1, "settlement_request_invalid")
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
            value = snapshot(repo, conn, request["tenant_id"], e["reference_type"], e["reference_id"])
            require(request_hash(value) == e["expected_sha256"], "settlement_snapshot_changed")
            if e["reference_type"] == "instance":
                require(value["reference"]["provider_instance_id"] not in excluded, "settlement_protected_instance")
            validate_terminal(value, e["reference_type"], e["provider_absence"], repo.clock())
            if e["actual_cost_microusd"] is not None:
                money(e["actual_cost_microusd"])
            require(all(r["state"] in {"reserved", "invoice_pending"} and r["actual_cost_microusd"] is None for r in value["reservations"]), "settlement_already_accounted")
            checked.append((e, value))
        # Match ordinary settlement's stable account order across the WHOLE
        # batch, not just within each reference.
        for account_id in sorted({r["account_id"] for _, value in checked for r in value["reservations"]}):
            repo._locked(conn, select(budget_accounts).where(budget_accounts.c.id == account_id))
        # All contended row/account locks have been acquired. Recheck freshness
        # now; a slow batch must not turn an expired inventory into authority.
        for e, value in checked:
            validate_terminal(value, e["reference_type"], e["provider_absence"], repo.clock())
        results = []
        for e, value in checked:
            kind, identity, cost = e["reference_type"], e["reference_id"], e["actual_cost_microusd"]
            before = [{k:r[k] for k in ("id", "account_id", "amount_microusd", "state")} for r in value["reservations"]]
            if apply:
                if kind == "instance":
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
