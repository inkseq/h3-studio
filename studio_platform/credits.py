"""Customer credits on the original admission transaction; never supplier USD.

Balance is total unspent credit, held is a reserved subset. Every mutation is
an append-only entry with both deltas and after-values. This module owns no
transaction, migration, provider call or authorization endpoint.
"""

import base64
from datetime import datetime, timezone
import json
import math
import re
import uuid
from decimal import Decimal

from sqlalchemy import and_, exists, func, insert, or_, select, update

from .credit_schema import accounts, entries as credit_entries, ready, CreditSchemaError
from .identity_schema import users
from .repository import (
    artifacts,
    attempts,
    documents,
    jobs,
    request_hash,
    spending_origins,
)

MAX_CREDITS = (1 << 63) - 1
POLICY_OWNER = POLICY_PROJECT = "__credits"
POLICY_KIND = "credit_policy"
POLICY_ID = "active"


class CreditError(Exception):
    def __init__(self, code, status=409, *, detail=None, **data):
        self.code, self.status, self.data = code, status, data
        self.detail = detail or code
        super().__init__(code)


def _integer(value, *, signed=False, positive=False):
    if (
        type(value) is not int
        or not (-MAX_CREDITS if signed else 0) <= value <= MAX_CREDITS
    ):
        raise CreditError("credit_amount_invalid", 422)
    if positive and value <= 0:
        raise CreditError("credit_amount_invalid", 422)
    return value


def _aggregate_integer(value):
    """PostgreSQL SUM(bigint) returns numeric; normalize only exact DB totals."""
    if type(value) is Decimal:
        if (
            not value.is_finite()
            or not 0 <= value <= MAX_CREDITS
            or value != value.to_integral_value()
        ):
            raise CreditError('credit_aggregate_invalid', 503)
        return int(value)
    if type(value) is not int or not 0 <= value <= MAX_CREDITS:
        raise CreditError('credit_aggregate_invalid', 503)
    return value


