"""Staging test limits projected from the original reservation ledger.

V2 is scoped to an immutable acceptance task, not a daily counter. V1 remains
readable for original runs; no historical attribution or invoice is rewritten.
Layered owner/shared reservations are counted only once.
"""

from datetime import datetime, time, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .repository import (
    BudgetExceeded,
    budget_reservations,
    instance_intents,
    scaler_actions,
)

# Staging test pools: v6 plus the TaoMate v7 bindings (CR-20261025-65).
POOLS = frozenset(
    {
        "inkseq-staging-v6-fl",
        "inkseq-staging-v6-ref",
        "inkseq-staging-v7-fl",
        "inkseq-staging-v7-ref",
        # v8 (CUDA 13 + sage2, #794) staging pools.
        "inkseq-staging-v8-fl",
        "inkseq-staging-v8-ref",
    }
)
PROJECTS = frozenset(
    {"project-inkseq-staging-superdan", "project-inkseq-staging-supervan"}
)
ACCOUNT = "inkseq-staging-gpu-shared"
TASK = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")


def valid_task_id(value):
    return isinstance(value, str) and TASK.fullmatch(value) is not None


def guard_limit(guard):
    return guard[
        "task_limit_microusd" if guard["version"] == 2 else "daily_limit_microusd"
    ]


def limit_reason(guard):
    return (
        "dstack_test_task_limit_reached"
        if guard["version"] == 2
        else "dstack_test_daily_limit_reached"
    )


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_guard(value):
    fields = {
        "version",
        "timezone",
        "daily_limit_microusd",
        "budget_account_id",
        "max_runtime_seconds",
        "stop_margin_seconds",
    }
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] not in {1, 2}
    ):
        raise ValueError("dstack_test_guard_invalid")
    if value["version"] == 2:
        fields = fields - {"timezone", "daily_limit_microusd"} | {
            "task_id",
            "task_limit_microusd",
        }
    if (
        set(value) != fields
        or value["budget_account_id"] != ACCOUNT
        or value["version"] == 1
        and value["timezone"] != "Australia/Sydney"
        or value["version"] == 2
        and not valid_task_id(value["task_id"])
        or type(guard_limit(value)) is not int
        or not 1 <= guard_limit(value) <= 9_000_000_000_000_000
        or type(value["max_runtime_seconds"]) is not int
        or not 60 <= value["max_runtime_seconds"] <= 86400
        or type(value["stop_margin_seconds"]) is not int
        or not 300 <= value["stop_margin_seconds"] <= 600
    ):
        raise ValueError("dstack_test_guard_invalid")
    return value


def task_guard(template, task_id, *, limit, max_runtime):
    validate_guard(template)
    value = {key: template[key] for key in ("budget_account_id", "stop_margin_seconds")}
    value.update(
        version=2,
        task_id=task_id,
        task_limit_microusd=limit,
        max_runtime_seconds=max_runtime,
    )
    return validate_guard(value)


def day_window(now):
    if not finite(now):
        raise ValueError("dstack_test_clock_invalid")
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(
        ZoneInfo("Australia/Sydney")
    )
    start = datetime.combine(local.date(), time(), local.tzinfo)
    end = datetime.combine(local.date() + timedelta(days=1), time(), local.tzinfo)
    return start.timestamp(), end.timestamp()


