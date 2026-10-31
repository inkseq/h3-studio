"""Credit expiry inside the caller's existing account transaction and row lock.

Existing documents hold lot state and immutable per-entry allocations. Existing
credit entries remain the balance ledger; no schema, worker or provider is added.
"""

import copy

from sqlalchemy import exists, insert, or_, select, update

from . import credit_lots as lots
from .credit_schema import entries
from .repository import documents, jobs

OWNER = "__credits_expiry"
PROJECT = "__credits"
STATE = "credit_lot_state"
ALLOCATION = "credit_lot_allocation"


def safe(function, *args, **kwargs):
    from .credits import CreditError

    try:
        return function(*args, **kwargs)
    except (lots.LotError, KeyError, TypeError) as error:
        raise CreditError("credit_lot_evidence_invalid", 503) from error


def check_adoption(connection, tenant, old, new, now):
    if "expiry_policy" not in new or "expiry_policy" in old:
        return
    from .credits_pricing import PricingError

    job = connection.execute(
        select(jobs.c.id)
        .where(
            jobs.c.tenant_id == tenant,
            or_(
                jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
                jobs.c.lease_expires_at > now,
            ),
        )
        .limit(1)
    ).first()
    terminal = entries.alias("expiry_service_terminal")
    image_hold = connection.execute(
        select(entries.c.id)
        .where(
            entries.c.tenant == tenant,
            entries.c.kind == "hold",
            entries.c.reference_type == "story_chat_image",
            ~exists(
                select(terminal.c.id).where(
                    terminal.c.tenant == entries.c.tenant,
                    terminal.c.user_id == entries.c.user_id,
                    terminal.c.reference_type == entries.c.reference_type,
                    terminal.c.reference_id == entries.c.reference_id,
                    terminal.c.kind.in_(("capture", "release")),
                )
            ),
        )
        .limit(1)
    ).first()
    if job or image_hold:
        raise PricingError(
            "credit_expiry_adoption_busy",
            "Wait for accepted work before expiry adoption.",
            409,
        )


def where(row, kind, ident):
    return (
        documents.c.tenant_id == row["tenant"],
        documents.c.owner_id == OWNER,
        documents.c.project_id == PROJECT,
        documents.c.kind == kind,
        documents.c.document_id == row["user_id"] + ":" + ident,
    )


def read(connection, row, kind=STATE, ident="active"):
    found = (
        connection.execute(select(documents).where(*where(row, kind, ident)))
        .mappings()
        .first()
    )
    return dict(found) if found else None


def save(connection, row, payload, now, *, old=None, kind=STATE, ident="active"):
    value = dict(
        payload=copy.deepcopy(payload),
        updated_at=now,
        version=1 if old is None else old["version"] + 1,
    )
    if old is None:
        connection.execute(
            insert(documents).values(
                tenant_id=row["tenant"],
                owner_id=OWNER,
                project_id=PROJECT,
                kind=kind,
                document_id=row["user_id"] + ":" + ident,
                **value,
            )
        )
    else:
        changed = connection.execute(
            update(documents)
            .where(*where(row, kind, ident), documents.c.version == old["version"])
            .values(**value)
        )
        lots.require(changed.rowcount == 1)


def active_policy(connection, row):
    from .credits import credit_policy
    from .credit_schema import prices
    from .credits_pricing import configuration

    revision = credit_policy(connection, row["tenant"])["active_revision"]
    config = connection.execute(
        select(prices.c.configuration).where(
            prices.c.tenant == row["tenant"], prices.c.revision == revision
        )
    ).scalar_one_or_none()
    # Accepted legacy settlements need no new pricing authority. Only an
    # actually published expiry contract opts an untracked account into lots.
    if config is None or "expiry_policy" not in config:
        return False
    return configuration(config)["expiry_policy"] == lots.POLICY


