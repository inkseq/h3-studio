"""Admin-only rolling cost exposure; never an admission limit or invoice."""

from collections import defaultdict
from fractions import Fraction
import math

from sqlalchemy import select

from .credit_schema import entries, prices
from .repository import budget_reservations, jobs


def _money(value):
    return type(value) is int and value >= 0


def exposure(rows):
    """One original job, even when owner/shared layers repeat its cost."""
    if not rows:
        # A quote, including an included CPU estimate of zero, is not an
        # invoice or authoritative actual-cost record.
        return None
    if any(not _money(row["amount_microusd"]) for row in rows):
        return None
    actuals = [row["actual_cost_microusd"] for row in rows]
    states = {row["state"] for row in rows}
    amount = max(row["amount_microusd"] for row in rows)
    if (
        states <= {"settled", "released"}
        and all(_money(value) for value in actuals)
        and len(set(actuals)) == 1
    ):
        return {"actual_microusd": actuals[0]}
    if all(value is None for value in actuals):
        if amount == 0:
            return None
        if states == {"reserved"}:
            return {"reserved_microusd": amount}
        if states == {"invoice_pending"}:
            return {"invoice_pending_microusd": amount}
    if any(value is not None and not _money(value) for value in actuals):
        return None
    return {
        "unconfirmed_microusd": max(
            [amount, *[value for value in actuals if value is not None]]
        )
    }


def report(connection, tenant, now):
    """Capture-revision nominal value versus original-job provider exposure.

    Cohort is jobs.created_at in [now-30d,now), NOT capture or invoice time.
    Captures are gross nominal credits (no claim of receipts after tax/refund).
    Late invoices update this same cohort; no reservation layers are summed.
    """
    start = now - 30 * 86400
    cohort = []
    for row in connection.execute(
        select(jobs).where(
            jobs.c.tenant_id == tenant,
            jobs.c.created_at >= start,
            jobs.c.created_at < now,
        )
    ).mappings():
        quote = row["execution_plan"].get("credit_quote")
        if isinstance(quote, dict) and quote.get("enforced") is True:
            cohort.append(row)
    identifiers = {row["id"] for row in cohort}
    captures = list(
        connection.execute(
            select(entries)
            .join(
                jobs,
                (entries.c.reference_type == "job")
                & (entries.c.reference_id == jobs.c.id),
            )
            .where(
                entries.c.tenant == tenant,
                jobs.c.tenant_id == tenant,
                jobs.c.created_at >= start,
                jobs.c.created_at < now,
                entries.c.kind == "capture",
            )
        ).mappings()
    )
    configurations = {
        row["revision"]: row["configuration"]
        for row in connection.execute(
            select(prices.c.revision, prices.c.configuration).where(
                prices.c.tenant == tenant
            )
        ).mappings()
    }
    captured, value, valuation_known = 0, Fraction(0), True
    revisions = set()
    for row in captures:
        if row["reference_id"] not in identifiers:
            continue
        amount = -row["amount"]
        captured += amount
        revisions.add(row["price_revision"])
        config = configurations.get(row["price_revision"])
        try:
            unit = Fraction(str(config["credit_usd"]))
            if not math.isfinite(float(unit)) or unit <= 0 or not _money(amount):
                raise ValueError
        except (TypeError, KeyError, ValueError, ZeroDivisionError, OverflowError):
            valuation_known = False
            continue
        value += amount * unit * 1_000_000
    grouped = defaultdict(list)
    for row in connection.execute(
        select(budget_reservations)
        .join(
            jobs,
            (budget_reservations.c.reference_type == "job")
            & (budget_reservations.c.reference_id == jobs.c.id),
        )
        .where(
            jobs.c.tenant_id == tenant,
            jobs.c.created_at >= start,
            jobs.c.created_at < now,
        )
    ).mappings():
        if row["reference_id"] in identifiers:
            grouped[row["reference_id"]].append(row)
    costs = dict(
        actual_microusd=0,
        reserved_microusd=0,
        invoice_pending_microusd=0,
        unconfirmed_microusd=0,
    )
    unknown = 0
    for job in cohort:
        part = exposure(grouped[job["id"]])
        if part is None:
            unknown += 1
        else:
            for key, amount in part.items():
                costs[key] += amount
    total = sum(costs.values()) if not unknown else None
    margin = (
        (value - total) / value
        if valuation_known
        and value > 0
        and total is not None
        and total > 0
        and not costs["unconfirmed_microusd"]
        else None
    )
    return {
        "window_start": start,
        "window_end": now,
        "window_basis": "job.created_at",
        "value_basis": "gross_captured_credits_at_immutable_capture_price_revision",
        "job_count": len(cohort),
        "captured_credits": captured,
        "price_revisions": sorted(
            revision for revision in revisions if revision is not None
        ),
        "captured_value_microusd": float(value) if valuation_known else None,
        **costs,
        "actual_microusd": None if unknown else costs["actual_microusd"],
        "exposure_microusd": total,
        "unknown_cost_jobs": unknown,
        "margin": float(margin) if margin is not None else None,
        "below_30_percent": margin < Fraction(3, 10) if margin is not None else None,
        "diagnostic_only": True,
        "status": "unknown"
        if not valuation_known or unknown or costs["unconfirmed_microusd"]
        else "undefined"
        if margin is None
        else "estimated_pending"
        if costs["reserved_microusd"] or costs["invoice_pending_microusd"]
        else "actual_cost",
    }
