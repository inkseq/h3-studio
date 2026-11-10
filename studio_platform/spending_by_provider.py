"""Spend our own ledger recorded per provider, shown beside each balance (#966).

Read-only. Rentals count once per original machine even when owner, project
and tenant layers each reserve it; hosted per-job suppliers count once per job.
Settled amounts are the recorded actual cost. Open amounts are reservations
not yet settled, an upper bound rather than a bill. Native engine jobs are
already paid through their rental, so they are never counted twice.
"""

from collections import defaultdict

from sqlalchemy import select

from .credit_margin import exposure
from .providers.registry import (
    EXECUTION_BACKENDS,
    LEGACY_MODAL_PROVIDER,
    MODAL_DIALOUT,
)
from .repository import budget_reservations, instance_intents, jobs

WINDOW_DAYS = 30
# Ledger names that bill to a balance row with another name.
BALANCE_ROW = {MODAL_DIALOUT.provider: "modal", LEGACY_MODAL_PROVIDER: "modal"}
# Hosted adapters that incur their own per-job supplier spend.
PER_JOB = {
    backend.id: backend.provider
    for backend in EXECUTION_BACKENDS
    if backend.per_job_supplier
}
# Balance rows whose spend our ledger records; others show no ledger figure.
TRACKED = frozenset({"vast", "runpod", "lium", "cloudrift", "modal", "everyframe"})
_COLUMNS = (
    budget_reservations.c.reference_type,
    budget_reservations.c.reference_id,
    budget_reservations.c.amount_microusd,
    budget_reservations.c.state,
    budget_reservations.c.actual_cost_microusd,
)


def _window(statement, start, now):
    return statement.where(
        budget_reservations.c.created_at >= start,
        budget_reservations.c.created_at <= now,
    )


def ledger_spend(connection, now, days=WINDOW_DAYS):
    """Per balance row: settled and open microUSD for reservations in the window."""
    start = now - days * 86400
    grouped = defaultdict(list)
    owner = {}
    rentals = select(*_COLUMNS, instance_intents.c.provider).select_from(
        budget_reservations.join(
            instance_intents,
            (budget_reservations.c.reference_type == "instance")
            & (budget_reservations.c.reference_id == instance_intents.c.id),
        )
    )
    for row in connection.execute(_window(rentals, start, now)).mappings():
        key = ("instance", row["reference_id"])
        grouped[key].append(row)
        owner[key] = BALANCE_ROW.get(row["provider"], row["provider"])
    hosted = select(*_COLUMNS, jobs.c.execution_plan).select_from(
        budget_reservations.join(
            jobs,
            (budget_reservations.c.reference_type == "job")
            & (budget_reservations.c.reference_id == jobs.c.id),
        )
    )
    for row in connection.execute(_window(hosted, start, now)).mappings():
        plan = row["execution_plan"] if isinstance(row["execution_plan"], dict) else {}
        provider = PER_JOB.get(plan.get("backend"))
        if provider is None or plan.get("simulation") is True:
            continue
        key = ("job", row["reference_id"])
        grouped[key].append(row)
        owner[key] = provider
    totals = {}
    for key, rows in grouped.items():
        total = totals.setdefault(
            owner[key],
            {"settled_microusd": 0, "open_microusd": 0, "references": 0},
        )
        value = exposure(rows) or {}
        total["references"] += 1
        total["settled_microusd"] += value.get("actual_microusd", 0)
        total["open_microusd"] += sum(
            value.get(name, 0)
            for name in (
                "reserved_microusd",
                "invoice_pending_microusd",
                "unconfirmed_microusd",
            )
        )
    return {"window_days": days, "since": start, "providers": totals}


def with_ledger(stored, connection, now):
    """Add our recorded spend to each balance row; untracked rows get None."""
    spend = ledger_spend(connection, now)
    empty = {"settled_microusd": 0, "open_microusd": 0, "references": 0}
    names = set()
    for row in stored["rows"]:
        names.add(row["provider"])
        row["ledger"] = (
            spend["providers"].get(row["provider"], dict(empty))
            if row["provider"] in TRACKED
            else None
        )
    stored["ledger_window_days"] = spend["window_days"]
    stored["ledger_since"] = spend["since"]
    # A ledger provider without a balance row stays visible rather than dropped.
    stored["ledger_other"] = {
        name: value
        for name, value in sorted(spend["providers"].items())
        if name not in names
    }
    return stored