def baseline(connection, row):
    previous = list(
        connection.execute(
            select(entries).where(
                entries.c.tenant == row["tenant"], entries.c.user_id == row["user_id"]
            )
        ).mappings()
    )
    terminal = {
        lots.reference(e) for e in previous if e["kind"] in {"capture", "release"}
    }
    held = {}
    for entry in previous:
        key = lots.reference(entry)
        if entry["kind"] == "hold" and key not in terminal and entry["held_delta"]:
            lots.require(key not in held)
            held[key] = [{"lot": lots.LEGACY, "amount": entry["held_delta"]}]
    state = lots.initial(row, held)
    lots.validate(state, row)
    return {
        "state": state,
        "legacy_captures": [e["id"] for e in previous if e["kind"] == "capture"],
    }


def state_document(connection, row, now, *, create=False):
    old = read(connection, row)
    if old is None and create and active_policy(connection, row):
        save(connection, row, baseline(connection, row), now)
        old = read(connection, row)
    if old is not None:
        payload = old["payload"]
        lots.require(
            type(payload) is dict
            and set(payload) == {"state", "legacy_captures"}
            and type(payload["legacy_captures"]) is list
        )
        lots.validate(payload["state"], row)
    return old


def expire_due(connection, row, now, *, create=False):
    """Never called by GET. Held credit stays spendable by its accepted work."""
    from .credits import _change

    old = state_document(connection, row, now, create=create)
    if old is None:
        return row
    for ident, lot in old["payload"]["state"]["lots"].items():
        amount = lot["remaining"] - lot["held"]
        if lot["expires_at"] is None or lot["expires_at"] > now or not amount:
            continue
        result = _change(
            connection,
            row,
            kind="expire",
            amount=-amount,
            held_delta=0,
            reference_type="credit_lot",
            reference_id=ident,
            actor="system:credit-expiry",
            reason="Unused credits reached their original expiry",
            now=now,
            idempotency_key=f"expire:{ident}:{row['version']}",
        )
        row = {**row, **result["account"], "updated_at": now}
    return row


def restoration(connection, row, entry, payload):
    captured = list(
        connection.execute(
            select(entries).where(
                entries.c.tenant == row["tenant"],
                entries.c.user_id == row["user_id"],
                entries.c.reference_type == entry["reference_type"],
                entries.c.reference_id == entry["reference_id"],
                entries.c.kind == "capture",
            )
        ).mappings()
    )
    lots.require(len(captured) == 1)
    original = captured[0]
    lots.require(-original["amount"] == entry["amount"])
    allocated = read(connection, row, ALLOCATION, original["id"])
    if allocated is None:
        lots.require(original["id"] in payload["legacy_captures"])
        return [{"lot": lots.LEGACY, "amount": entry["amount"]}]
    lots.require(allocated["payload"]["entry_fingerprint"] == original["fingerprint"])
    return copy.deepcopy(allocated["payload"]["uses"])


def record(connection, before, entry, *, issued_at=None):
    old = state_document(connection, before, entry["created_at"])
    if old is None:
        return
    payload = copy.deepcopy(old["payload"])
    restored = (
        restoration(connection, before, entry, payload)
        if entry["kind"] == "waive"
        else None
    )
    state, uses = lots.apply(
        payload["state"], before, entry, restored=restored, issued_at=issued_at
    )
    payload["state"] = state
    save(connection, before, payload, entry["created_at"], old=old)
    save(
        connection,
        before,
        {"entry_fingerprint": entry["fingerprint"], "uses": uses},
        entry["created_at"],
        kind=ALLOCATION,
        ident=entry["id"],
    )


def public_projection(connection, row, now):
    old = state_document(connection, row, now)
    if old is None:
        return None
    return lots.projection(old["payload"]["state"], row, now)


def refundable_purchase(connection, row, payment_id, now):
    """Expired or consumed original packs cannot claw back another purchase."""
    old = state_document(connection, row, now)
    if old is None:
        return row["balance"] - row["held"]
    state = old["payload"]["state"]
    found = [
        v
        for v in state["lots"].values()
        if (v["source_type"], v["source_id"]) == ("payment", payment_id)
    ]
    if not found:
        # Pre-adoption payments can reclaim only the grandfathered balance;
        # they cannot consume unrelated purchases issued after adoption.
        return (
            state["lots"][lots.LEGACY]["remaining"] - state["lots"][lots.LEGACY]["held"]
        )
    return sum(
        v["remaining"] - v["held"]
        for v in found
        if v["expires_at"] is None or v["expires_at"] > now
    )
