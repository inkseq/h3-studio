"""Read-only payment health, derived from original signed and ledger facts.

No provider/config reader, wallet mutation or queue is imported. The independent
connection keeps an unavailable optional family from hiding existing GPU alarms.
Only UUID incident identities, original times and closed aggregate counts leave.
"""

import math
import json
import time
import uuid

from sqlalchemy import and_, case, exists, literal, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from .credit_schema import entries
from .payment_schema import events, payments

MAX_ROWS = 10000
ACTIVITY = {
    "payment_failed": ("checkout.session.async_payment_failed",),
    "payment_refunded": ("charge.refunded",),
    "payment_disputed": ("charge.dispute.created", "charge.dispute.closed"),
}
PAID_TYPES = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
PENDING = ("received", "deferred", "review")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _empty(window_seconds):
    return {
        "complete": False,
        "fulfillment": [],
        "processing": [],
        "activity": {name: {"count": 0, "since": None} for name in ACTIVITY},
        "window_seconds": window_seconds,
    }


def _scope(table, tenant_id, livemode, account_scope):
    clauses = [table.c.tenant == tenant_id]
    if livemode is not None:
        clauses.append(table.c.livemode == int(livemode))
    if account_scope is not None:
        clauses.append(table.c.account_scope == account_scope)
    return clauses


def _grant_exists():
    # This is the immutable original purchase, not today's available balance.
    return exists(
        select(entries.c.id).where(
            entries.c.tenant == payments.c.tenant,
            entries.c.user_id == payments.c.user_id,
            entries.c.kind == "purchase",
            entries.c.amount == payments.c.credits,
            entries.c.held_delta == 0,
            entries.c.reference_type == "payment",
            entries.c.reference_id == payments.c.id,
            entries.c.actor == "stripe:webhook",
            entries.c.idempotency_key
            == literal("stripe-purchase:") + payments.c.stripe_checkout_session_id,
            entries.c.origin
            == case((payments.c.livemode == 1, "stripe-live"), else_="stripe-test"),
        )
    ).correlate(payments)


def _event_statement(tenant_id, livemode, account_scope):
    obj = events.c.verified_payload["object"]
    identifiers = (
        ("payment_id", payments.c.id),
        ("session_id", payments.c.stripe_checkout_session_id),
        ("payment_intent_id", payments.c.stripe_payment_intent_id),
        ("charge_id", payments.c.stripe_charge_id),
        ("dispute_id", payments.c.stripe_dispute_id),
    )
    same = and_(
        payments.c.tenant == events.c.tenant,
        payments.c.account_scope == events.c.account_scope,
        payments.c.livemode == events.c.livemode,
        or_(
            payments.c.id == events.c.payment_id,
            *(column == obj[key].as_string() for key, column in identifiers),
        ),
    )
    return (
        select(
            events.c.event_id,
            events.c.account_scope,
            events.c.livemode,
            events.c.received_at,
            payments.c.id.label("correlated_id"),
            _grant_exists().label("grant_present"),
        )
        .select_from(events.outerjoin(payments, same))
        .where(
            *_scope(events, tenant_id, livemode, account_scope),
            events.c.verified_payload["account_scope"].as_string()
            == events.c.account_scope,
            events.c.verified_payload["livemode"].as_boolean()
            == (events.c.livemode == 1),
            events.c.verified_payload["id"].as_string() == events.c.event_id,
            events.c.verified_payload["type"].as_string() == events.c.type,
        )
    )


def _rows(connection, statement):
    rows = list(connection.execute(statement.limit(MAX_ROWS + 1)).mappings())
    if len(rows) > MAX_ROWS:
        raise ValueError("payment_alert_snapshot_too_large")
    return rows


def _event_incidents(rows, tenant_id, now, *, needs_grant=False):
    grouped = {}
    for row in rows:
        if not _finite(row["received_at"]) or row["received_at"] > now:
            raise ValueError("payment_alert_timestamp_invalid")
        grouped.setdefault(row["event_id"], []).append(row)
    incidents = {}
    for event_id, group in grouped.items():
        first = group[0]
        if needs_grant and all(
            row["correlated_id"] is not None and row["grant_present"] for row in group
        ):
            continue
        matched = {r["correlated_id"] for r in group if r["correlated_id"] is not None}
        if len(matched) == 1:
            identity = next(iter(matched))
            if not _uuid(identity):
                raise ValueError("payment_alert_identity_invalid")
        else:
            # Ambiguous correlation is an event incident, never false authority
            # over one arbitrarily chosen customer's purchase.
            identity = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"inkseq:payment-alert:{tenant_id}:{first['account_scope']}:"
                    f"{first['livemode']}:{event_id}",
                )
            )
        incidents[identity] = min(incidents.get(identity, now), first["received_at"])
    return incidents


