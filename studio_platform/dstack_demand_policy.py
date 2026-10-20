"""Read the tenant demand-start switch from the original operator policy table.

An absent row is OFF in every environment. Reading never creates policy, loads
credentials or contacts capacity providers; callers serialize mutations with
the existing account and capacity gates.
"""

from sqlalchemy import select

from .operator_capacity import operator_policy, require
from .repository import request_hash


def policy_key(tenant):
    return "demand:" + request_hash({"tenant_id": tenant})[:23]


def load_runtime(connection, tenant):
    """Return the closed public value and SQL version for an authorized writer."""
    row = (
        connection.execute(
            select(operator_policy).where(operator_policy.c.id == policy_key(tenant))
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"enabled": False, "revision": None}, 0
    payload, version = row["payload"], row["version"]
    require(
        isinstance(payload, dict)
        and set(payload) == {"tenant_id", "enabled"}
        and payload["tenant_id"] == tenant
        and type(payload["enabled"]) is bool
        and type(version) is int
        and version >= 1,
        "dstack_demand_policy_unavailable",
        503,
    )
    return {
        "enabled": payload["enabled"],
        "revision": request_hash({"version": version, "payload": payload}),
    }, version


def read_runtime(connection, tenant):
    return load_runtime(connection, tenant)[0]
