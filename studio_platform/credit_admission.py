"""Trusted admission roles on original reservations; no startup DDL."""

from sqlalchemy import inspect, select, text

from .schema_version import CREDIT_ADMISSION_REVISION, read_schema_version


def ready(connection):
    from .credits import CreditError

    marker = read_schema_version(connection)
    columns = inspect(connection).get_columns("platform_budget_reservations")
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
    checks = inspect(connection).get_check_constraints("platform_budget_reservations")
    from .migrations.versions.v20261022_0007_credit_admission import normalize_check

    roles = [row for row in checks if row["name"] == "ck_budget_reservation_role"]
    if len(roles) != 1 or normalize_check(roles[0]["sqltext"]) is None:
        raise CreditError("credit_admission_schema_requires_review", 409)
    return True


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
    return result


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
    from .migrations.versions.v20261022_0007_credit_admission import ROLE_CHECK
    import re

    compact = lambda value: re.sub(r"\s+", "", value).lower()
    if (
        marker != CREDIT_ADMISSION_REVISION
        and not (published and marker is None)
        or tuple(row[1:]) != ("budget_role", "VARCHAR(20)", 1, "'enforced'", 0)
        or sql is None
        or compact("CONSTRAINT ck_budget_reservation_role CHECK (" + ROLE_CHECK + ")")
        not in compact(sql[0])
    ):
        raise BackupError("credit_admission_schema_requires_review")
    return CREDIT_ADMISSION_REVISION


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
        or frozen.get("version") != 2
        or type(frozen.get("version")) is not int
        or frozen.get("admission") != "credits"
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
        or row["price_revision"] != frozen["price_revision"]
    ):
        raise CreditError("credit_policy_changed_repreflight", 409)
