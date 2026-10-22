"""Supplier rental exposure for the adopted tenant admission policy.

The original rental ledger is the money authority. Account layers and customer
job prices are not additional supplier bills. This reader never settles costs;
uncertain attribution remains an explicit blocker for the next paid admission.
"""

from collections import defaultdict

from sqlalchemy import select

from .capacity_settings import admission_snapshot, read_policy
from .dstack_test_budget import day_window, node_guard
from .repository import (
    BudgetExceeded,
    budget_accounts,
    budget_reservations,
    instance_intents,
    jobs,
    money,
)
from .spending_limits import ADMIN_OWNER, lock_owner

INTERNAL_BACKENDS = frozenset({"mock", "cpu-render", "comfy-worker", "wangp-worker"})
TIMEZONE = "Australia/Sydney"


def _time(value, now):
    from .dstack_test_budget import finite

    return finite(value) and 0 <= value <= now


def _amount(value):
    try:
        money(value)
        return True
    except ValueError:
        return False


def _detail(code, reference, field):
    return {
        "code": code,
        "reference_type": reference[0],
        "reference_id": reference[1],
        "field": field,
    }


def _rental(connection, tenant_id, reference, rows, now):
    """Return a bound rental, its confirmed test exclusion, or finite problems."""
    from .operator_capacity import operator_nodes

    intent = (
        connection.execute(
            select(instance_intents).where(instance_intents.c.id == reference[1])
        )
        .mappings()
        .first()
    )
    if intent is None:
        return (
            None,
            False,
            [_detail("rental_binding_unconfirmed", reference, "intent_id")],
        )
    node = (
        connection.execute(
            select(operator_nodes).where(operator_nodes.c.intent_id == reference[1])
        )
        .mappings()
        .first()
    )
    problems, test = [], False
    if node is not None:
        payload = node["payload"]
        if not isinstance(payload, dict) or payload.get("tenant_id") != tenant_id:
            problems.append(
                _detail("rental_binding_unconfirmed", reference, "tenant_id")
            )
        else:
            try:
                guard = node_guard(dict(intent), dict(node))
                if guard is not None:
                    test = any(
                        row["account_id"] == guard["budget_account_id"] for row in rows
                    )
                    if not test:
                        problems.append(
                            _detail(
                                "rental_binding_unconfirmed",
                                reference,
                                "budget_account_id",
                            )
                        )
            except (TypeError, ValueError, KeyError):
                # A malformed guard must never turn a real liability into a test
                # exclusion. Keep it visible and require attribution review.
                problems.append(
                    _detail("rental_binding_unconfirmed", reference, "test_gpu_guard")
                )
    if not _time(intent["created_at"], now):
        problems.append(
            _detail("supplier_timestamp_unconfirmed", reference, "intent_created_at")
        )
    # A foreign tenant sharing an original reference cannot be silently treated
    # as another ordinary budget layer of this tenant.
    foreign = connection.execute(
        select(budget_accounts.c.tenant_id)
        .select_from(
            budget_reservations.join(
                budget_accounts,
                budget_accounts.c.id == budget_reservations.c.account_id,
            )
        )
        .where(
            budget_reservations.c.reference_type == reference[0],
            budget_reservations.c.reference_id == reference[1],
            budget_accounts.c.tenant_id != tenant_id,
        )
    ).first()
    if foreign is not None:
        problems.append(_detail("supplier_reference_conflict", reference, "tenant_id"))
    return dict(intent), test and not problems, problems


