"""Bounded provider statement inspection, not financial settlement.

The existing controller safety callback records facts in scaler_receipts. A
Vast charge snapshot has no final-invoice flag and cannot settle a reservation,
allocate a rental bill to H3 jobs, or authorize a provider mutation.
"""

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import time
import uuid

from sqlalchemy import insert, select, literal, or_, and_

from .repository import (
    budget_accounts,
    budget_reservations,
    instance_intents,
    scaler_receipts,
    spending_origins,
)

INTERVAL = 900
OPERATION = "provider_charges_inspected"
CHARGES_URL = "https://console.vast.ai/api/v0/charges/"


def read_vast_charges(read, start, end, *, monotonic=time.monotonic, timeout_budget=30):
    """Complete bounded same-account read; no top-up/payment endpoint."""
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 < start <= end
        or end - start > 31 * 86400
    ):
        raise ValueError("provider_charge_window_invalid")
    params = {
        "select_filters": json.dumps(
            {"day": {"gte": start, "lte": end}, "type": {"in": ["instance"]}}
        ),
        "format": "table",
        "limit": 500,
        "latest_first": "true",
    }
    deadline, seen, records, total = monotonic() + timeout_budget, set(), {}, None
    for _ in range(10):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ValueError("provider_charge_read_timeout")
        page = read(params, min(15, remaining))
        if monotonic() >= deadline:
            raise ValueError("provider_charge_read_timeout")
        if (
            not isinstance(page, dict)
            or page.get("success") is not True
            or not isinstance(page.get("results"), list)
            or type(page.get("count")) is not int
            or page["count"] != len(page["results"])
            or not 0 <= page["count"] <= 500
            or type(page.get("total")) is not int
            or not 0 <= page["total"] <= 5000
            or total is not None
            and page["total"] != total
            or "next_token" not in page
        ):
            raise ValueError("provider_charge_page_unconfirmed")
        total = page["total"]
        for row in page["results"]:
            source = row.get("source") if isinstance(row, dict) else None
            if (
                not isinstance(row, dict)
                or row.get("type") != "instance"
                or not isinstance(source, str)
                or not re.fullmatch(r"instance-[1-9][0-9]{0,17}", source)
                or source in records
                or type(row.get("start")) is not int
                or type(row.get("end")) is not int
                or not start <= row["start"] <= row["end"] <= end
                or type(row.get("amount")) not in (str, int, float)
            ):
                raise ValueError("provider_charge_record_unconfirmed")
            try:
                amount = Decimal(str(row["amount"])) * 1_000_000
                if (
                    not amount.is_finite()
                    or not 0 <= amount < 2**63
                    or amount != amount.to_integral_value()
                ):
                    raise ValueError("provider_charge_amount_unconfirmed")
            except InvalidOperation:
                raise ValueError("provider_charge_amount_unconfirmed") from None
            records[source] = {
                "reported_cost_microusd": int(amount),
                "period_start": row["start"],
                "period_end": row["end"],
            }
        token = page["next_token"]
        if token is None:
            if len(records) != total:
                raise ValueError("provider_charge_total_unconfirmed")
            normalized = {"window_start": start, "window_end": end, "records": records}
            digest = hashlib.sha256(
                json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return {
                **normalized,
                "statement_sha256": digest,
                "complete": True,
                "final_cost_confirmed": False,
            }
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,4096}", token)
            or token in seen
            or not page["results"]
            or len(records) >= total
        ):
            raise ValueError("provider_charge_cursor_unconfirmed")
        seen.add(token)
        params["after_token"] = token
    raise ValueError("provider_charge_page_limit")


