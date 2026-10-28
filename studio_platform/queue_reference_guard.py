"""One heavy reference generation per owner, using original transaction fences."""

import hashlib
import json

from sqlalchemy import select, text

from .repository import jobs

ACTIVE = (
    "claimed",
    "submitting",
    "running",
    "submission_unknown",
    "collecting",
    "cancel_requested",
    "recovery_hold",
)


def columns():
    """Scalar projection avoids loading prompts and media snapshots for sorting."""
    return (
        jobs.c.request["output_spec"]["width"].as_integer().label("guard_width"),
        jobs.c.request["output_spec"]["height"].as_integer().label("guard_height"),
        jobs.c.request["request"]["video_conditioning"]
        .as_string()
        .label("guard_conditioning"),
        jobs.c.request["request"]["inputs"]["videos"][0]
        .as_string()
        .label("guard_video"),
    )


def heavy(row):
    width, height = row["guard_width"], row["guard_height"]
    return (
        type(width) is int
        and type(height) is int
        and min(width, height) > 480
        and row["guard_conditioning"] != "denoise"
        and (bool(row["guard_video"]) or row["guard_conditioning"] == "in_context")
    )


def active_owners(connection, *, tenant=None, owner=None):
    statement = select(jobs.c.tenant_id, jobs.c.owner_id, *columns()).where(
        jobs.c.status.in_(ACTIVE)
    )
    if tenant is not None:
        statement = statement.where(
            jobs.c.tenant_id == tenant, jobs.c.owner_id == owner
        )
    return {
        (row["tenant_id"], row["owner_id"])
        for row in connection.execute(statement).mappings()
        if heavy(row)
    }


def eligible(connection, candidates):
    occupied = active_owners(connection)
    return [
        row
        for row in candidates
        if (row["tenant_id"], row["owner_id"]) not in occupied or not heavy(row)
    ]


def blocked_at_claim(connection, candidate):
    if not heavy(candidate):
        return False
    tenant, owner = candidate["tenant_id"], candidate["owner_id"]
    if connection.dialect.name == "postgresql":
        # Nonblocking, transaction-scoped and tenant-qualified. No owner lock
        # wait while holding a pool/job lock; concurrent pools recheck after it.
        key = int.from_bytes(
            hashlib.sha256(
                json.dumps(
                    ["sharp-reference-claim", tenant, owner], separators=(",", ":")
                ).encode()
            ).digest()[:8],
            "big",
            signed=True,
        )
        if not connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
        ):
            return True
    return (tenant, owner) in active_owners(connection, tenant=tenant, owner=owner)