def node_guard(intent, node):
    payload = node.get("payload", {})
    if not isinstance(payload, dict) or not isinstance(
        payload.get("lifecycle", {}), dict
    ):
        raise ValueError("dstack_test_guard_binding_invalid")
    guard = payload.get("lifecycle", {}).get("test_gpu_guard")
    if guard is None:
        return None
    validate_guard(guard)
    if not isinstance(payload.get("request"), dict):
        raise ValueError("dstack_test_guard_binding_invalid")
    if (
        intent.get("pool") not in POOLS
        or payload.get("project_id") not in PROJECTS
        or payload.get("capacity_backend") != "dstack-v1"
        or ACCOUNT not in payload.get("lifecycle", {}).get("budget_account_ids", [])
        or payload.get("request", {}).get("test_max_duration_s")
        != guard["max_runtime_seconds"]
    ):
        raise ValueError("dstack_test_guard_binding_invalid")
    if guard["version"] == 2:
        try:
            native_task = payload["dstack"]["run_spec"]["configuration"]["tags"][
                "inkseq_test_task"
            ]
        except (KeyError, TypeError):
            raise ValueError("dstack_test_guard_binding_invalid") from None
        if (
            payload["request"].get("test_task_id") != guard["task_id"]
            or native_task != guard["task_id"]
        ):
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
        if not finite(started) or not 0 <= now - started:
            return "dstack_test_guard_unconfirmed"
        if (
            now - started + expected_runtime_s + guard["stop_margin_seconds"]
            >= guard["max_runtime_seconds"]
        ):
            return "dstack_test_runtime_limit_reached"
        return None
    except (ValueError, TypeError, KeyError):
        return "dstack_test_guard_unconfirmed"


def projected_cost(connection, guard, now, *, replacement_id=None, minimum_reserved=0):
    """Same-task settled bills and commitments; original v1 daily semantics.

    An allocation spanning midnight is deliberately overcounted, rather than
    apportioned without an invoice. Open runs additionally cover elapsed time
    at their approved maximum rate plus shutdown grace and transport allowance.
    """
    from .operator_capacity import operator_nodes

    validate_guard(guard)
    start, _ = day_window(now)
    statement = (
        select(
            budget_reservations,
            instance_intents.c.pool,
            instance_intents.c.provider.label("instance_provider"),
            instance_intents.c.state.label("instance_state"),
            instance_intents.c.created_at.label("intent_created_at"),
            instance_intents.c.updated_at.label("intent_updated_at"),
            operator_nodes.c.payload,
            operator_nodes.c.runtime_state.label("node_runtime_state"),
            scaler_actions.c.create_started_at,
        )
        .select_from(
            budget_reservations.outerjoin(
                instance_intents,
                budget_reservations.c.reference_id == instance_intents.c.id,
            )
            .outerjoin(
                operator_nodes, operator_nodes.c.intent_id == instance_intents.c.id
            )
            .outerjoin(
                scaler_actions, scaler_actions.c.intent_id == instance_intents.c.id
            )
        )
        .where(budget_reservations.c.account_id == ACCOUNT)
    )
    total = 0
    for row in connection.execute(statement).mappings():
        if row["instance_provider"] == "modal":
            from .modal_budget import projected_reservation

            total += projected_reservation(connection, row, guard, now)
            continue
        if guard["version"] == 2:
            payload = row["payload"]
            lifecycle = payload.get("lifecycle") if isinstance(payload, dict) else None
            attribution = (
                lifecycle.get("test_gpu_guard") if isinstance(lifecycle, dict) else None
            )
            request = payload.get("request") if isinstance(payload, dict) else None
            request_task = (
                request.get("test_task_id") if isinstance(request, dict) else None
            )
            try:
                native_task = payload["dstack"]["run_spec"]["configuration"][
                    "tags"
                ].get("inkseq_test_task")
            except (KeyError, TypeError, AttributeError):
                native_task = None
            # Old/unattributed and unrelated obligations remain in the actual
            # shared account, but cannot consume this acceptance task's cap.
            attributed_task = (
                attribution.get("task_id") if isinstance(attribution, dict) else None
            )
            if guard["task_id"] not in (attributed_task, request_task, native_task):
                continue
            try:
                checked = node_guard({"pool": row["pool"]}, {"payload": payload})
                if (
                    checked is None
                    or checked["version"] != 2
                    or checked["task_id"] != guard["task_id"]
                ):
                    raise ValueError("dstack_test_guard_binding_invalid")
            except (ValueError, TypeError, KeyError):
                raise BudgetExceeded("dstack_test_budget_unconfirmed") from None
        if (
            row["reference_type"] != "instance"
            or row["pool"] not in POOLS
            or not isinstance(row["payload"], dict)
            or row["payload"].get("project_id") not in PROJECTS
            or row["payload"].get("capacity_backend") != "dstack-v1"
        ):
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        amount, actual = row["amount_microusd"], row["actual_cost_microusd"]
        if (
            type(amount) is not int
            or amount < 0
            or not finite(row["created_at"])
            or row["created_at"] > now
        ):
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        if row["state"] in {"settled", "released"}:
            settled = row["settled_at"]
            if (
                type(actual) is not int
                or actual < 0
                or not finite(settled)
                or settled > now
            ):
                raise BudgetExceeded("dstack_test_budget_unconfirmed")
            if guard["version"] == 2 or row["created_at"] >= start or settled >= start:
                total += actual
            continue
        if row["state"] not in {"reserved", "invoice_pending"} or actual is not None:
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        payload = row["payload"]
        if guard["version"] == 2 and (
            row["instance_state"] in {"destroyed", "not_created"}
            or row["node_runtime_state"] == "stopped"
        ):
            # Invoice unknown is still a real commitment, not a fabricated
            # elapsed-time bill for a machine that is no longer running.
            total += (
                max(amount, minimum_reserved)
                if row["reference_id"] == replacement_id
                else amount
            )
            continue
        hourly, extra = (
            payload.get("hourly_cost_microusd"),
            payload.get("extra_reservation_microusd", 0),
        )
        if (
            type(hourly) is not int
            or hourly <= 0
            or type(extra) is not int
            or extra < 0
        ):
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        started = row["create_started_at"]
        started = row["intent_created_at"] if started is None else started
        if not finite(started) or not 0 <= now - started:
            raise BudgetExceeded("dstack_test_budget_unconfirmed")
        binding = payload.get("dstack", {})
        # A native run can stop before the supplier stops billing its VM.
        # Positive provider absence bounds that later interval conservatively.
        ended = (
            binding.get("provider_removal_proof", {}).get(
                "observed_at", row["intent_updated_at"]
            )
            if row["instance_state"] == "destroyed"
            else None
        )
        through = (
            ended
            if finite(ended) and started <= ended <= now
            else now + guard["stop_margin_seconds"]
        )
        amount = max(amount, math.ceil(hourly * (through - started) / 3600) + extra)
        observed = binding.get("estimated_cost_microusd")
        if type(observed) is int and observed >= 0:
            amount = max(amount, observed + extra)
        if row["reference_id"] == replacement_id:
            amount = max(amount, minimum_reserved)
        total += amount
    return total