def _pending(connection, tenant, *, owner=None):
    columns = [
        instance_intents.c.id,
        instance_intents.c.created_at,
        instance_intents.c.provider_instance_id,
        instance_intents.c.provider,
    ]
    if owner is None:
        from .operator_capacity import operator_nodes

        query = select(*columns, operator_nodes.c.payload).outerjoin(
            operator_nodes, operator_nodes.c.intent_id == instance_intents.c.id
        )
    else:
        query = select(*columns, literal(None).label("payload"))
    query = query.where(
        instance_intents.c.id.in_(
            select(budget_reservations.c.reference_id)
            .join(
                budget_accounts,
                budget_accounts.c.id == budget_reservations.c.account_id,
            )
            .outerjoin(
                spending_origins,
                and_(
                    spending_origins.c.reference_type == "instance",
                    spending_origins.c.reference_id
                    == budget_reservations.c.reference_id,
                ),
            )
            .where(
                budget_accounts.c.tenant_id == tenant,
                budget_reservations.c.reference_type == "instance",
                budget_reservations.c.state == "invoice_pending",
                *(
                    [
                        or_(
                            budget_accounts.c.owner_id == owner,
                            and_(
                                spending_origins.c.tenant_id == tenant,
                                spending_origins.c.owner_id == owner,
                            ),
                        )
                    ]
                    if owner is not None
                    else []
                ),
            )
        )
    )
    rows = list(
        connection.execute(query.order_by(instance_intents.c.id).limit(1001)).mappings()
    )
    if len(rows) > 1000:
        raise ValueError("provider_charge_scope_limit")
    return [{**r, "payload": r["payload"] or {}} for r in rows]


def _last(connection, ident):
    return (
        connection.execute(
            select(scaler_receipts)
            .where(
                scaler_receipts.c.intent_id == ident,
                scaler_receipts.c.operation == OPERATION,
            )
            .order_by(scaler_receipts.c.observed_at.desc(), scaler_receipts.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )


def inspect_pending_invoices(repo, tenant, client):
    """One check per original instance/15min, even across controller restarts.

    Only existing operational receipts are inserted. Old invoices never wake
    the fast demand loop; this function is called by the existing safety sweep.
    """
    now = repo.clock()
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise ValueError("provider_charge_clock_invalid")
    with repo.engine.connect() as connection:
        rows = _pending(connection, tenant)
        due, clock_unconfirmed = [], 0
        for row in rows:
            last = _last(connection, row["id"])
            if last is not None and now - last["observed_at"] < 0:
                clock_unconfirmed += 1
                continue
            if last is None or now - last["observed_at"] >= INTERVAL:
                due.append(dict(row))
    if not due:
        return {
            "state": "clock_unconfirmed" if clock_unconfirmed else "not_due",
            "checked": 0,
            "ledger_writes": 0,
            "provider_mutations": 0,
        }
    candidates = [
        r
        for r in due
        if r["provider"] == "vast"
        and r["payload"].get("capacity_backend") == "dstack-v1"
        and r["payload"].get("tenant_id") == tenant
        and r["payload"].get("dstack", {}).get("backend") == "vastai"
        and r["payload"]["dstack"].get("provider_instance_id")
        == r["provider_instance_id"]
        and isinstance(r["provider_instance_id"], str)
        and re.fullmatch(r"[1-9][0-9]{0,17}", r["provider_instance_id"])
        and type(r["created_at"]) in (int, float)
        and math.isfinite(r["created_at"])
        and 0 < r["created_at"] <= now
        and int(now) - int(r["created_at"]) // 86400 * 86400 <= 31 * 86400
    ]
    snapshot, code = None, "provider_charge_identity_or_backend_unavailable"
    if candidates:
        start = int(min(r["created_at"] for r in candidates)) // 86400 * 86400
        try:
            snapshot = client.vast_charges(start, int(now))
            code = "provider_statement_not_final"
        except Exception:
            # Provider errors may contain authorization/labels; publish only
            # this closed code. No zero cost or successful check is inferred.
            code = "provider_charge_read_unavailable"
    written = 0
    with repo.transaction() as connection:
        current = {r["id"]: r for r in _pending(connection, tenant)}
        for original in due:
            row = current.get(original["id"])
            if (
                row is None
                or row["provider_instance_id"] != original["provider_instance_id"]
                or row["payload"] != original["payload"]
                or row["provider"] != original["provider"]
            ):
                continue
            last = _last(connection, row["id"])
            if last is not None and now - last["observed_at"] < INTERVAL:
                continue
            item = (
                snapshot["records"].get("instance-" + row["provider_instance_id"])
                if snapshot and original in candidates
                else None
            )
            reason = (
                (
                    "provider_statement_not_final"
                    if item
                    else "provider_charge_record_missing"
                )
                if snapshot and original in candidates
                else code
                if original in candidates
                else "provider_charge_identity_or_backend_unavailable"
            )
            facts = {
                "schema_version": 1,
                "provider": "vastai" if original in candidates else "unknown",
                "provider_instance_id": row["provider_instance_id"],
                "reason_code": reason,
                "reported_cost_microusd": item["reported_cost_microusd"]
                if item
                else None,
                "complete": bool(item),
                "final_cost_confirmed": False,
                "statement_sha256": snapshot["statement_sha256"] if item else None,
                "window_start": snapshot["window_start"] if item else None,
                "window_end": snapshot["window_end"] if item else None,
            }
            ident = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"inkseq:charge-check:{tenant}:{row['id']}:{int(now) // INTERVAL}",
                )
            )
            if connection.execute(
                select(scaler_receipts.c.id).where(scaler_receipts.c.id == ident)
            ).first():
                continue
            connection.execute(
                insert(scaler_receipts).values(
                    id=ident,
                    intent_id=row["id"],
                    operation=OPERATION,
                    observed_at=now,
                    facts=facts,
                )
            )
            written += 1
    return {
        "state": "observed",
        "checked": written,
        "ledger_writes": 0,
        "provider_mutations": 0,
    }


