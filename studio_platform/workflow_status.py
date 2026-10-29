"""The only reads of DBOS's internal workflow_status table (#817).

They run inside the caller's business transaction (row locks on the original
job/film), which DBOS's own client cannot join. Every column used here is part
of the frozen DBOS 3.3.0 catalog that workflow_schema.validate() compares at
startup, so an SDK upgrade that changes this table stops at that check.
"""

from __future__ import annotations

from sqlalchemy import text

# Columns read below; a test binds them to the frozen catalog.
COLUMNS = frozenset(
    {
        "workflow_uuid",
        "status",
        "application_name",
        "application_version",
        "name",
        "created_at",
    }
)
OPEN = ("ENQUEUED", "PENDING", "DELAYED")
_MATCH = (
    "FROM inkseq_workflows.workflow_status "
    "WHERE (workflow_uuid=:id OR workflow_uuid LIKE :prefix) "
    "AND application_name=:application AND name=:name"
)


def _params(base, application, name, **extra):
    return {
        "id": base,
        "prefix": base + "/%",
        "application": application,
        "name": name,
        **extra,
    }


def open_ids(connection, base, application, name):
    """Up to two open (enqueued/pending/delayed) epochs of one original run."""
    return (
        connection.execute(
            text(
                "SELECT workflow_uuid "
                + _MATCH
                + " AND status IN ('ENQUEUED','PENDING','DELAYED') LIMIT 2"
            ),
            _params(base, application, name),
        )
        .scalars()
        .all()
    )


def latest_id(connection, base, application, name):
    """The newest epoch of one original run, or None if it never ran."""
    return connection.execute(
        text(
            "SELECT workflow_uuid "
            + _MATCH
            + " ORDER BY created_at DESC,workflow_uuid DESC LIMIT 1"
        ),
        _params(base, application, name),
    ).scalar()


def latest_status(connection, base, application, version, name):
    """Status of the newest epoch for one application version."""
    return connection.execute(
        text(
            "SELECT status " + _MATCH + " AND application_version=:version"
            " ORDER BY created_at DESC,workflow_uuid DESC LIMIT 1"
        ),
        _params(base, application, name, version=version),
    ).scalar()
