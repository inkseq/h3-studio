"""Tax-exclusive settlement facts; original signed event DTOs stay unchanged.

The frozen pack price is base; provider totals retain actual customer tax.
"""


def _get(value, key, default=None):
    try:
        return value[key]
    except (KeyError, TypeError):
        return default


def _amount(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= (1 << 63) - 1:
        raise ValueError("payment_tax_proof_invalid")
    return value


def session_basis(session):
    """Derive the immutable credit price from the actual one-item Price.

    Archived inclusive Prices remain valid settlement evidence for already
    accepted purchases. Only new checkout admission requires exclusive Prices.
    """
    lines = _get(session, "line_items", {})
    items = _get(lines, "data", [])
    if (
        _get(lines, "has_more") is not False
        or not isinstance(items, list)
        or len(items) != 1
    ):
        raise ValueError("payment_tax_proof_invalid")
    item = items[0]
    price = _get(item, "price", {})
    if _get(item, "quantity") != 1 or _get(price, "currency") != "usd":
        raise ValueError("payment_tax_proof_invalid")
    behavior = _get(price, "tax_behavior")
    if behavior not in {"inclusive", "exclusive"}:
        raise ValueError("payment_tax_proof_invalid")
    base = _amount(_get(price, "unit_amount"), positive=True)
    gross = _amount(_get(session, "amount_total"), positive=True)
    details = _get(session, "total_details", {})
    tax = _amount(_get(details, "amount_tax", 0))
    if (
        _get(details, "amount_discount", 0) != 0
        or _get(details, "amount_shipping", 0) != 0
        or behavior == "exclusive"
        and (
            _amount(_get(session, "amount_subtotal"), positive=True) != base
            or gross != base + tax
        )
        or behavior == "inclusive"
        and (gross != base or tax > gross)
    ):
        raise ValueError("payment_tax_proof_invalid")
    return {
        "base_amount_cents": base,
        "tax_amount_cents": tax,
        "tax_behavior": behavior,
    }


def original_price_amount(obj):
    """Optional authoritative expansion; no expansion keeps legacy semantics."""
    fields = {"base_amount_cents", "tax_amount_cents", "tax_behavior"}
    present = fields & set(obj)
    if not present:
        return obj.get("amount_cents")
    if present != fields:
        raise ValueError("payment_tax_proof_invalid")
    base = _amount(obj["base_amount_cents"], positive=True)
    gross = _amount(obj.get("amount_cents"), positive=True)
    tax = _amount(obj["tax_amount_cents"])
    behavior = obj["tax_behavior"]
    if (
        behavior == "exclusive"
        and gross == base + tax
        or behavior == "inclusive"
        and gross == base
        and tax <= gross
    ):
        return base
    raise ValueError("payment_tax_proof_invalid")


def refund_base_amount(obj):
    """No invented allocation for exclusive tax-only/partial refunds.

    A full gross refund reverses the complete original pack, bonus included.
    Partial exclusive refunds need explicit reviewed line/tax allocation.
    The caller retains the immutable event in review without wallet writes.
    """
    base = original_price_amount(obj)
    refunded = _amount(obj.get("amount_refunded"))
    if obj.get("tax_behavior") != "exclusive":
        return refunded
    if refunded == 0:
        return 0
    if refunded == obj["amount_cents"]:
        return base
    raise ValueError("payment_refund_tax_allocation_required")


def receipt_money(connection, row):
    """Project gross facts from existing processed signed payment events.

    No raw event or fingerprint is rewritten. A successful core transition is
    the authority that the original paid gross belongs to this frozen pack.
    """
    from sqlalchemy import select
    from studio_platform.payment_schema import events

    payloads = connection.execute(
        select(events.c.verified_payload).where(
            events.c.tenant == row["tenant"],
            events.c.account_scope == row["account_scope"],
            events.c.livemode == row["livemode"],
            events.c.payment_id == row["id"],
            events.c.status == "processed",
            events.c.type.in_(
                (
                    "checkout.session.completed",
                    "checkout.session.async_payment_succeeded",
                    "charge.refunded",
                )
            ),
        )
    ).scalars()
    gross_values, refund_values = set(), [0]
    for payload in payloads:
        obj = payload["object"]
        if payload["type"].startswith("checkout.session.") and obj.get(
            "payment_status"
        ) in {"unpaid", "no_payment_required"}:
            continue
        if obj.get("currency") != row["currency"]:
            raise ValueError("payment_receipt_money_conflict")
        gross = _amount(obj.get("amount_cents"), positive=True)
        if gross < row["amount_cents"]:
            raise ValueError("payment_receipt_money_conflict")
        gross_values.add(gross)
        if payload["type"] == "charge.refunded":
            refund_values.append(_amount(obj.get("amount_refunded")))
    if len(gross_values) > 1:
        raise ValueError("payment_receipt_money_conflict")
    gross = next(iter(gross_values), row["amount_cents"])
    refunded = max(refund_values) if gross_values else row["refunded_cents"]
    if refunded > gross:
        raise ValueError("payment_receipt_money_conflict")
    return {
        "amount_cents": gross,
        "refunded_cents": refunded,
        **(
            {
                "base_amount_cents": row["amount_cents"],
                "tax_amount_cents": gross - row["amount_cents"],
            }
            if gross > row["amount_cents"]
            else {}
        ),
    }