def budget_reason(
    connection, intent, node, now, *, minimum_reserved=0, expected_runtime_s=0
):
    reason = runtime_reason(intent, node, now, expected_runtime_s=expected_runtime_s)
    if reason:
        return reason
    try:
        guard = node_guard(intent, node)
        if guard is not None:
            from .spending_limits import ADMIN_OWNER, read_settings

            limits = read_settings(
                connection, node["payload"]["tenant_id"], ADMIN_OWNER
            )
            # New admissions freeze their accepted guard. Changing the admin
            # policy must not shorten an already running test rental. Legacy
            # nodes preserve their original behavior until explicitly adopted.
            snapshot = node["payload"].get("capacity_policy_snapshot")
            if snapshot is not None:
                from .capacity_settings import validate_snapshot

                validate_snapshot(snapshot)
            elif "capacity_policy_version" in limits["value"]:
                from .capacity_settings import POLICY_VERSION, validate_snapshot
                from .operator_capacity import operator_commands
                from .repository import outbox, request_hash
                from .spending_limits import validate_settings

                if (
                    type(limits["value"]["capacity_policy_version"]) is not int
                    or limits["value"]["capacity_policy_version"] != POLICY_VERSION
                ):
                    raise ValueError("dstack_test_legacy_adoption_unconfirmed")
                tenant = node["payload"]["tenant_id"]
                transitions = []
                for row in connection.execute(
                    select(outbox).where(
                        outbox.c.event_type == "spending.settings_updated",
                        outbox.c.payload["tenant_id"].as_string() == tenant,
                        outbox.c.payload["owner_id"].as_string() == ADMIN_OWNER,
                    )
                ).mappings():
                    event = row["payload"]
                    old, new = event["old"], event["new"]
                    if (
                        "capacity_policy_version" not in old["value"]
                        and "capacity_policy_version" in new["value"]
                    ):
                        transitions.append((row, old, new))
                if len(transitions) != 1:
                    raise ValueError("dstack_test_legacy_adoption_unconfirmed")
                adoption, old, new = transitions[0]
                event, at = adoption["payload"], adoption["created_at"]
                command = (
                    connection.execute(
                        select(operator_commands).where(
                            operator_commands.c.id == node["command_id"]
                        )
                    )
                    .mappings()
                    .first()
                )
                if command is None:
                    raise ValueError("dstack_test_legacy_adoption_unconfirmed")
                if not (
                    event["tenant_id"] == tenant
                    and event["owner_id"] == ADMIN_OWNER
                    and adoption["aggregate_id"] == ADMIN_OWNER
                    and type(limits["version"]) is int
                    and type(old["version"]) is int
                    and old["version"] >= 0
                    and type(new["version"]) is int
                    and new["version"] == old["version"] + 1 <= limits["version"]
                    and isinstance(event.get("actor_id"), str)
                    and 1 <= len(event["actor_id"]) <= 200
                    and isinstance(event.get("reason"), str)
                    and 1 <= len(event["reason"]) <= 500
                    and event["reason"].strip()
                    and finite(at)
                    and at <= now
                    and finite(intent["created_at"])
                    and finite(command["created_at"])
                    and 0 <= intent["created_at"] <= at
                    and 0 <= command["created_at"] <= at
                    and node["intent_id"] == intent["id"]
                    and command["kind"] == "dstack_start"
                    and command["payload"]["node_id"] == intent["id"]
                    and intent["intent_key"]
                    == "dstack:" + command["payload"]["preview_id"]
                    and node["payload"]["request"]["intent_id"] == intent["id"]
                    and command["actor"]
                    == "dstack:"
                    + request_hash(
                        {"tenant_id": tenant, "owner_id": node["payload"]["owner_id"]}
                    )
                ):
                    raise ValueError("dstack_test_legacy_adoption_unconfirmed")
                validate_snapshot(
                    {
                        "settings_version": new["version"],
                        "capacity_policy_version": POLICY_VERSION,
                        "value": new["value"],
                    }
                )
                # This is the one pre-adoption effective cap, not a later edit
                # and not the possibly higher template frozen in the old node.
                limits = {
                    "version": old["version"],
                    "value": validate_settings(old["value"], admin=True),
                }
            if limits["version"] and snapshot is None:
                key = (
                    "task_limit_microusd"
                    if guard["version"] == 2
                    else "daily_limit_microusd"
                )
                guard = dict(
                    guard,
                    **{
                        key: min(
                            guard_limit(guard),
                            limits["value"]["test_task_limit_microusd"],
                        )
                    },
                )
                runtime = min(
                    guard["max_runtime_seconds"], limits["value"]["max_runtime_seconds"]
                )
                if (
                    now
                    - intent["created_at"]
                    + expected_runtime_s
                    + guard["stop_margin_seconds"]
                    >= runtime
                ):
                    return "dstack_test_runtime_limit_reached"
        if guard is not None and projected_cost(
            connection,
            guard,
            now,
            replacement_id=intent["id"],
            minimum_reserved=minimum_reserved,
        ) >= guard_limit(guard):
            return limit_reason(guard)
        return None
    except (ValueError, TypeError, KeyError, BudgetExceeded):
        return "dstack_test_budget_unconfirmed"