def public_reconciliation(connection, tenant, owner, now):
    """Owner-scoped status only; a reported amount is never a settled cost."""
    rows = _pending(connection, tenant, owner=owner)
    checks = [_last(connection, row["id"]) for row in rows]
    future = any(r is not None and r["observed_at"] > now for r in checks)
    observed = [r for r in checks if r is not None and 0 <= now - r["observed_at"]]
    by_id = {r["id"]: r for r in rows}
    complete = [
        r
        for r in observed
        if r["facts"].get("complete") is True
        and r["facts"].get("provider_instance_id")
        == by_id[r["intent_id"]]["provider_instance_id"]
        and r["facts"].get("provider") == "vastai"
        and by_id[r["intent_id"]]["provider"] == "vast"
    ]
    identities = [
        (r["facts"]["provider"], r["facts"]["provider_instance_id"]) for r in complete
    ]
    duplicate = len(set(identities)) != len(identities)
    state = (
        "not_needed"
        if not rows
        else "unavailable"
        if future or duplicate
        else "not_checked"
        if not observed
        else "overdue"
        if any(now - r["observed_at"] >= 2 * INTERVAL for r in observed)
        else "unavailable"
        if len(complete) != len(rows)
        else "current"
    )
    return {
        "job": {
            "state": "allocation_required",
            "reason_code": "rental_cost_not_per_job_invoice",
        },
        "instance": {
            "state": state,
            "pending_count": len(rows),
            "checked_count": len(observed),
            "observed_at": min((r["observed_at"] for r in observed), default=None),
            "reported_cost_microusd": sum(
                r["facts"]["reported_cost_microusd"] for r in complete
            )
            if complete and not duplicate
            else None,
            "final_cost_confirmed": False,
            "check_interval_seconds": INTERVAL,
            "reason_code": "provider_charge_identity_conflict"
            if duplicate
            else "provider_charge_clock_unconfirmed"
            if future
            else "provider_statement_not_final"
            if state == "current"
            else "provider_charge_check_" + state,
        },
    }
