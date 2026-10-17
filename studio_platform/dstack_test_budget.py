"""Opt-in staging test limits projected from the original reservation ledger.

No counters are reset and no daily ledger is created. Unknown supplier costs
remain charged conservatively on every day until the ordinary settlement path
confirms them. Layered owner/shared reservations are counted only once.
"""
from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .repository import BudgetExceeded, budget_reservations, instance_intents, scaler_actions

POOLS = frozenset({"inkseq-staging-v6-fl", "inkseq-staging-v6-ref"})
PROJECTS = frozenset({"project-inkseq-staging-superdan", "project-inkseq-staging-supervan"})
ACCOUNT = "inkseq-staging-gpu-shared"


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_guard(value):
    fields = {"version", "timezone", "daily_limit_microusd", "budget_account_id",
              "max_runtime_seconds", "stop_margin_seconds"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["version"]) is not int or value["version"] != 1
            or value["timezone"] != "Australia/Sydney" or value["budget_account_id"] != ACCOUNT
            or type(value["daily_limit_microusd"]) is not int or not 1 <= value["daily_limit_microusd"] <= 20_000_000
            or type(value["max_runtime_seconds"]) is not int or not 600 <= value["max_runtime_seconds"] <= 7200
            or type(value["stop_margin_seconds"]) is not int or not 300 <= value["stop_margin_seconds"] <= 600):
        raise ValueError("dstack_test_guard_invalid")
    return value


def day_window(now):
    if not finite(now):
        raise ValueError("dstack_test_clock_invalid")
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(ZoneInfo("Australia/Sydney"))
    start = datetime.combine(local.date(), time(), local.tzinfo)
    end = datetime.combine(local.date() + timedelta(days=1), time(), local.tzinfo)
    return start.timestamp(), end.timestamp()


def node_guard(intent, node):
    payload = node.get("payload", {})
    guard = payload.get("lifecycle", {}).get("test_gpu_guard")
    if guard is None:
        return None
    validate_guard(guard)
    if (intent.get("pool") not in POOLS or payload.get("project_id") not in PROJECTS
            or payload.get("capacity_backend") != "dstack-v1"
            or ACCOUNT not in payload.get("lifecycle", {}).get("budget_account_ids", [])
            or payload.get("request", {}).get("test_max_duration_s") != guard["max_runtime_seconds"]):
        raise ValueError("dstack_test_guard_binding_invalid")
    return guard


def runtime_reason(intent, node, now, *, expected_runtime_s=0):
    try:
        guard = node_guard(intent, node)
        if guard is None:
            return None
        if not finite(now) or not finite(expected_runtime_s) or expected_runtime_s < 0:
            return "dstack_test_guard_unconfirmed"
        # Intent creation precedes allocation/model loading. The original
        # controller starts draining early; native max_duration is the fallback
        # for an active/unknown task or an interrupted CPU controller.
        started = intent.get("created_at")
        if not finite(started) or not 0 <= now-started:
            return "dstack_test_guard_unconfirmed"
        if now-started + expected_runtime_s + guard["stop_margin_seconds"] >= guard["max_runtime_seconds"]:
            return "dstack_test_runtime_limit_reached"
        return None
    except (ValueError, TypeError, KeyError):
        return "dstack_test_guard_unconfirmed"


def projected_cost(connection, guard, now, *, replacement_id=None, minimum_reserved=0):
    """Today's full settled bills plus all unresolved original instance holds.

    An allocation spanning midnight is deliberately overcounted, rather than
    apportioned without an invoice. Open runs additionally cover elapsed time
    at their approved maximum rate plus shutdown grace and transport allowance.
    """
    from .operator_capacity import operator_nodes
    validate_guard(guard)
    start, _ = day_window(now)
    statement = select(budget_reservations, instance_intents.c.pool,
        instance_intents.c.state.label("instance_state"), instance_intents.c.created_at.label("intent_created_at"),
        instance_intents.c.updated_at.label("intent_updated_at"),
        operator_nodes.c.payload, scaler_actions.c.create_started_at).select_from(
            budget_reservations.outerjoin(instance_intents,
                budget_reservations.c.reference_id == instance_intents.c.id).outerjoin(operator_nodes,
                operator_nodes.c.intent_id == instance_intents.c.id).outerjoin(scaler_actions,
                scaler_actions.c.intent_id == instance_intents.c.id)).where(
                    budget_reservations.c.account_id == ACCOUNT)
    total = 0
    for row in connection.execute(statement).mappings():
        if (row["reference_type"] != "instance" or row["pool"] not in POOLS
                or not isinstance(row["payload"], dict) or row["payload"].get("project_id") not in PROJECTS
                or row["payload"].get("capacity_backend") != "dstack-v1"):
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        amount, actual = row["amount_microusd"], row["actual_cost_microusd"]
        if type(amount) is not int or amount < 0 or not finite(row["created_at"]) or row["created_at"] > now:
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        if row["state"] in {"settled", "released"}:
            settled = row["settled_at"]
            if type(actual) is not int or actual < 0 or not finite(settled) or settled > now:
                raise BudgetExceeded("dstack_test_budget_unconfirmed")
            if row["created_at"] >= start or settled >= start:
                total += actual
            continue
        if row["state"] not in {"reserved", "invoice_pending"} or actual is not None:
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        payload = row["payload"]
        hourly, extra = payload.get("hourly_cost_microusd"), payload.get("extra_reservation_microusd", 0)
        if type(hourly) is not int or hourly <= 0 or type(extra) is not int or extra < 0:
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        started = row["create_started_at"]
        started = row["intent_created_at"] if started is None else started
        if not finite(started) or not 0 <= now-started:
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        binding = payload.get("dstack", {})
        # A native run can stop before the supplier stops billing its VM.
        # Positive provider absence bounds that later interval conservatively.
        ended = (binding.get("provider_removal_proof", {}).get("observed_at", row["intent_updated_at"])
                 if row["instance_state"] == "destroyed" else None)
        through = ended if finite(ended) and started <= ended <= now else now + guard["stop_margin_seconds"]
        amount = max(amount, math.ceil(hourly * (through-started) / 3600) + extra)
        observed = binding.get("estimated_cost_microusd")
        if type(observed) is int and observed >= 0:
            amount = max(amount, observed + extra)
        if row["reference_id"] == replacement_id:
            amount = max(amount, minimum_reserved)
        total += amount
    return total


def budget_reason(connection, intent, node, now, *, minimum_reserved=0, expected_runtime_s=0):
    reason = runtime_reason(intent, node, now, expected_runtime_s=expected_runtime_s)
    if reason:
        return reason
    try:
        guard = node_guard(intent, node)
        if guard is not None and projected_cost(connection, guard, now, replacement_id=intent["id"],
                minimum_reserved=minimum_reserved) >= guard["daily_limit_microusd"]:
            return "dstack_test_daily_limit_reached"
        return None
    except (ValueError, TypeError, KeyError, BudgetExceeded):
        return "dstack_test_budget_unconfirmed"
