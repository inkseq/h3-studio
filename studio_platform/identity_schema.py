"""Frozen optional business identity tables; imports and readers never create DDL.

Password hashes, sessions and tokens stay in the existing auth metadata. User
profiles, role/status and admin actions belong in portable business backups.
"""

import re

from sqlalchemy import CheckConstraint, MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.schema import CreateTable

from .migrations.versions import v20261019_0003_account_roles as frozen

IDENTITY_REVISION = frozen.revision


class _Collector:
    def __init__(self):
        self.metadata = MetaData()

    def create_table(self, name, *columns, **kwargs):
        return Table(name, self.metadata, *columns, **kwargs)


def frozen_metadata():
    collector = _Collector()
    frozen.build(collector)
    return collector.metadata


metadata = frozen_metadata()
users = metadata.tables["platform_users"]
admin_audit = metadata.tables["platform_admin_audit"]
TABLE_NAMES = frozenset(metadata.tables)


class IdentitySchemaError(Exception):
    """A finite diagnostic without SQL, credentials or profile values."""


def present_tables(names):
    """A family is absent or complete; partial adoption never becomes authority."""
    present = TABLE_NAMES & set(names)
    if present and present != TABLE_NAMES:
        raise IdentitySchemaError("identity_schema_partial_family_requires_review")
    return bool(present)


def normalize_check(value):
    """Accept only the three frozen identity CHECKs, including PG deparsing."""
    parts = re.split(r"('(?:[^']|'')*')", str(value))
    for index in range(0, len(parts), 2):
        # Preserve quoted values byte-for-byte. Lowercasing or stripping casts,
        # spaces or parentheses inside a literal would accept a different enum.
        part = re.sub(r"\s+|[()]", "", parts[index]).lower()
        for cast in ("::charactervarying", "::text[]", "::text"):
            part = part.replace(cast, "")
        parts[index] = part
    compact = "".join(parts)
    for name, choices in (
        ("role", "'user','admin'"),
        ("status", "'active','suspended'"),
    ):
        if compact in {name + "in" + choices, name + "=anyarray[" + choices + "]"}:
            return [(name, "in", choices)]
    if compact == "email_verified>=0andemail_verified<=1":
        return [("email_verified", "<=", "1"), ("email_verified", ">=", "0")]
    raise IdentitySchemaError("identity_schema_check_requires_review")


def _default(value):
    value = str(value) if value is not None else None
    known = {"'user'": "user", "'active'": "active", "0": "0", "'0'": "0"}
    for cast in ("::character varying", "::integer"):
        if value is not None and value.endswith(cast):
            value = value[: -len(cast)]
    return known.get(value, value)


def validate_connection(connection, *, schema=None):
    """Reflect and verify ONLY the exact optional family, without Alembic/DDL."""
    inspector = inspect(connection)
    if not present_tables(inspector.get_table_names(schema=schema)):
        return False
    if connection.dialect.name == "postgresql":
        namespace = schema or inspector.default_schema_name
        unsafe = connection.execute(
            text("""
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relname IN ('platform_users','platform_admin_audit')
            AND (c.relkind <> 'r' OR c.relpersistence <> 'p' OR c.relispartition
                 OR c.relrowsecurity OR c.relforcerowsecurity
                 OR EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal)
                 OR EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid=c.oid
                            AND (NOT k.convalidated OR k.condeferrable OR k.condeferred))
                 OR EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid
                            AND (NOT i.indisvalid OR NOT i.indisready))
                 OR EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_type y ON y.oid=a.atttypid
                            WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                            AND a.attcollation<>y.typcollation)) LIMIT 1
        """),
            {"schema": namespace},
        ).first()
        if unsafe:
            raise IdentitySchemaError("identity_schema_shape_requires_review")
    for name, table in metadata.tables.items():
        actual_columns = inspector.get_columns(name, schema=schema)
        if len(actual_columns) != len(table.columns):
            raise IdentitySchemaError("identity_schema_shape_requires_review")
        actual = {}
        for column in actual_columns:
            if column.get("computed") or column.get("identity"):
                raise IdentitySchemaError("identity_schema_shape_requires_review")
            kind = str(column["type"].compile(dialect=connection.dialect)).upper()
            if connection.dialect.name == "postgresql" and kind == "FLOAT":
                kind = "DOUBLE PRECISION"
            actual[column["name"]] = (
                kind,
                column["nullable"],
                _default(column.get("default")),
            )
        expected = {}
        for column in table.columns:
            kind = str(column.type.compile(dialect=connection.dialect)).upper()
            if connection.dialect.name == "postgresql" and kind == "FLOAT":
                kind = "DOUBLE PRECISION"
            value = (
                None
                if column.server_default is None
                else str(column.server_default.arg)
            )
            if value in {"user", "active"}:
                value = "'" + value + "'"
            expected[column.name] = (kind, column.nullable, _default(value))
        unique_rows = inspector.get_unique_constraints(name, schema=schema)
        if any(
            any(
                value not in (None, [], {})
                and not (key == "postgresql_nulls_not_distinct" and value is False)
                for key, value in row.get("dialect_options", {}).items()
            )
            for row in unique_rows
        ):
            raise IdentitySchemaError("identity_schema_shape_requires_review")
        unique = sorted(
            ((row.get("name"), tuple(row["column_names"])) for row in unique_rows),
            key=lambda item: (item[0] or "", item[1]),
        )
        expected_unique = sorted(
            (constraint.name, tuple(constraint.columns.keys()))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        try:
            checks = sorted(
                normalize_check(row["sqltext"])
                for row in inspector.get_check_constraints(name, schema=schema)
            )
            expected_checks = sorted(
                normalize_check(constraint.sqltext)
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            )
        except IdentitySchemaError:
            raise IdentitySchemaError("identity_schema_shape_requires_review") from None
        if (
            actual != expected
            or inspector.get_pk_constraint(name, schema=schema).get(
                "constrained_columns"
            )
            != list(table.primary_key.columns.keys())
            or unique != expected_unique
            or checks != expected_checks
            or inspector.get_foreign_keys(name, schema=schema)
            or any(
                not row.get("duplicates_constraint")
                for row in inspector.get_indexes(name, schema=schema)
            )
        ):
            raise IdentitySchemaError("identity_schema_shape_requires_review")
    return True


def validate_sqlite(db):
    """Check the exact frozen SQLite family before any business export/read."""
    rows = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    if not present_tables(rows):
        return False
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    for name, table in metadata.tables.items():
        expected = str(CreateTable(table).compile(dialect=sqlite_dialect()))
        if normalize(rows[name]) != normalize(expected):
            raise IdentitySchemaError("identity_schema_shape_requires_review")
        expected_unique = {tuple(table.primary_key.columns.keys())}
        expected_unique.update(
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        actual_unique = set()
        for index in db.execute('PRAGMA index_list("' + name + '")'):
            if index[2] != 1 or index[3] not in {"pk", "u"} or index[4] != 0:
                raise IdentitySchemaError("identity_schema_shape_requires_review")
            actual_unique.add(
                tuple(
                    row[2]
                    for row in db.execute('PRAGMA index_info("' + index[1] + '")')
                )
            )
        if actual_unique != expected_unique:
            raise IdentitySchemaError("identity_schema_shape_requires_review")
    return True
