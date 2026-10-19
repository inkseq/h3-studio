"""Exact optional test-payment readers; imports never create DDL or money.

The verified event subset and immutable original purchase are business history.
Stripe secrets, raw signature headers and full webhook bodies do not belong here.
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

from .migrations.versions import v20261020_0005_stripe_payments as frozen

PAYMENT_REVISION = frozen.revision


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
payments = metadata.tables["platform_payments"]
events = metadata.tables["platform_stripe_events"]
TABLE_NAMES = frozenset(metadata.tables)


class PaymentSchemaError(Exception):
    """A finite diagnostic with no SQL, payment identity or credentials."""


def present_tables(names):
    present = TABLE_NAMES & set(names)
    if present and present != TABLE_NAMES:
        raise PaymentSchemaError("payment_schema_partial_family_requires_review")
    return bool(present)


def normalize_check(value):
    """Only the frozen expressions, including PostgreSQL's deparsed enums."""
    parts = re.split(r"('(?:[^']|'')*')", str(value))
    for index in range(0, len(parts), 2):
        part = re.sub(r"\s+|[()]", "", parts[index]).lower()
        for cast in (
            "::charactervarying",
            "::text[]",
            "::text",
            "::bigint",
            "::integer",
        ):
            part = part.replace(cast, "")
        parts[index] = part
    compact = "".join(parts)
    for choices in (
        "'created','paid','failed','expired','refunded','disputed'",
        "'received','processed','deferred','review'",
    ):
        if compact in {"statusin" + choices, "status=anyarray[" + choices + "]"}:
            return [("status", "in", choices)]
    for expression in (
        "livemode=0",
        "dispute_open>=0anddispute_open<=1",
        "credits>0andamount_cents>0andrefunded_cents>=0"
        "andrefunded_cents<=amount_centsandrefund_credits>=0"
        "andrefund_credits<=creditsandclawed_back_credits>=0"
        "andclawed_back_credits<=refund_creditsandunmet_credits>=0"
        "andunmet_credits=refund_credits-clawed_back_credits",
    ):
        if compact == expression:
            return [("frozen", expression)]
    raise PaymentSchemaError("payment_schema_check_requires_review")


def _kind(value, dialect):
    kind = str(value.compile(dialect=dialect)).upper()
    return (
        "DOUBLE PRECISION" if dialect.name == "postgresql" and kind == "FLOAT" else kind
    )


def validate_connection(connection, *, schema=None):
    """Verify precisely the optional family without Alembic or business reads."""
    inspector = inspect(connection)
    if not present_tables(inspector.get_table_names(schema=schema)):
        return False
    if connection.dialect.name == "postgresql":
        unsafe = connection.execute(
            text("""
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relname IN
              ('platform_payments','platform_stripe_events')
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
            raise PaymentSchemaError("payment_schema_shape_requires_review")
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
            raise PaymentSchemaError("payment_schema_shape_requires_review")
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
        except PaymentSchemaError:
            raise PaymentSchemaError("payment_schema_shape_requires_review") from None
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
            raise PaymentSchemaError("payment_schema_shape_requires_review")
    return True


def validate_sqlite(db):
    rows = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    if not present_tables(rows):
        return False
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    for name, table in metadata.tables.items():
        expected = str(CreateTable(table).compile(dialect=sqlite_dialect()))
        if normalize(rows[name]) != normalize(expected):
            raise PaymentSchemaError("payment_schema_shape_requires_review")
        expected_unique = {tuple(table.primary_key.columns.keys())}
        expected_unique.update(
            tuple(constraint.columns.keys())
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        actual_unique = set()
        for index in db.execute('PRAGMA index_list("' + name + '")'):
            if index[2] != 1 or index[3] not in {"pk", "u"} or index[4] != 0:
                raise PaymentSchemaError("payment_schema_shape_requires_review")
            actual_unique.add(
                tuple(
                    row[2]
                    for row in db.execute('PRAGMA index_info("' + index[1] + '")')
                )
            )
        if actual_unique != expected_unique:
            raise PaymentSchemaError("payment_schema_shape_requires_review")
    return True


def ready(connection, *, schema=None):
    """Bounded recognition; complete unrecorded future DDL is never authority."""
    from .identity_schema import TABLE_NAMES as identity_tables
    from .credit_schema import TABLE_NAMES as credit_tables
    from .schema_version import KNOWN_REVISIONS

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
        raise PaymentSchemaError("payment_schema_revision_requires_review") from None
    if len(revisions) != 1 or revisions[0] not in KNOWN_REVISIONS:
        raise PaymentSchemaError("payment_schema_revision_requires_review")
    if revisions[0] != PAYMENT_REVISION:
        return False
    if not present or not (identity_tables | credit_tables) <= names:
        raise PaymentSchemaError("payment_schema_partial_family_requires_review")
    return True