def _text(value, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or not (0 if empty else 1) <= len(value) <= maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise CreditError("credit_reference_invalid", 422)
    return value


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise CreditError("credit_clock_invalid", 503)
    return value


def _ready(connection):
    try:
        return ready(connection)
    except CreditSchemaError:
        raise CreditError("credit_schema_not_ready", 503) from None


def credit_policy(connection, tenant):
    """One persisted switch shared by API/worker; absent pre-adoption is legacy."""
    adopted = _ready(connection)
    value = connection.execute(
        select(documents.c.payload).where(
            documents.c.tenant_id == tenant,
            documents.c.owner_id == POLICY_OWNER,
            documents.c.project_id == POLICY_PROJECT,
            documents.c.kind == POLICY_KIND,
            documents.c.document_id == POLICY_ID,
        )
    ).scalar_one_or_none()
    if value is None:
        return {"enabled": False, "active_revision": None}
    if (
        type(value) is not dict
        or set(value) != {"enabled", "active_revision"}
        or type(value["enabled"]) is not bool
        or value["active_revision"] is not None
        and (
            not isinstance(value["active_revision"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value["active_revision"])
        )
        or value["enabled"]
        and (not adopted or value["active_revision"] is None)
    ):
        raise CreditError("credit_policy_invalid", 503)
    return dict(value)


def quote(connection, tenant, owner, compiled, *, execution=None):
    from .credits_pricing import quote as price_quote

    return price_quote(connection, tenant, owner, compiled, execution=execution)


def _user(connection, tenant, owner):
    row = (
        connection.execute(
            select(users.c.id, users.c.owner, users.c.status).where(
                users.c.tenant == tenant, users.c.owner == owner
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CreditError("credit_account_not_found", 404)
    return row


def _account(connection, tenant, user_id, *, now, create=False, lock=True):
    if not _ready(connection):
        raise CreditError("credit_schema_not_ready", 503)
    if create:
        # Both supported databases provide a conflict-safe bootstrap. The row
        # lock below remains the sole balance serializer, including first use.
        if connection.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as upsert
        elif connection.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as upsert
        else:
            raise CreditError("credit_database_unsupported", 503)
        connection.execute(
            upsert(accounts)
            .values(
                user_id=user_id,
                tenant=tenant,
                balance=0,
                held=0,
                version=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
    statement = select(accounts).where(accounts.c.user_id == user_id)
    row = (
        connection.execute(statement.with_for_update() if lock else statement)
        .mappings()
        .first()
    )
    if row is not None and row["tenant"] != tenant:
        raise CreditError("credit_account_not_found", 404)
    result = (
        dict(row)
        if row is not None
        else dict(
            user_id=user_id,
            tenant=tenant,
            balance=0,
            held=0,
            version=0,
            created_at=now,
            updated_at=now,
        )
    )
    if lock and row is not None:
        from .credit_expiry import safe, expire_due

        result = safe(expire_due, connection, result, now, create=True)
    return result


def _public_account(row):
    return {key: row[key] for key in ("user_id", "balance", "held", "version")} | {
        "available": row["balance"] - row["held"]
    }


def account(connection, tenant, owner, *, now):
    user = _user(connection, tenant, owner)
    row = _account(connection, tenant, user["id"], now=_clock(now), lock=False)
    from .dstack_test_budget import day_window

    start, end = day_window(now)
    used = connection.execute(
        select(func.coalesce(func.sum(-credit_entries.c.amount), 0)).where(
            credit_entries.c.tenant == tenant,
            credit_entries.c.user_id == user["id"],
            credit_entries.c.kind == "capture",
            credit_entries.c.created_at >= start,
            credit_entries.c.created_at < end,
        )
    ).scalar_one()
    used = _aggregate_integer(used)
    from .spending_limits import credit_settings

    limit = credit_settings(connection, tenant, owner)["value"]["daily_limit_credits"]
    result = {**_public_account(row), "today_used": used, "daily_limit": limit}
    from .credit_expiry import safe, public_projection

    projected = safe(public_projection, connection, row, now)
    if projected is not None:
        result.update(
            expiry=projected,
            balance=result["balance"] - projected["expired_unspent"],
            available=result["available"] - projected["expired_unspent"],
        )
    return result


def daily_projection(connection, tenant, owner, now, actor_id=None):
    """Captures today plus every unresolved hold, without counting layers twice."""
    if not _ready(connection):
        return 0
    user = _user(connection, tenant, owner)
    from .dstack_test_budget import day_window

    start, end = day_window(_clock(now))
    terminal = credit_entries.alias("credit_terminal")
    resolved = exists(
        select(terminal.c.id).where(
            terminal.c.tenant == credit_entries.c.tenant,
            terminal.c.user_id == credit_entries.c.user_id,
            terminal.c.reference_type == credit_entries.c.reference_type,
            terminal.c.reference_id == credit_entries.c.reference_id,
            terminal.c.kind.in_(("capture", "release")),
        )
    )
    where = [credit_entries.c.tenant == tenant, credit_entries.c.user_id == user["id"]]
    if actor_id is not None:
        where.append(credit_entries.c.origin == actor_id)
    captured = connection.execute(
        select(func.coalesce(func.sum(-credit_entries.c.amount), 0)).where(
            *where,
            credit_entries.c.kind == "capture",
            credit_entries.c.created_at >= start,
            credit_entries.c.created_at < end,
        )
    ).scalar_one()
    captured = _aggregate_integer(captured)
    held = connection.execute(
        select(func.coalesce(func.sum(credit_entries.c.held_delta), 0)).where(
            *where,
            credit_entries.c.kind == "hold",
            ~resolved,
        )
    ).scalar_one()
    held = _aggregate_integer(held)
    return _aggregate_integer(captured + held)


def _change(
    connection,
    row,
    *,
    kind,
    amount,
    held_delta,
    reference_type,
    reference_id,
    actor,
    reason,
    idempotency_key,
    now,
    price_revision=None,
    origin=None,
    receipt_url=None,
    fingerprint=None,
    issued_at=None,
):
    amount, held_delta = (
        _integer(amount, signed=True),
        _integer(held_delta, signed=True),
    )
    if issued_at is not None:
        if kind not in {"grant", "purchase"} or _clock(issued_at) > _clock(now):
            raise CreditError("credit_clock_invalid", 422)
    for value, maximum, empty in (
        (reference_type, 32, False),
        (reference_id, 200, False),
        (actor, 200, False),
        (reason, 500, False),
        (idempotency_key, 200, False),
    ):
        _text(value, maximum, empty=empty)
    for value, maximum in ((price_revision, 64), (origin, 200), (receipt_url, 2048)):
        if value is not None:
            _text(value, maximum)
    fingerprint = fingerprint or request_hash(
        {
            "kind": kind,
            "amount": amount,
            "held_delta": held_delta,
            "reference_type": reference_type,
            "reference_id": reference_id,
            "actor": actor,
            "reason": reason,
            "price_revision": price_revision,
            "origin": origin,
            "receipt_url": receipt_url,
            **({"issued_at": issued_at} if issued_at is not None else {}),
        }
    )
    prior = (
        connection.execute(
            select(credit_entries).where(
                credit_entries.c.tenant == row["tenant"],
                credit_entries.c.user_id == row["user_id"],
                credit_entries.c.idempotency_key == idempotency_key,
            )
        )
        .mappings()
        .first()
    )
    if prior is not None:
        if prior["fingerprint"] != fingerprint:
            raise CreditError("credit_idempotency_conflict")
        return {"account": _public_account(row), "entry": dict(prior), "created": False}
    balance, held = row["balance"] + amount, row["held"] + held_delta
    if not 0 <= held <= balance <= MAX_CREDITS:
        raise CreditError("credit_balance_conflict")
    changed = connection.execute(
        update(accounts)
        .where(
            accounts.c.user_id == row["user_id"],
            accounts.c.tenant == row["tenant"],
            accounts.c.version == row["version"],
        )
        .values(balance=balance, held=held, version=row["version"] + 1, updated_at=now)
    )
    if changed.rowcount != 1:
        raise CreditError("credit_account_changed")
    entry = dict(
        id=str(uuid.uuid4()),
        tenant=row["tenant"],
        user_id=row["user_id"],
        kind=kind,
        amount=amount,
        held_delta=held_delta,
        balance_after=balance,
        held_after=held,
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        created_at=_clock(now),
        price_revision=price_revision,
        origin=origin,
        receipt_url=receipt_url,
    )
    connection.execute(insert(credit_entries).values(**entry))
    current = dict(row, balance=balance, held=held, version=row["version"] + 1)
    from .credit_expiry import safe, record, expire_due

    safe(record, connection, row, entry, issued_at=issued_at)
    if kind != "expire":
        current = safe(expire_due, connection, current, now)
    return {"account": _public_account(current), "entry": entry, "created": True}


def adjust(connection, tenant, owner, delta, *, actor, reason, idempotency_key, now):
    delta = _integer(delta, signed=True)
    if not delta:
        raise CreditError("credit_amount_invalid", 422)
    user = _user(connection, tenant, owner)
    row = _account(connection, tenant, user["id"], now=_clock(now), create=True)
    return _change(
        connection,
        row,
        kind="adjust",
        amount=delta,
        held_delta=0,
        reference_type="admin_action",
        reference_id=idempotency_key,
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
        now=now,
    )


def grant(
    connection,
    tenant,
    owner,
    amount,
    *,
    actor,
    reason,
    idempotency_key,
    now,
    kind="grant",
    reference_type="admin_action",
    reference_id=None,
    price_revision=None,
    origin=None,
    receipt_url=None,
    issued_at=None,
):
    if kind not in {"grant", "purchase", "refund", "waive"}:
        raise CreditError("credit_kind_invalid", 422)
    amount = _integer(amount, positive=True)
    user = _user(connection, tenant, owner)
    row = _account(connection, tenant, user["id"], now=_clock(now), create=True)
    return _change(
        connection,
        row,
        kind=kind,
        amount=amount,
        held_delta=0,
        reference_type=reference_type,
        reference_id=reference_id or idempotency_key,
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
        now=now,
        price_revision=price_revision,
        origin=origin,
        receipt_url=receipt_url,
        issued_at=issued_at,
    )


def _job(connection, job):
    row = (
        connection.execute(
            select(jobs)
            .where(
                jobs.c.id == job["id"],
                jobs.c.tenant_id == job["tenant_id"],
                jobs.c.owner_id == job["owner_id"],
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CreditError("credit_job_not_found", 404)
    return row


def hold(connection, repo, scope, job, spending_actor=None):
    policy = credit_policy(connection, scope.tenant_id)
    if not policy["enabled"]:
        return None
    if (scope.tenant_id, scope.owner_id) != (job["tenant_id"], job["owner_id"]):
        raise CreditError("credit_job_not_found", 404)
    frozen = job.get("execution_plan", {}).get("credit_quote")
    if (
        type(frozen) is not dict
        or type(frozen.get("version")) is not int
        or frozen["version"] not in (1, 2)
        or frozen.get("enforced") is not True
        or not isinstance(frozen.get("price_revision"), str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", frozen["price_revision"])
    ):
        raise CreditError("credit_quote_required")
    if frozen["version"] == 2 and policy["active_revision"] != frozen["price_revision"]:
        raise CreditError("credit_policy_changed_repreflight", 409)
    amount = _integer(frozen.get("credits"))
    user = _user(connection, scope.tenant_id, scope.owner_id)
    if user["status"] != "active" or frozen.get("user_id") != user["id"]:
        raise CreditError("credit_account_unavailable", 403)
    from .spending_limits import check_credit_spending

    origin = (
        connection.execute(
            select(spending_origins).where(
                spending_origins.c.reference_type == "job",
                spending_origins.c.reference_id == job["id"],
            )
        )
        .mappings()
        .first()
    )
    if origin is not None and (origin["tenant_id"], origin["owner_id"]) != (
        scope.tenant_id,
        scope.owner_id,
    ):
        raise CreditError("credit_origin_conflict")
    actor = (
        origin["actor_id"] if origin is not None else spending_actor or scope.actor_id
    )
    key = "job:" + job["id"] + ":hold"
    fingerprint = request_hash({"job": job["id"], "quote": frozen, "origin": actor})
    existing = connection.execute(
        select(credit_entries.c.fingerprint).where(
            credit_entries.c.tenant == scope.tenant_id,
            credit_entries.c.user_id == user["id"],
            credit_entries.c.idempotency_key == key,
        )
    ).scalar_one_or_none()
    if existing is None:
        check_credit_spending(connection, repo, scope, job, amount, actor_id=actor)
    row = _account(
        connection, scope.tenant_id, user["id"], now=repo.clock(), create=True
    )
    if existing is None and row["balance"] - row["held"] < amount:
        raise CreditError(
            "insufficient_credits",
            402,
            detail="Not enough credits for this generation.",
            need=amount,
            have=row["balance"] - row["held"],
        )
    return _change(
        connection,
        row,
        kind="hold",
        amount=0,
        held_delta=amount,
        reference_type="job",
        reference_id=job["id"],
        actor=actor,
        reason="Generation reserved",
        idempotency_key=key,
        now=repo.clock(),
        price_revision=frozen["price_revision"],
        origin=actor,
        fingerprint=fingerprint,
    )


def _terminal(connection, repo, job, *, kind, reason):
    frozen = job.get("execution_plan", {}).get("credit_quote")
    if not frozen:
        return None
    if not _ready(connection):
        raise CreditError("credit_schema_not_ready", 503)
    held = (
        connection.execute(
            select(credit_entries).where(
                credit_entries.c.tenant == job["tenant_id"],
                credit_entries.c.kind == "hold",
                credit_entries.c.reference_type == "job",
                credit_entries.c.reference_id == job["id"],
            )
        )
        .mappings()
        .all()
    )
    if not held:
        return None
    if len(held) != 1:
        raise CreditError("credit_hold_conflict")
    original = held[0]
    user = _user(connection, job["tenant_id"], job["owner_id"])
    if original["user_id"] != user["id"]:
        raise CreditError("credit_hold_conflict")
    current = _job(connection, job)
    if kind == "capture":
        committed = connection.execute(
            select(artifacts.c.id).where(
                artifacts.c.job_id == job["id"],
                artifacts.c.metadata["validated"].as_boolean().is_(True),
            )
        ).first()
        if current["status"] != "succeeded" or committed is None:
            raise CreditError("credit_outcome_unconfirmed")
    else:
        if current["status"] not in {"failed", "cancelled"}:
            raise CreditError("credit_outcome_unconfirmed")
        for attempt in connection.execute(
            select(attempts).where(attempts.c.job_id == job["id"])
        ).mappings():
            from .capacity_reclaim import authorized_lost_attempt

            closed = (
                attempt["status"] in {"succeeded", "failed", "cancelled"}
                and attempt["upstream_stopped"] == 1
            )
            unsubmitted = (
                attempt["submission_started_at"] is None
                and attempt["upstream_task_id"] is None
                and attempt["status"]
                in {"claimed", "lease_expired", "deferred", "failed", "cancelled"}
            )
            # A fenced abandoned attempt cannot deliver to this job. Customer
            # credit release is independent of the unresolved supplier invoice.
            if (
                not closed
                and not unsubmitted
                and not authorized_lost_attempt(current, attempt)
            ):
                raise CreditError("credit_outcome_unconfirmed")
    amount = original["held_delta"]
    row = _account(connection, job["tenant_id"], user["id"], now=repo.clock())
    return _change(
        connection,
        row,
        kind=kind,
        amount=-amount if kind == "capture" else 0,
        held_delta=-amount,
        reference_type="job",
        reference_id=job["id"],
        actor=original["actor"],
        reason=reason,
        idempotency_key="job:" + job["id"] + ":terminal",
        now=repo.clock(),
        price_revision=original["price_revision"],
        origin=original["origin"],
        fingerprint=request_hash(
            {"job": job["id"], "kind": kind, "hold": original["id"]}
        ),
    )


def capture(connection, repo, job, reason="Generation completed"):
    return _terminal(connection, repo, job, kind="capture", reason=reason)


def release(connection, repo, job, reason="Generation released"):
    return _terminal(connection, repo, job, kind="release", reason=reason)


def refund_job(connection, repo, job, *, actor, reason, idempotency_key):
    """A reviewed full waiver refunds only this job's captured customer credits."""
    if not job.get("execution_plan", {}).get("credit_quote"):
        return None
    if not _ready(connection):
        raise CreditError("credit_schema_not_ready", 503)
    user = _user(connection, job["tenant_id"], job["owner_id"])
    row = _account(connection, job["tenant_id"], user["id"], now=repo.clock())
    captured = list(
        connection.execute(
            select(credit_entries).where(
                credit_entries.c.tenant == job["tenant_id"],
                credit_entries.c.user_id == user["id"],
                credit_entries.c.reference_type == "job",
                credit_entries.c.reference_id == job["id"],
                credit_entries.c.kind == "capture",
            )
        ).mappings()
    )
    if not captured:
        return None
    if len(captured) != 1 or captured[0]["amount"] > 0:
        raise CreditError("credit_capture_conflict")
    capture_entry = captured[0]
    ceiling = -capture_entry["amount"]
    fingerprint = request_hash(
        {
            "kind": "waive",
            "capture": capture_entry["id"],
            "ceiling": ceiling,
            "actor": actor,
            "reason": reason,
        }
    )
    previous = (
        connection.execute(
            select(credit_entries).where(
                credit_entries.c.tenant == job["tenant_id"],
                credit_entries.c.user_id == user["id"],
                credit_entries.c.idempotency_key == idempotency_key,
            )
        )
        .mappings()
        .first()
    )
    refunded = connection.execute(
        select(func.coalesce(func.sum(credit_entries.c.amount), 0)).where(
            credit_entries.c.tenant == job["tenant_id"],
            credit_entries.c.user_id == user["id"],
            credit_entries.c.reference_type == "job",
            credit_entries.c.reference_id == job["id"],
            credit_entries.c.kind.in_(("refund", "waive")),
        )
    ).scalar_one()
    refunded = _aggregate_integer(refunded)
    remaining = ceiling - refunded
    if remaining < 0:
        raise CreditError("credit_refund_conflict")
    if previous is None and not remaining:
        return None
    return _change(
        connection,
        row,
        kind="waive",
        amount=previous["amount"] if previous else remaining,
        held_delta=0,
        reference_type="job",
        reference_id=job["id"],
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
        now=repo.clock(),
        price_revision=capture_entry["price_revision"],
        origin=capture_entry["origin"],
        fingerprint=fingerprint,
    )


def entries(connection, tenant, owner, *, cursor=None, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100 or not _ready(connection):
        raise CreditError("credit_page_invalid", 422)
    user = _user(connection, tenant, owner)
    binding = request_hash({"tenant": tenant, "user_id": user["id"]})
    position = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,800}", cursor
            ):
                raise ValueError
            value = json.loads(
                base64.b64decode(
                    cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
                )
            )
            if (
                type(value) is not dict
                or set(value) != {"binding", "time", "id"}
                or value["binding"] != binding
            ):
                raise ValueError
            _clock(value["time"])
            if str(uuid.UUID(value["id"])) != value["id"]:
                raise ValueError
            position = value
        except (ValueError, TypeError, AttributeError, CreditError):
            raise CreditError("credit_cursor_invalid", 422) from None
    statement = select(credit_entries).where(
        credit_entries.c.tenant == tenant, credit_entries.c.user_id == user["id"]
    )
    if position is not None:
        statement = statement.where(
            or_(
                credit_entries.c.created_at < position["time"],
                and_(
                    credit_entries.c.created_at == position["time"],
                    credit_entries.c.id < position["id"],
                ),
            )
        )
    rows = list(
        connection.execute(
            statement.order_by(
                credit_entries.c.created_at.desc(), credit_entries.c.id.desc()
            ).limit(limit + 1)
        ).mappings()
    )
    selected = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = selected[-1]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "binding": binding,
                        "time": last["created_at"],
                        "id": last["id"],
                    },
                    separators=(",", ":"),
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {
        "entries": [
            {
                **{
                    key: row[key]
                    for key in (
                        "id",
                        "kind",
                        "amount",
                        "held_delta",
                        "balance_after",
                        "held_after",
                        "reason",
                    )
                },
                "created_at": datetime.fromtimestamp(
                    row["created_at"], timezone.utc
                ).isoformat(),
            }
            for row in selected
        ],
        "next_cursor": next_cursor,
    }
