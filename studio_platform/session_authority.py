"""Private session epochs in the existing document store; no provider I/O."""

from sqlalchemy import and_, insert, select, update

from .repository import documents

OWNER = PROJECT = "__session_authority"


def where(tenant, kind, ident):
    return and_(
        documents.c.tenant_id == tenant,
        documents.c.owner_id == OWNER,
        documents.c.project_id == PROJECT,
        documents.c.kind == kind,
        documents.c.document_id == ident,
    )


def read(connection, tenant, kind, ident):
    return connection.execute(
        select(documents.c.payload).where(where(tenant, kind, ident))
    ).scalar_one_or_none()


def write(connection, tenant, kind, ident, value, now):
    found = connection.execute(
        update(documents)
        .where(where(tenant, kind, ident))
        .values(payload=value, version=documents.c.version + 1, updated_at=now)
    )
    if not found.rowcount:
        connection.execute(
            insert(documents).values(
                tenant_id=tenant,
                owner_id=OWNER,
                project_id=PROJECT,
                kind=kind,
                document_id=ident,
                payload=value,
                version=1,
                updated_at=now,
            )
        )


def epoch(connection, tenant):
    return (read(connection, tenant, "epoch", "current") or {"value": 0})["value"]


def assert_epoch(connection, tenant, owner, captured):
    from .auth import AuthenticationError

    cutoff = (read(connection, tenant, "cutoff", owner) or {"epoch": 0})["epoch"]
    if type(captured) is not int or captured < cutoff:
        raise AuthenticationError("The sign-in changed. Please sign in again.")


def advance(connection, tenant, owner, now):
    """Caller holds the exclusive account gate before any session locks."""
    value = epoch(connection, tenant) + 1
    write(connection, tenant, "epoch", "current", {"value": value}, now)
    write(connection, tenant, "cutoff", owner, {"epoch": value}, now)
    return value