def _add_rows(target, rows, now):
    for row in rows:
        identity, since = row["id"], row["since"]
        if not _uuid(identity) or not _finite(since) or since > now:
            raise ValueError("payment_alert_original_fact_invalid")
        target[identity] = min(target.get(identity, now), since)


def read_payment_snapshot(
    repo, *, now, tenant_id, livemode=None, account_scope=None, window_seconds=300
):
    """Observe original payment facts without reading credentials or wallets.

    A false ``complete`` means all payment fields are unknown. In particular,
    consumers must not resolve an existing incident from the empty placeholders.
    """
    if (
        not _finite(now)
        or not isinstance(tenant_id, str)
        or not tenant_id
        or livemode is not None
        and type(livemode) is not bool
        or account_scope is not None
        and (not isinstance(account_scope, str) or not account_scope)
        or type(window_seconds) is not int
        or not 60 <= window_seconds <= 86400
    ):
        raise ValueError("payment_alert_context_invalid")
    result = _empty(window_seconds)
    try:
        with repo.engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
                connection.execute(text("SET LOCAL statement_timeout='5000'"))
            scope = _scope(payments, tenant_id, livemode, account_scope)
            fulfillment = {}
            _add_rows(
                fulfillment,
                _rows(
                    connection,
                    select(
                        payments.c.id,
                        case(
                            (payments.c.paid_at.is_not(None), payments.c.paid_at),
                            else_=payments.c.created_at,
                        ).label("since"),
                    ).where(
                        *scope,
                        or_(
                            payments.c.paid_at.is_not(None), payments.c.status == "paid"
                        ),
                        ~_grant_exists(),
                    ),
                ),
                now,
            )
            event_query = _event_statement(tenant_id, livemode, account_scope)
            signed_paid = event_query.where(
                events.c.status.in_(PENDING),
                events.c.type.in_(PAID_TYPES),
                events.c.verified_payload["object"]["payment_status"].as_string()
                == "paid",
            )
            for identity, since in _event_incidents(
                _rows(connection, signed_paid), tenant_id, now, needs_grant=True
            ).items():
                fulfillment[identity] = min(fulfillment.get(identity, now), since)
            processing = _event_incidents(
                _rows(connection, event_query.where(events.c.status.in_(PENDING))),
                tenant_id,
                now,
            )
            for name, kinds in ACTIVITY.items():
                activity = _event_incidents(
                    _rows(
                        connection,
                        event_query.where(
                            events.c.type.in_(kinds),
                            events.c.received_at >= now - window_seconds,
                            events.c.received_at <= now,
                        ),
                    ),
                    tenant_id,
                    now,
                )
                condition = {
                    "payment_failed": payments.c.status == "failed",
                    "payment_refunded": payments.c.refunded_cents > 0,
                    "payment_disputed": payments.c.dispute_open == 1,
                }[name]
                _add_rows(
                    activity,
                    _rows(
                        connection,
                        select(
                            payments.c.id, payments.c.updated_at.label("since")
                        ).where(
                            *scope,
                            condition,
                            payments.c.updated_at >= now - window_seconds,
                            payments.c.updated_at <= now,
                        ),
                    ),
                    now,
                )
                result["activity"][name] = {
                    "count": len(activity),
                    "since": min(activity.values()) if activity else None,
                }
            result["fulfillment"] = [
                {"id": identity, "since": since}
                for identity, since in sorted(fulfillment.items())
            ]
            result["processing"] = [
                {"id": identity, "since": since}
                for identity, since in sorted(processing.items())
            ]
            result["complete"] = True
        return result
    except (SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError):
        # Never expose database exceptions, SQL text or signed/provider payloads.
        return _empty(window_seconds)


def main():
    """Optional probe with its own process/timeout, isolated from GPU health."""
    import argparse

    from .repository import Repository
    from .settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument('--window-seconds', type=int, default=300)
    arguments = parser.parse_args()
    repo = None
    value = _empty(arguments.window_seconds)
    try:
        settings = Settings.from_environment()
        repo = Repository(settings.database_url)
        value = read_payment_snapshot(
            repo,
            now=time.time(),
            tenant_id=settings.tenant_id,
            livemode=settings.deployment_target == 'production',
            window_seconds=arguments.window_seconds,
        )
    except Exception:
        pass  # A failed optional probe carries no SQL/config/error text.
    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
