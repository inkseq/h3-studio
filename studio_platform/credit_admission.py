"""Read-only recognition of the preview 007 reservation shape; no adoption."""

from sqlalchemy import inspect

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