def _value(reference, rows, now, start):
    """Project one original obligation, not each accounting cap layer."""
    problems = []
    if any(not _amount(row["amount_microusd"]) for row in rows):
        return (
            None,
            0,
            [_detail("supplier_amount_unconfirmed", reference, "amount_microusd")],
        )
    if any(not _time(row["created_at"], now) for row in rows):
        problems.append(
            _detail("supplier_timestamp_unconfirmed", reference, "created_at")
        )
    states = {row["state"] for row in rows}
    amount = max(row["amount_microusd"] for row in rows)
    if states <= {"reserved", "invoice_pending"}:
        if any(row["actual_cost_microusd"] is not None for row in rows):
            problems.append(
                _detail("supplier_state_unconfirmed", reference, "actual_cost_microusd")
            )
        return (
            "pending" if "invoice_pending" in states else "committed",
            amount,
            problems,
        )
    if states <= {"settled", "released"}:
        actual = {row["actual_cost_microusd"] for row in rows}
        if len(actual) != 1 or not all(_amount(value) for value in actual):
            problems.append(
                _detail(
                    "supplier_amount_unconfirmed", reference, "actual_cost_microusd"
                )
            )
            return None, amount, problems
        dates = {row["settled_at"] for row in rows}
        if (
            len(dates) != 1
            or not all(_time(value, now) for value in dates)
            or any(
                not _time(row["created_at"], now)
                or row["settled_at"] < row["created_at"]
                for row in rows
            )
        ):
            problems.append(
                _detail("supplier_timestamp_unconfirmed", reference, "settled_at")
            )
            return None, amount, problems
        # Settled old invoices cease carrying only after their original actual
        # amount and settlement timestamp are known on every account layer.
        return (
            "settled",
            next(iter(actual)) if next(iter(dates)) >= start else 0,
            problems,
        )
    problems.append(_detail("supplier_state_unconfirmed", reference, "state"))
    return None, amount, problems


def projection(connection, tenant_id, now, *, policy=None):
    start, end = day_window(now)
    policy = policy if policy is not None else read_policy(connection, tenant_id)
    snapshot = admission_snapshot(policy)
    value = policy["value"]
    limit = value.get("tenant_daily_limit_microusd")
    warning = value.get("tenant_daily_warning_fraction", 0.8)
    result = {
        "status": "policy_not_adopted" if snapshot is None else "within_limit",
        "limit_microusd": limit,
        "warning_fraction": warning,
        "timezone": TIMEZONE,
        "period_start": start,
        "resets_at": end,
        "settled_microusd": 0,
        "committed_microusd": 0,
        "pending_microusd": 0,
        "projected_microusd": 0,
        "unresolved_count": 0,
        "details": [],
    }
    grouped = defaultdict(list)
    for row in connection.execute(
        select(budget_reservations)
        .select_from(
            budget_reservations.join(
                budget_accounts,
                budget_accounts.c.id == budget_reservations.c.account_id,
            )
        )
        .where(budget_accounts.c.tenant_id == tenant_id)
    ).mappings():
        grouped[(row["reference_type"], row["reference_id"])].append(row)
    provider_refs = {}
    for reference, rows in sorted(grouped.items()):
        if reference[0] == "job":
            job = (
                connection.execute(
                    select(jobs.c.execution_plan).where(
                        jobs.c.tenant_id == tenant_id, jobs.c.id == reference[1]
                    )
                )
                .mappings()
                .first()
            )
            if job and (
                job["execution_plan"].get("backend") in INTERNAL_BACKENDS
                or job["execution_plan"].get("simulation") is True
            ):
                continue
        if reference[0] != "instance":
            # There is currently no independently billed per-job supplier
            # adapter/proof contract. Never turn customer estimates into cost.
            if any(
                row["amount_microusd"] or row["actual_cost_microusd"] for row in rows
            ):
                result["details"].append(
                    _detail(
                        "external_obligation_unconfirmed", reference, "reference_type"
                    )
                )
            continue
        intent, excluded, problems = _rental(
            connection, tenant_id, reference, rows, now
        )
        if excluded:
            continue
        if intent and intent.get("provider_instance_id"):
            identity = (intent["provider"], intent["provider_instance_id"])
            previous = provider_refs.setdefault(identity, reference)
            if previous != reference:
                problems.append(
                    _detail(
                        "supplier_reference_conflict", reference, "provider_instance_id"
                    )
                )
        category, amount, values_problems = _value(reference, rows, now, start)
        problems.extend(values_problems)
        result["details"].extend(problems)
        if not problems and category is not None:
            result[category + "_microusd"] += amount
    result["unresolved_count"] = len(
        {(item["reference_type"], item["reference_id"]) for item in result["details"]}
    )
    result["confirmed_exposure_microusd"] = sum(
        result[key + "_microusd"] for key in ("settled", "committed", "pending")
    )
    result["projected_microusd"] = (
        None if result["details"] else result["confirmed_exposure_microusd"]
    )
    if snapshot is not None:
        if result["details"]:
            result["status"] = "usage_unconfirmed"
        elif result["projected_microusd"] >= limit:
            result["status"] = "limit_reached"
        elif result["projected_microusd"] >= limit * warning:
            result["status"] = "warning"
    return result


