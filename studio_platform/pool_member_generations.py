"""Immutable generation ancestry and conservative retirement ledger checks.

No provider calls, process management, credentials or alternate rental ledger.
Generation zero remains the original immutable pool membership.
"""

from sqlalchemy import or_, select

from .repository import (
    Conflict,
    artifacts,
    attempts,
    budget_reservations,
    capacity_pool_members,
    capacity_member_generations,
    jobs,
    registered_devices,
    registered_workers,
    scaler_actions,
    scaler_receipts,
)
from .service_policy import validate_member_replacement


def replacement_policy(payload):
    value = payload.get("member_replacement")
    if value is None:
        return None
    if (
        payload.get("pool_controller") != "continuing-two-members-v1"
        or "pool_members" not in payload
    ):
        raise Conflict("capacity_member_replacement_identity")
    return validate_member_replacement(value)


def bindings(connection, approval):
    """Return all immutable bindings and the unique latest binding per member."""
    from .capacity import pool_member_ids

    allowed = pool_member_ids(approval["payload"])
    originals = list(
        connection.execute(
            select(capacity_pool_members).where(
                capacity_pool_members.c.approval_id == approval["id"]
            )
        ).mappings()
    )
    extra = list(
        connection.execute(
            select(capacity_member_generations)
            .where(capacity_member_generations.c.approval_id == approval["id"])
            .order_by(capacity_member_generations.c.generation)
        ).mappings()
    )
    policy = replacement_policy(approval["payload"])
    if extra and policy is None:
        raise Conflict("capacity_member_unapproved_generation")
    all_rows, current, ids = [], {}, set()
    for original in originals:
        row = {**original, "generation": 0, "previous_intent_id": None}
        if (
            row["approval_hash"] != approval["approval_hash"]
            or row["member_id"] not in allowed
            or row["member_id"] in current
            or row["intent_id"] in ids
        ):
            raise Conflict("capacity_pool_member_identity_mismatch")
        all_rows.append(row)
        current[row["member_id"]] = row
        ids.add(row["intent_id"])
    for raw in extra:
        row = dict(raw)
        previous = current.get(row["member_id"])
        if (
            previous is None
            or row["approval_hash"] != approval["approval_hash"]
            or row["generation"] != previous["generation"] + 1
            or row["generation"] > policy["max_replacements"]
            or row["previous_intent_id"] != previous["intent_id"]
            or row["intent_id"] in ids
        ):
            raise Conflict("capacity_member_generation_chain_invalid")
        all_rows.append(row)
        current[row["member_id"]] = row
        ids.add(row["intent_id"])
    return all_rows, current


def one_receipt(connection, intent_id, operation):
    values = list(
        connection.execute(
            select(scaler_receipts.c.facts)
            .where(
                scaler_receipts.c.intent_id == intent_id,
                scaler_receipts.c.operation == operation,
            )
            .limit(2)
        ).scalars()
    )
    if len(values) > 1:
        raise Conflict("capacity_member_retirement_conflict")
    return values[0] if values else None


def no_rent_proven(connection, intent):
    """An exact once-only create returned authoritative absence, not a list miss."""
    if intent["state"] != "destroyed" or intent["provider_instance_id"] is not None:
        return False
    action = (
        connection.execute(
            select(scaler_actions).where(scaler_actions.c.intent_id == intent["id"])
        )
        .mappings()
        .first()
    )
    if (
        action is None
        or action["create_started_at"] is None
        or action["destroy_started_at"] is not None
    ):
        return False

    def positive(fact):
        return (
            isinstance(fact, dict)
            and fact.get("state") == "not_created"
            and fact.get("instance_id") is None
            and fact.get("absence_confirmed") is True
            and type(fact.get("actual_cost_microusd")) is int
            and fact["actual_cost_microusd"] == 0
        )

    if not positive(action["last_observation"]):
        return False
    receipts = list(
        connection.execute(
            select(scaler_receipts).where(
                scaler_receipts.c.intent_id == intent["id"],
                scaler_receipts.c.operation.in_(("create", "reconcile", "destroy")),
            )
        ).mappings()
    )
    return any(
        row["operation"] == "create" and positive(row["facts"]) for row in receipts
    ) and all(
        row["facts"].get("instance_id") is None
        and row["facts"].get("state") not in ("starting", "running", "destroyed")
        for row in receipts
    )


