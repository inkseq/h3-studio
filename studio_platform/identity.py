"""Read-only account authority bridge; never creates or backfills users.

Until explicit adoption, the original operator allowlists retain their exact
meaning. Once the canonical table exists, its rows are the only authority.
"""

from dataclasses import replace
import hashlib

from sqlalchemy import inspect, select, text

from .identity_schema import users, admin_audit


def _schema_state(connection):
    inspector = inspect(connection)
    present = (inspector.has_table(users.name), inspector.has_table(admin_audit.name))
    if any(present):
        return "canonical" if all(present) else "partial"
    from .schema_version import (
        IDENTITY_REVISIONS,
        SchemaVersionError,
        read_schema_version,
    )

    try:
        revision = read_schema_version(connection)
    except SchemaVersionError:
        return "partial"
    return "partial" if revision in IDENTITY_REVISIONS else "legacy"


def lock_account_gate(connection, tenant=None, *, exclusive=False):
    """Serialize future status writes with admission before any job/worker locks.

    This uses PostgreSQL's existing transaction lock facility; it creates no
    table or background coordinator. SQLite callers already hold the repository
    transaction's write reservation. Acquire even before adoption so creating
    the tables and suspending an account cannot race an unfenced earlier claim.
    Legacy authorization fallback is independent of this serialization gate.
    """
    if connection.dialect.name == "postgresql":
        # Worker candidates span owners/tenants. Every admission and future
        # account writer uses this same global gate, before existing locks.
        # It serializes authority changes, never grants queue/tenant authority.
        key = int.from_bytes(
            hashlib.sha256(b"inkseq-account-authority").digest()[:8],
            "big",
            signed=True,
        )
        function = (
            "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        )
        connection.execute(text("SELECT " + function + "(:key)"), {"key": key})
    return True


def account_identity(connection, tenant, owner):
    """Return None only for the pre-adoption schema, otherwise an exact row."""
    state = _schema_state(connection)
    if state == "legacy":
        return None
    if state != "canonical":
        return {"status": "unavailable"}
    row = (
        connection.execute(
            select(users.c.id, users.c.owner, users.c.role, users.c.status).where(
                users.c.tenant == tenant, users.c.owner == owner
            )
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or row["role"] not in {"user", "admin"}
        or row["status"] not in {"active", "suspended"}
    ):
        return {"status": "unavailable"}
    return dict(row)


def account_status(connection, tenant, owner):
    identity = account_identity(connection, tenant, owner)
    return "active" if identity is None else identity["status"]


def account_active(connection, tenant, owner):
    return account_status(connection, tenant, owner) == "active"


def hydrate(principal, connection, tenant):
    """Re-read identity on every authentication; machine grants stay narrow."""
    identity = account_identity(connection, tenant, principal.owner)
    if identity is None:
        return replace(principal, identity_source="legacy")
    if identity["status"] != "active":
        return None
    return replace(
        principal,
        user_id=identity["id"],
        role="user" if principal.machine else identity["role"],
        status=identity["status"],
        identity_source="canonical",
    )


def is_admin(principal, repository, settings, *, legacy_capacity=False):
    if principal is None or principal.machine:
        return False
    with repository.engine.connect() as connection:
        identity = account_identity(connection, settings.tenant_id, principal.owner)
    if identity is not None:
        return identity.get("status") == "active" and identity.get("role") == "admin"
    field = "operator_capacity_owners" if legacy_capacity else "operator_admin_owners"
    return principal.owner in getattr(settings, field, ())


def capacity_owner_known(repository, settings, owner):
    """Validate configured target ownership without granting operator powers."""
    with repository.engine.connect() as connection:
        identity = account_identity(connection, settings.tenant_id, owner)
    return (
        identity.get("status") in {"active", "suspended"}
        if identity is not None
        else owner in getattr(settings, "operator_capacity_owners", ())
    )


def operator_owners(repository, settings):
    """Admin read projection includes users whose machines outlive a role change."""
    with repository.engine.connect() as connection:
        state = _schema_state(connection)
        if state == "legacy":
            return set(getattr(settings, "operator_capacity_owners", ()))
        if state != "canonical":
            return set()
        return set(
            connection.execute(
                select(users.c.owner).where(users.c.tenant == settings.tenant_id)
            ).scalars()
        )


def require_admin(
    principal,
    repository,
    settings,
    *,
    legacy_capacity=False,
    legacy_code="operator_admin_required",
    machine_code=None,
):
    """One human-admin guard, with a bounded pre-adoption compatibility path."""
    from .operator_capacity import OperatorError

    if principal is None:
        raise OperatorError("operator_login_required", 401)
    with repository.engine.connect() as connection:
        identity = account_identity(connection, settings.tenant_id, principal.owner)
    if identity is not None:
        if (
            principal.machine
            or identity.get("status") != "active"
            or identity.get("role") != "admin"
        ):
            raise OperatorError("admin_required", 403)
        return principal.owner
    field = "operator_capacity_owners" if legacy_capacity else "operator_admin_owners"
    if principal.machine or principal.owner not in getattr(settings, field, ()):
        raise OperatorError(
            machine_code if principal.machine and machine_code else legacy_code, 403
        )
    return principal.owner


def public_identity(principal, repository, settings):
    """Account profile fields; dollar reservations are not product credits."""
    with repository.engine.connect() as connection:
        current = hydrate(principal, connection, settings.tenant_id)
    if current is None:
        return {"role": "user", "status": "suspended", "user_id": None}
    role = current.role
    if current.identity_source == "legacy" and not current.machine:
        configured = set(getattr(settings, "operator_capacity_owners", ())) | set(
            getattr(settings, "operator_admin_owners", ())
        )
        role = "admin" if current.owner in configured else "user"
    return {"role": role, "status": current.status, "user_id": current.user_id}