def admit(
    connection,
    tenant_id,
    now,
    *,
    additional_microusd=0,
    reference_type,
    reference_id=None,
    test_task=False,
    policy=None,
):
    """First admission only, under the caller's existing global capacity fence.

    Callers already own the capacity row before taking the tenant settings lock.
    That same transaction must persist both this snapshot and the reservation.
    Existing-command/job replay and rolling rental funding do not call here.
    test_task is trusted prospective admission data, never a stored exclusion.
    """
    money(additional_microusd)
    if type(test_task) is not bool or reference_type not in {"instance", "job"}:
        raise ValueError("supplier_admission_invalid")
    lock_owner(connection, tenant_id, ADMIN_OWNER)
    policy = policy if policy is not None else read_policy(connection, tenant_id)
    snapshot = admission_snapshot(policy)
    if snapshot is None or test_task:
        return snapshot
    usage = projection(connection, tenant_id, now, policy=policy)
    if usage["status"] == "usage_unconfirmed":
        raise BudgetExceeded("tenant_daily_usage_unconfirmed")
    if (
        usage["projected_microusd"] + additional_microusd > usage["limit_microusd"]
        or usage["status"] == "limit_reached"
    ):
        raise BudgetExceeded("tenant_daily_limit_reached")
    return snapshot


def _test_job(connection, tenant_id, owner_id, project_id, execution):
    """Only an exact existing staging rental can establish a test exemption."""
    from .operator_capacity import operator_nodes

    if execution.get("backend") != "wangp-worker" or not execution.get(
        "configuration_id"
    ):
        return False
    for node in connection.execute(select(operator_nodes)).mappings():
        payload = node["payload"]
        if not isinstance(payload, dict) or (
            payload.get("tenant_id"),
            payload.get("owner_id"),
            payload.get("project_id"),
            payload.get("configuration_id"),
        ) != (tenant_id, owner_id, project_id, execution.get("configuration_id")):
            continue
        intent = (
            connection.execute(
                select(instance_intents).where(
                    instance_intents.c.id == node["intent_id"],
                    instance_intents.c.pool == execution.get("pool"),
                )
            )
            .mappings()
            .first()
        )
        if intent is not None and intent["state"] not in {"destroyed", "failed"}:
            try:
                if node_guard(dict(intent), dict(node)) is not None:
                    return True
            except (TypeError, ValueError, KeyError):
                continue
    return False


def admit_job(connection, scope, execution, now, *, estimated_cost_microusd, job_id):
    """Freeze first admission; only paid execution enters the rejecting gate."""
    lock_owner(connection, scope.tenant_id, ADMIN_OWNER)
    policy = read_policy(connection, scope.tenant_id)
    snapshot = admission_snapshot(policy)
    if snapshot is None:
        return None
    quote = execution.get("credit_quote")
    paid = (
        estimated_cost_microusd > 0
        or execution.get("backend") in {"wangp-worker", "comfy-worker"}
        or isinstance(quote, dict)
        and quote.get("credits", 0) > 0
    )
    if not paid:
        return snapshot
    return admit(
        connection,
        scope.tenant_id,
        now,
        reference_type="job",
        reference_id=job_id,
        test_task=_test_job(
            connection, scope.tenant_id, scope.owner_id, scope.project_id, execution
        ),
        policy=policy,
    )
