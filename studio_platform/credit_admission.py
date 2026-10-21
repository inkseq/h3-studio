"""Trusted roles on original reservations; strict readers and no startup DDL."""

from sqlalchemy import inspect, select, text

from .schema_version import CREDIT_ADMISSION_REVISION, read_schema_version


def ready(connection, *, schema=None):
    from .credits import CreditError

    marker = read_schema_version(connection, schema=schema)
    columns = inspect(connection).get_columns(
        "platform_budget_reservations", schema=schema
    )
    role = next((row for row in columns if row["name"] == "budget_role"), None)
    if marker != CREDIT_ADMISSION_REVISION:
        if role is not None:
            raise CreditError("credit_admission_schema_requires_review", 409)
        return False
    if (
        role is None
        or str(role["type"]).upper() != "VARCHAR(20)"
        or role["nullable"]
        or role.get("default") not in {"'enforced'", "'enforced'::character varying"}
        or role.get("computed")
        or role.get("identity")
    ):
        raise CreditError("credit_admission_schema_requires_review", 409)
    checks = inspect(connection).get_check_constraints(
        "platform_budget_reservations", schema=schema
    )
    from .migrations.versions.v20261022_0007_credit_admission import normalize_check

    roles = [row for row in checks if row["name"] == "ck_budget_reservation_role"]
    if (
        len(roles) != 1
        or roles[0].get("dialect_options")
        or normalize_check(roles[0]["sqltext"]) is None
    ):
        raise CreditError("credit_admission_schema_requires_review", 409)
    return True


def sqlite_revision(db, *, published=False):
    from .backup import BackupError
    from .schema_version import read_sqlite_schema_version, SchemaVersionError

    try:
        marker = read_sqlite_schema_version(db)
    except SchemaVersionError:
        raise BackupError("backup_schema_revision_requires_review") from None
    columns = list(db.execute('PRAGMA table_info("platform_budget_reservations")'))
    row = next((row for row in columns if row[1] == "budget_role"), None)
    if row is None:
        if marker == CREDIT_ADMISSION_REVISION:
            raise BackupError("credit_admission_schema_requires_review")
        return None
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='platform_budget_reservations'"
    ).fetchone()
    from .migrations.versions.v20261022_0007_credit_admission import sqlite_check

    if (
        marker != CREDIT_ADMISSION_REVISION
        and not (published and marker is None)
        or tuple(row[1:]) != ("budget_role", "VARCHAR(20)", 1, "'enforced'", 0)
        or sql is None
        or not sqlite_check(sql[0])
    ):
        raise BackupError("credit_admission_schema_requires_review")
    return CREDIT_ADMISSION_REVISION


def roles(connection, tenant, owner, execution):
    """Only selected, canonical owner rows can become accounting rows."""
    from .credits import CreditError
    from .repository import budget_accounts

    identifiers = execution.get("budget_account_ids", [])
    if (
        not isinstance(identifiers, (list, tuple))
        or any(not isinstance(item, str) for item in identifiers)
        or len(set(identifiers)) != len(identifiers)
    ):
        raise CreditError("credit_quote_invalid", 409)
    result = []
    for identifier in sorted(identifiers):
        row = (
            connection.execute(
                select(budget_accounts).where(budget_accounts.c.id == identifier)
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["tenant_id"] != tenant
            or row["owner_id"] not in {None, owner}
        ):
            raise CreditError("credit_quote_invalid", 409)
        owner_row = row["owner_id"] == owner and row["project_id"] is None
        result.append(
            {
                "account_id": identifier,
                "scope": "owner"
                if owner_row
                else "project"
                if row["project_id"] is not None
                else "shared",
                "budget_role": "accounting" if owner_row else "enforced",
            }
        )
    if sum(row["scope"] == "owner" for row in result) > 1:
        raise CreditError(
            "credit_budget_binding_requires_review",
            409,
            detail="The generation budget binding needs administrator review.",
        )
    return result


def validate(connection, scope, job, account_ids):
    """Recompute before first admission; accepted/idempotent jobs bypass this."""
    from .credits import CreditError, credit_policy
    from .credits_pricing import quote

    frozen = job["execution_plan"].get("credit_quote")
    adopted = ready(connection)
    if not adopted:
        if isinstance(frozen, dict) and frozen.get("version") == 2:
            raise CreditError("credit_admission_schema_required", 409)
        return {}
    enabled = credit_policy(connection, scope.tenant_id)["enabled"]
    if not enabled and not frozen:
        return {}
    if (
        not enabled
        or not isinstance(frozen, dict)
        or type(frozen.get("version")) is not int
        or sorted(account_ids)
        != sorted(job["execution_plan"].get("budget_account_ids", []))
    ):
        raise CreditError("credit_policy_changed_repreflight", 409)
    current = quote(
        connection,
        scope.tenant_id,
        scope.owner_id,
        job["request"],
        execution=job["execution_plan"],
    )
    if current != frozen:
        raise CreditError("credit_policy_changed_repreflight", 409)
    # Included work remains an ordinary USD admission. A zero credit hold is
    # never evidence authorizing an accounting-only USD exemption.
    if (
        frozen["version"] == 1
        and type(frozen.get("credits")) is int
        and frozen["credits"] == 0
    ):
        return {}
    if (
        frozen["version"] != 2
        or frozen.get("admission") != "credits"
        or type(frozen.get("credits")) is not int
        or frozen["credits"] <= 0
    ):
        raise CreditError("credit_policy_changed_repreflight", 409)
    return {row["account_id"]: row["budget_role"] for row in frozen["budget_rows"]}


def store_role(connection, reservation_id, role):
    if role not in {"accounting", "enforced"}:
        raise ValueError("invalid_budget_role")
    # The baseline Table deliberately stays 006-compatible. The adopted-only
    # column is written on the SAME inserted row, inside the SAME transaction.
    connection.execute(
        text("UPDATE platform_budget_reservations SET budget_role=:role WHERE id=:id"),
        {"role": role, "id": reservation_id},
    )


def require_hold(connection, scope, job, held, role_map):
    from .credits import CreditError
    from .credit_schema import entries

    frozen = job["execution_plan"].get("credit_quote")
    if not role_map and (not isinstance(frozen, dict) or frozen.get("version") != 2):
        return
    if "accounting" in role_map.values() and (
        not isinstance(frozen, dict)
        or type(frozen.get("credits")) is not int
        or frozen["credits"] <= 0
    ):
        raise CreditError("credit_policy_changed_repreflight", 409)
    row = (
        connection.execute(
            select(entries).where(
                entries.c.tenant == scope.tenant_id,
                entries.c.idempotency_key == "job:" + job["id"] + ":hold",
            )
        )
        .mappings()
        .first()
    )
    if (
        held is None
        or row is None
        or row["kind"] != "hold"
        or row["user_id"] != frozen["user_id"]
        or row["reference_type"] != "job"
        or row["reference_id"] != job["id"]
        or row["held_delta"] != frozen["credits"]
        or "accounting" in role_map.values()
        and (type(row["held_delta"]) is not int or row["held_delta"] <= 0)
        or row["price_revision"] != frozen["price_revision"]
    ):
        raise CreditError("credit_policy_changed_repreflight", 409)
