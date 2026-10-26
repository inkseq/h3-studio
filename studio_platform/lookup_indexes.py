"""Closed additive index catalog; no startup DDL or business-row writes."""

from sqlalchemy import inspect

INDEXES = (
    ("platform_outbox", "ix_outbox_event_aggregate", ("event_type", "aggregate_id")),
    ("platform_outbox", "ix_outbox_aggregate", ("aggregate_id",)),
    (
        "platform_budget_reservations",
        "ix_budget_reference",
        ("reference_type", "reference_id"),
    ),
    (
        "platform_scaler_receipts",
        "ix_scaler_intent_operation",
        ("intent_id", "operation"),
    ),
)


def equivalent(row, columns):
    options = {
        k: v
        for k, v in row.get("dialect_options", {}).items()
        if v not in (None, [], {})
    }
    return (
        tuple(row.get("column_names", ())) == columns
        and not row.get("unique")
        and not row.get("duplicates_constraint")
        and not row.get("column_sorting")
        and not options
    )


def plan(connection, *, schema=None):
    """Recognize actual definitions, not IF NOT EXISTS / name-only guesses."""
    from .schema_baseline import BaselineError

    inspector = inspect(connection)
    result = []
    for table, name, columns in INDEXES:
        rows = inspector.get_indexes(table, schema=schema)
        if any(row["name"] == name and not equivalent(row, columns) for row in rows):
            raise BaselineError("lookup_index_name_conflict")
        matches = sorted(row["name"] for row in rows if equivalent(row, columns))
        result.append(
            dict(table=table, name=name, columns=list(columns), existing=matches)
        )
    return result


def expected_indexes(expected, actual, *, required):
    """007 rollback readers tolerate ONLY these exact additive shapes.

    Keep actual names in the fingerprint. An equivalent pre-existing differently
    named index is reused; unknown/partial/expression/unique indexes still drift.
    """
    from .schema_baseline import BaselineError

    for table, name, columns in INDEXES:
        rows = actual.get(table, {}).get("indexes", [])
        matches = [
            row
            for row in rows
            if row["columns"] == list(columns)
            and not row["unique"]
            and not row["options"]
        ]
        if required and not matches:
            raise BaselineError("lookup_indexes_missing_or_changed")
        expected[table]["indexes"].extend(matches)
        expected[table]["indexes"].sort(key=lambda row: row["name"])


def adopt(connection, *, expected_fingerprint, tenant_id):
    """Explicit 007->008, existing account/migration locks, all-or-nothing DDL.

    Host operator must first qualify both retained reader images under release.lock.
    Source merge, application startup and CI never call this function.
    """
    import re
    from sqlalchemy import or_, select
    from alembic import command
    from .schema_baseline import BaselineError, alembic_config, compare_schema
    from .schema_version import CREDIT_ADMISSION_REVISION, LOOKUP_INDEX_REVISION
    from .identity import lock_account_gate
    from .credits import credit_policy
    from .repository import jobs

    if (
        connection.dialect.name != "postgresql"
        or not connection.in_transaction()
        or not isinstance(expected_fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint)
        or not isinstance(tenant_id, str)
        or not 1 <= len(tenant_id) <= 200
    ):
        raise BaselineError("lookup_index_verified_transaction_required")
    connection.exec_driver_sql("SET LOCAL lock_timeout = '1s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '3s'")
    lock_account_gate(connection, exclusive=True)
    connection.exec_driver_sql("SELECT pg_advisory_xact_lock(685939796868749721)")
    # Acquire together in fixed order before the fingerprint; no later writes
    # can race index adoption. Timeout rolls back the entire original transaction.
    connection.exec_driver_sql(
        "LOCK TABLE platform_budget_reservations, platform_outbox, "
        "platform_scaler_receipts IN SHARE MODE"
    )
    before = compare_schema(connection)
    if before["schema_fingerprint"] != expected_fingerprint:
        raise BaselineError("schema_snapshot_changed")
    if not before["matches"] or before["recorded_revision"] not in {
        CREDIT_ADMISSION_REVISION,
        LOOKUP_INDEX_REVISION,
    }:
        raise BaselineError("lookup_indexes_require_adopted007")
    if credit_policy(connection, tenant_id)["enabled"]:
        raise BaselineError("lookup_indexes_require_charging_off")
    if connection.execute(
        select(jobs.c.id)
        .where(
            or_(
                jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
                jobs.c.lease_worker_id.is_not(None),
                jobs.c.lease_expires_at.is_not(None),
            )
        )
        .limit(1)
    ).first():
        raise BaselineError("lookup_indexes_require_quiescence")
    planned = plan(connection)
    if before["recorded_revision"] == LOOKUP_INDEX_REVISION:
        return dict(
            state="already_adopted", before=before, after=before, indexes=planned
        )
    config = alembic_config()
    config.attributes.update(
        verified_baseline_connection=connection,
        verified_baseline_mode="upgrade",
        verified_lookup_indexes=True,
    )
    command.upgrade(config, LOOKUP_INDEX_REVISION)
    after = compare_schema(connection)
    if not after["matches"] or after["recorded_revision"] != LOOKUP_INDEX_REVISION:
        raise BaselineError("lookup_index_adoption_verification_failed")
    return dict(
        state="adopted_lookup_indexes", before=before, after=after, indexes=planned
    )