def retirement_ledger(connection, approval, intent):
    """Positive exact removal/billing and original execution stop obligations."""
    no_rent = no_rent_proven(connection, intent)
    if (
        intent["state"] != "destroyed"
        or not intent["provider_instance_id"]
        and not no_rent
    ):
        raise Conflict("member_replacement_removal_unconfirmed")
    if (
        intent["pool"] != approval["pool"]
        or intent["provider"] != approval["payload"]["launch"]["provider"]
    ):
        raise Conflict("capacity_pool_member_identity_mismatch")
    action = (
        connection.execute(
            select(scaler_actions).where(scaler_actions.c.intent_id == intent["id"])
        )
        .mappings()
        .one()
    )
    fact = action["last_observation"] or {}
    if not no_rent and (
        fact.get("state") != "destroyed"
        or fact.get("instance_id") != intent["provider_instance_id"]
    ):
        raise Conflict("member_replacement_removal_unconfirmed")
    receipts = list(
        connection.execute(
            select(scaler_receipts.c.facts).where(
                scaler_receipts.c.intent_id == intent["id"]
            )
        ).scalars()
    )
    removed = [
        r
        for r in receipts
        if isinstance(r, dict)
        and r.get("state") == "destroyed"
        and r.get("instance_id") == intent["provider_instance_id"]
    ]
    if not removed and not no_rent:
        raise Conflict("member_replacement_removal_unconfirmed")
    final = [
        r["actual_cost_microusd"]
        for r in removed
        if type(r.get("actual_cost_microusd")) is int and r["actual_cost_microusd"] >= 0
    ]
    reservations = list(
        connection.execute(
            select(budget_reservations).where(
                budget_reservations.c.reference_type == "instance",
                budget_reservations.c.reference_id == intent["id"],
            )
        ).mappings()
    )
    # The existing invoice poll settles these same authoritative rows after
    # removal. It need not produce a second provider lifecycle receipt. Never
    # require an impossible extra observation after polling has completed.
    costs = [r["actual_cost_microusd"] for r in reservations]
    if (
        not costs
        or any(type(cost) is not int or cost < 0 for cost in costs)
        or len(set(costs)) != 1
        or {r["account_id"] for r in reservations}
        != set(approval["payload"]["budget_account_ids"])
        or any(r["state"] not in ("settled", "released") for r in reservations)
        or any(value != costs[0] for value in final)
        or no_rent
        and (costs[0] != 0 or any(r["state"] != "released" for r in reservations))
    ):
        raise Conflict("member_replacement_billing_unconfirmed")
    worker_id = "lium-" + intent["id"].replace("-", "")
    workers = list(
        connection.execute(
            select(registered_workers)
            .where(
                or_(
                    registered_workers.c.id == worker_id,
                    (registered_workers.c.provider == intent["provider"])
                    & (
                        registered_workers.c.instance_id
                        == intent["provider_instance_id"]
                    )
                    & registered_workers.c.instance_id.is_not(None),
                )
            )
            .with_for_update()
        ).mappings()
    )
    if workers and (
        no_rent
        or len(workers) != 1
        or workers[0]["id"] != worker_id
        or workers[0]["provider"] != intent["provider"]
        or workers[0]["instance_id"] != intent["provider_instance_id"]
        or workers[0]["pool"] != approval["pool"]
        or workers[0]["state"] != "retired"
        or workers[0]["current_job_id"]
    ):
        raise Conflict("member_replacement_worker_unretired")
    if connection.execute(
        select(registered_devices.c.worker_id).where(
            registered_devices.c.worker_id == worker_id,
            registered_devices.c.state != "released",
        )
    ).first():
        raise Conflict("member_replacement_worker_unretired")
    history = list(
        connection.execute(
            select(attempts).where(attempts.c.worker_id == worker_id)
        ).mappings()
    )
    job_ids = {a["job_id"] for a in history}
    related = list(
        connection.execute(
            select(jobs)
            .where(or_(jobs.c.id.in_(job_ids), jobs.c.lease_worker_id == worker_id))
            .with_for_update()
        ).mappings()
    )
    if not workers and history:
        raise Conflict("member_replacement_attempt_unresolved")
    if any(
        a["status"] not in ("succeeded", "failed", "cancelled")
        or (a["submission_started_at"] is not None or a["upstream_task_id"] is not None)
        and a["upstream_stopped"] != 1
        for a in history
    ):
        raise Conflict("member_replacement_attempt_unresolved")
    for job in related:
        current = (
            connection.execute(
                select(attempts).where(
                    attempts.c.id == job["current_attempt_id"],
                    attempts.c.job_id == job["id"],
                    attempts.c.number == job["attempt_no"],
                )
            )
            .mappings()
            .first()
        )
        if (
            job["status"] not in ("succeeded", "failed", "cancelled")
            or job["lease_worker_id"] is not None
            or job["lease_expires_at"] is not None
            or current is None
            or current["status"] not in ("succeeded", "failed", "cancelled")
            or (
                current["submission_started_at"] is not None
                or current["upstream_task_id"] is not None
            )
            and current["upstream_stopped"] != 1
        ):
            raise Conflict("member_replacement_attempt_unresolved")
    return workers[0] if workers else None


def successful_generation(connection, intent_id):
    """Reset only from this generation's completed actual attempt/output."""
    rows = connection.execute(
        select(attempts.c.id)
        .join(jobs, jobs.c.id == attempts.c.job_id)
        .where(
            attempts.c.worker_id == "lium-" + intent_id.replace("-", ""),
            attempts.c.status == "succeeded",
            attempts.c.upstream_stopped == 1,
            attempts.c.submission_started_at.is_not(None),
            jobs.c.status == "succeeded",
            jobs.c.current_attempt_id == attempts.c.id,
            jobs.c.attempt_no == attempts.c.number,
            jobs.c.lease_worker_id.is_(None),
            jobs.c.lease_expires_at.is_(None),
        )
    ).scalars()
    for attempt in rows:
        outputs = list(
            connection.execute(
                select(artifacts.c.metadata).where(artifacts.c.attempt_id == attempt)
            ).scalars()
        )
        if (
            outputs
            and all(
                isinstance(value, dict) and value.get("validated") is True
                for value in outputs
            )
            and any(value.get("kind") == "video" for value in outputs)
        ):
            return True
    return False
