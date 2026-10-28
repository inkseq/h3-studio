"""Pure credit-lot allocation; SQL/account locking stays in credit_expiry.

Inbound/outbound provenance follows the established prepaid-wallet pattern
(Lago wallet_transactions/track_consumption_service.rb), adapted to H3 holds.
No provider, clock, global state or automatic financial retry lives here.
"""

import calendar
import copy
from datetime import datetime, timezone
import math

POLICY = {"version": 1, "purchase_months": 12, "promotion_days": 30}
LEGACY = "legacy"


class LotError(ValueError):
    pass


def require(value):
    if not value:
        raise LotError("credit_lot_evidence_invalid")


def integer(value):
    return type(value) is int and 0 <= value <= (1 << 63) - 1


def clock(value):
    require(type(value) in (int, float) and math.isfinite(value) and value > 0)
    return value


def expiry(kind, now):
    now = clock(now)
    if kind == "purchase":
        start = datetime.fromtimestamp(now, timezone.utc)
        year = start.year + 1
        return start.replace(
            year=year, day=min(start.day, calendar.monthrange(year, start.month)[1])
        ).timestamp()
    return now + 30 * 86400 if kind == "grant" else None


def initial(row, holds=None):
    """Grandfather the actual existing balance and every already accepted hold."""
    return {
        "version": 1,
        "account_version": row["version"],
        "lots": {
            LEGACY: {
                "remaining": row["balance"],
                "held": row["held"],
                "expires_at": None,
                "created_at": row["created_at"],
                "source_type": "legacy",
                "source_id": row["user_id"],
            }
        },
        "holds": copy.deepcopy(holds or {}),
    }


def validate(state, row):
    require(
        type(state) is dict
        and set(state) == {"version", "account_version", "lots", "holds"}
    )
    require(
        type(state["version"]) is int
        and state["version"] == 1
        and type(state["account_version"]) is int
        and state["account_version"] == row["version"]
    )
    require(
        type(state["lots"]) is dict
        and LEGACY in state["lots"]
        and type(state["holds"]) is dict
    )
    for key, lot in state["lots"].items():
        require(
            isinstance(key, str)
            and type(lot) is dict
            and set(lot)
            == {
                "remaining",
                "held",
                "expires_at",
                "created_at",
                "source_type",
                "source_id",
            }
        )
        require(
            integer(lot["remaining"])
            and integer(lot["held"])
            and lot["held"] <= lot["remaining"]
        )
        clock(lot["created_at"])
        if lot["expires_at"] is not None:
            clock(lot["expires_at"])
        require(
            isinstance(lot["source_type"], str) and isinstance(lot["source_id"], str)
        )
    require(
        sum(x["remaining"] for x in state["lots"].values()) == row["balance"]
        and sum(x["held"] for x in state["lots"].values()) == row["held"]
    )
    mapped = {}
    for key, uses in state["holds"].items():
        require(isinstance(key, str) and type(uses) is list and uses)
        for use in uses:
            require(
                type(use) is dict
                and set(use) == {"lot", "amount"}
                and use["lot"] in state["lots"]
                and integer(use["amount"])
                and use["amount"] > 0
            )
            mapped[use["lot"]] = mapped.get(use["lot"], 0) + use["amount"]
    for key, lot in state["lots"].items():
        require(mapped.get(key, 0) == lot["held"])


def reference(entry):
    return entry["reference_type"] + ":" + entry["reference_id"]


def order(state):
    return sorted(
        state["lots"],
        key=lambda key: (
            state["lots"][key]["expires_at"] or math.inf,
            state["lots"][key]["created_at"],
            key,
        ),
    )


def allocate(state, amount, now, *, source=None):
    require(integer(amount))
    uses = []
    for key in order(state):
        lot = state["lots"][key]
        if source is not None and (lot["source_type"], lot["source_id"]) != source:
            continue
        if lot["expires_at"] is not None and lot["expires_at"] <= now:
            continue
        take = min(amount, lot["remaining"] - lot["held"])
        if take:
            uses.append({"lot": key, "amount": take})
            amount -= take
        if not amount:
            break
    require(amount == 0)
    return uses


