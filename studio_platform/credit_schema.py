"""Optional customer-credit schema, read-only until explicit guarded adoption.

These are portable business rows. Provider microUSD accounting is separate and
unchanged; this module never creates tables, grants credits or mutates policy.
"""

import re

from sqlalchemy import (
    CheckConstraint,
    Column,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.schema import CreateTable

from .migrations.versions import v20261019_0004_customer_credits as frozen

CREDIT_REVISION = frozen.revision


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
accounts = metadata.tables["platform_credit_accounts"]
entries = metadata.tables["platform_credit_entries"]
prices = metadata.tables["platform_credit_prices"]
TABLE_NAMES = frozenset(metadata.tables)


class CreditSchemaError(Exception):
    """A static diagnostic without SQL, identities, balances or credentials."""


def present_tables(names):
    present = TABLE_NAMES & set(names)
    if present and present != TABLE_NAMES:
        raise CreditSchemaError("credit_schema_partial_family_requires_review")
    return bool(present)


def normalize_check(value):
    """Recognize only frozen CHECKs, including PostgreSQL's deparsed form."""
    parts = re.split(r"('(?:[^']|'')*')", str(value))
    for index in range(0, len(parts), 2):
        part = re.sub(r"\s+|[()]", "", parts[index]).lower()
        for cast in ("::charactervarying", "::text[]", "::text", "::bigint"):
            part = part.replace(cast, "")
        parts[index] = part
    compact = "".join(parts)
    choices = "'grant','purchase','hold','capture','release','refund','adjust','expire','waive'"
    if compact in {"kindin" + choices, "kind=anyarray[" + choices + "]"}:
        return [("kind", "in", choices)]
    for terms in (
        (
            ("balance", ">=", "0"),
            ("held", ">=", "0"),
            ("held", "<=", "balance"),
            ("version", ">=", "0"),
        ),
        (
            ("balance_after", ">=", "0"),
            ("held_after", ">=", "0"),
            ("held_after", "<=", "balance_after"),
        ),
    ):
        if compact == "and".join("".join(term) for term in terms):
            return sorted(terms)
    raise CreditSchemaError("credit_schema_check_requires_review")


def _kind(value, dialect):
    kind = str(value.compile(dialect=dialect)).upper()
    return (
        "DOUBLE PRECISION" if dialect.name == "postgresql" and kind == "FLOAT" else kind
    )


def validate_connection(connection, *, schema=None):
    """Verify the exact optional family without Alembic, DDL or row reads."""
    inspector = inspect(connection)
    if not present_tables(inspector.get_table_names(schema=schema)):
        return False
    if connection.dialect.name == "postgresql":
        unsafe = connection.execute(
            text("""
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relname IN
              ('platform_credit_accounts','platform_credit_entries','platform_credit_prices')
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
            {"schema": schema or inspector.default_schema_name},
        ).first()
        if unsafe:
            raise CreditSchemaError("credit_schema_shape_requires_review")
    for name, table in metadata.tables.items():
        columns = inspector.get_columns(name, schema=schema)
        actual = {
            column["name"]: (
                _kind(column["type"], connection.dialect),
                column["nullable"],
                column.get("default"),
            )
            for column in columns
        }
        expected = {
            column.name: (_kind(column.type, connection.dialect), column.nullable, None)
            for column in table.columns
        }
        unique_rows = inspector.get_unique_constraints(name, schema=schema)
        if any(
            any(
                value not in (None, [], {})
                and not (key == "postgresql_nulls_not_distinct" and value is False)
                for key, value in row.get("dialect_options", {}).items()
            )
            for row in unique_rows
        ):
            raise CreditSchemaError("credit_schema_shape_requires_review")
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
        except CreditSchemaError:
            raise CreditSchemaError("credit_schema_shape_requires_review") from None
        if (
            len(columns) != len(table.columns)
            or any(
                column.get("computed") or column.get("identity") for column in columns
            )
            or actual != expected
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
            raise CreditSchemaError("credit_schema_shape_requires_review")
    return True


def validate_sqlite(db):
    rows = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    if not present_tables(rows):
        return False
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    for name, table in metadata.tables.items():
        expected = str(CreateTable(table).compile(dialect=sqlite_dialect()))
        if normalize(rows[name]) != normalize(expected):
            raise CreditSchemaError("credit_schema_shape_requires_review")
        expected_unique = {tuple(table.primary_key.columns.keys())}
        expected_unique.update(
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        actual_unique = set()
        for index in db.execute('PRAGMA index_list("' + name + '")'):
            if index[2] != 1 or index[3] not in {"pk", "u"} or index[4] != 0:
                raise CreditSchemaError("credit_schema_shape_requires_review")
            actual_unique.add(
                tuple(
                    row[2]
                    for row in db.execute('PRAGMA index_info("' + index[1] + '")')
                )
            )
        if actual_unique != expected_unique:
            raise CreditSchemaError("credit_schema_shape_requires_review")
    return True


def ready(connection, *, schema=None):
    """Bounded runtime authority check, with no durable catalog cache.

    Adoption/check/backup validate the complete frozen shape. Runtime needs the
    known marker and complete table families, not per-request catalog reflection.
    Merely recognizing complete but unrecorded DDL does not enable credit writes.
    """
    from .identity_schema import TABLE_NAMES as identity_tables
    from .schema_version import CREDIT_REVISIONS, KNOWN_REVISIONS

    names = set(inspect(connection).get_table_names(schema=schema))
    present = present_tables(names)
    if "alembic_version" not in names:
        return False
    marker = Table(
        "alembic_version", MetaData(), Column("version_num", String(32)), schema=schema
    )
    try:
        revisions = list(
            connection.execute(select(marker.c.version_num).limit(2)).scalars()
        )
    except SQLAlchemyError:
        raise CreditSchemaError("credit_schema_revision_requires_review") from None
    if len(revisions) != 1 or revisions[0] not in KNOWN_REVISIONS:
        raise CreditSchemaError("credit_schema_revision_requires_review")
    if revisions[0] not in CREDIT_REVISIONS:
        return False
    if not present or not identity_tables <= names:
        raise CreditSchemaError("credit_schema_partial_family_requires_review")
    return True
