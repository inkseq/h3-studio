"""What customers paid, for the admin Pricing tab (#485). Read-only.

The cohort is orders paid inside the period; their refunds and disputes count
whenever they happened. Stripe test-mode orders are kept apart from live ones,
so staging never reads as revenue. Gross amounts come from the processed signed
checkout events (tax-inclusive, as charged). The net figure is an estimate with
the fee terms of admin_margin_estimate, never an invoice or a payout.
"""

from collections import Counter
from fractions import Fraction
import math

from sqlalchemy import func, select

from .credit_schema import accounts, entries
from .credits_pricing import DEFAULT_CONFIGURATION, PricingError, current, fee_terms
from .payment_schema import PaymentSchemaError, payments, ready
from .payment_tax import receipt_money

PERIODS = frozenset({7, 30, 90})


def _ready(connection):
    try:
        return ready(connection)
    except PaymentSchemaError:
        return False


def _fees(connection, tenant):
    try:
        price = current(connection, tenant)
    except PricingError:
        price = {"configuration": DEFAULT_CONFIGURATION}
    return fee_terms(price)


def _block(connection, rows, first_paid, start, fee, fixed):
    gross = tax = refunded = 0
    net = Fraction(0)
    disputed = open_disputes = conflicts = 0
    packs, users = Counter(), set()
    credits = 0
    for row in rows:
        try:
            money = receipt_money(connection, row)
        except ValueError:
            # Keep the order visible at its stored price; never drop it.
            conflicts += 1
            money = {
                "amount_cents": row["amount_cents"],
                "refunded_cents": row["refunded_cents"],
            }
        gross += money["amount_cents"]
        tax += money["amount_cents"] - row["amount_cents"]
        refunded += money["refunded_cents"]
        packs[row["pack_id"]] += 1
        users.add(row["user_id"])
        credits += row["credits"]
        disputed += row["stripe_dispute_id"] is not None
        open_disputes += row["dispute_open"]
        charge = money["amount_cents"] * fee + fixed * 100
        # Tax is passed on, refunds go back, and an open dispute holds the rest.
        kept = 0 if row["dispute_open"] else row["amount_cents"] - row["refunded_cents"]
        net += kept - charge
    return {
        "orders": len(rows),
        "orders_by_pack": dict(sorted(packs.items())),
        "gross_cents": gross,
        "tax_cents": tax,
        "refunded_cents": refunded,
        "disputed_count": disputed,
        "open_disputes": open_disputes,
        "net_estimate_cents": math.floor(net),
        "paying_users": len(users),
        "first_time_payers": sum(
            1 for user in users if first_paid.get(user, start) >= start
        ),
        "credits_sold": credits,
        "receipt_conflicts": conflicts,
    }


def _credits(connection, tenant, start, now):
    from .credits import _aggregate_integer

    used = connection.execute(
        select(func.coalesce(-func.sum(entries.c.amount), 0)).where(
            entries.c.tenant == tenant,
            entries.c.kind == "capture",
            entries.c.created_at >= start,
            entries.c.created_at <= now,
        )
    ).scalar_one()
    outstanding = connection.execute(
        select(func.coalesce(func.sum(accounts.c.balance), 0)).where(
            accounts.c.tenant == tenant
        )
    ).scalar_one()
    return {
        "credits_used": _aggregate_integer(used),
        # Every customer balance, free grants included: the credits still owed.
        "credits_outstanding": _aggregate_integer(outstanding),
        "outstanding_includes_granted_credits": True,
    }


def report(connection, tenant, now, days):
    if type(days) is not int or days not in PERIODS:
        raise PricingError("revenue_period_invalid", "Choose 7, 30 or 90 days.")
    start = now - days * 86400
    fee, fixed = _fees(connection, tenant)
    result = {
        "days": days,
        "period_start": start,
        "period_end": now,
        "basis": "orders_paid_in_period",
        "currency": "usd",
        "payments_ready": _ready(connection),
        "live": None,
        "test": None,
        "net_estimate": {
            "estimated": True,
            "fee_rate": float(fee),
            "fixed_fee_usd_per_order": float(fixed),
            "source": "admin_margin_estimate_fee_terms_not_actual_payout",
        },
        **_credits(connection, tenant, start, now),
        # No measured provider cost is joined to revenue yet.
        "provider_cost_usd": None,
    }
    if not result["payments_ready"]:
        return result
    first_paid = {}
    for row in connection.execute(
        select(
            payments.c.livemode,
            payments.c.user_id,
            func.min(payments.c.paid_at).label("first"),
        )
        .where(payments.c.tenant == tenant, payments.c.paid_at.is_not(None))
        .group_by(payments.c.livemode, payments.c.user_id)
    ).mappings():
        first_paid[(row["livemode"], row["user_id"])] = row["first"]
    paid = [
        dict(row)
        for row in connection.execute(
            select(payments)
            .where(
                payments.c.tenant == tenant,
                payments.c.paid_at.is_not(None),
                payments.c.paid_at >= start,
                payments.c.paid_at <= now,
            )
            .order_by(payments.c.paid_at, payments.c.id)
        ).mappings()
    ]
    for mode, name in ((1, "live"), (0, "test")):
        result[name] = _block(
            connection,
            [row for row in paid if row["livemode"] == mode],
            {
                user: first
                for (livemode, user), first in first_paid.items()
                if livemode == mode
            },
            start,
            fee,
            fixed,
        )
    return result