def terminal_uses(state, entry):
    key, amount = reference(entry), -entry["held_delta"]
    uses = state["holds"].pop(key, None)
    if uses is None:
        require(amount == 0)
        uses = []
    require(sum(u["amount"] for u in uses) == amount)
    return uses


def apply(state, row, entry, *, restored=None, issued_at=None):
    validate(state, row)
    state = copy.deepcopy(state)
    kind, amount, delta, now = (
        entry[k] for k in ("kind", "amount", "held_delta", "created_at")
    )
    clock(now)
    issued_at = now if issued_at is None else clock(issued_at)
    require(issued_at <= now)
    uses = []
    if amount > 0:
        require(delta == 0)
        if kind == "waive":
            uses = (
                restored
                if restored is not None
                else [{"lot": LEGACY, "amount": amount}]
            )
            require(sum(u["amount"] for u in uses) == amount)
            for use in uses:
                require(use["lot"] in state["lots"] and integer(use["amount"]))
                state["lots"][use["lot"]]["remaining"] += use["amount"]
        else:
            require(
                kind in {"purchase", "grant", "refund", "adjust"}
                and entry["id"] not in state["lots"]
            )
            state["lots"][entry["id"]] = {
                "remaining": amount,
                "held": 0,
                "expires_at": expiry(kind, issued_at),
                "created_at": issued_at,
                "source_type": entry["reference_type"],
                "source_id": entry["reference_id"],
            }
    elif kind == "hold":
        require(amount == 0 and delta >= 0 and reference(entry) not in state["holds"])
        uses = allocate(state, delta, now)
        if uses:
            state["holds"][reference(entry)] = uses
        for use in uses:
            state["lots"][use["lot"]]["held"] += use["amount"]
    elif kind in {"capture", "release"}:
        require(delta <= 0 and amount == (delta if kind == "capture" else 0))
        uses = terminal_uses(state, entry)
        for use in uses:
            lot = state["lots"][use["lot"]]
            lot["held"] -= use["amount"]
            if kind == "capture":
                lot["remaining"] -= use["amount"]
    elif kind == "expire":
        require(delta == 0 and amount < 0 and entry["reference_type"] == "credit_lot")
        lot = state["lots"][entry["reference_id"]]
        require(
            lot["expires_at"] is not None
            and lot["expires_at"] <= now
            and -amount == lot["remaining"] - lot["held"]
        )
        lot["remaining"] += amount
        uses = [{"lot": entry["reference_id"], "amount": -amount}]
    else:
        require(kind in {"refund", "adjust"} and amount < 0 and delta == 0)
        source = (
            ("payment", entry["reference_id"])
            if kind == "refund" and entry["reference_type"] == "payment"
            else None
        )
        if source is not None and not any(
            (v["source_type"], v["source_id"]) == source for v in state["lots"].values()
        ):
            source = ("legacy", state["lots"][LEGACY]["source_id"])
        uses = allocate(state, -amount, now, source=source)
        for use in uses:
            state["lots"][use["lot"]]["remaining"] -= use["amount"]
    state["account_version"] += 1
    validate(
        state,
        {
            **row,
            "version": row["version"] + 1,
            "balance": row["balance"] + amount,
            "held": row["held"] + delta,
        },
    )
    return state, copy.deepcopy(uses)


def projection(state, row, now):
    validate(state, row)
    clock(now)
    expired, future = 0, {}
    for lot in state["lots"].values():
        free = lot["remaining"] - lot["held"]
        at = lot["expires_at"]
        if at is None or not free:
            continue
        if at <= now:
            expired += free
        else:
            future[at] = future.get(at, 0) + free
    upcoming = [{"at": at, "credits": future[at]} for at in sorted(future)[:20]]
    return {
        "expired_unspent": expired,
        "next_at": upcoming[0]["at"] if upcoming else None,
        "next_credits": upcoming[0]["credits"] if upcoming else 0,
        "upcoming": upcoming,
    }
