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
from .migrations.versions import v20261021_0006_stripe_live_mode as live_frozen
from .schema_version import LIVE_PAYMENT_REVISION, PAYMENT_REVISIONS

PAYMENT_REVISION = frozen.revision


class _Collector:
    def __init__(self):
        self.metadata = MetaData()

    def create_table(self, name, *columns, **kwargs):
        return Table(name, self.metadata, *columns, **kwargs)


def frozen_metadata(*, revision=PAYMENT_REVISION):
    collector = _Collector()
    if revision == PAYMENT_REVISION:
        frozen.build(collector)
    elif revision == LIVE_PAYMENT_REVISION:
        live_frozen.build(collector)
    else:
        raise PaymentSchemaError("payment_schema_revision_requires_review")
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
    if compact in {"livemodein0,1", "livemode=anyarray[0,1]"}:
        return [("livemode", "in", "0,1")]
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


def recognized_revision(connection, *, schema=None):
    """Verify precisely the optional family without Alembic or business reads."""
    inspector = inspect(connection)
    if not present_tables(inspector.get_table_names(schema=schema)):
        return None
    from .schema_version import SchemaVersionError, read_schema_version

    try:
        marker = read_schema_version(connection, schema=schema)
    except SchemaVersionError:
        raise PaymentSchemaError("payment_schema_revision_requires_review") from None
    revision = (
        LIVE_PAYMENT_REVISION if marker == LIVE_PAYMENT_REVISION else PAYMENT_REVISION
    )
    expected_metadata = frozen_metadata(revision=revision)
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
    for name, table in expected_metadata.tables.items():
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
            check_rows = inspector.get_check_constraints(name, schema=schema)
            checks = sorted(normalize_check(row["sqltext"]) for row in check_rows)
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
            or revision == LIVE_PAYMENT_REVISION
            and sorted(
                (row.get("name") or "", normalize_check(row["sqltext"]))
                for row in check_rows
            )
            != sorted(
                (constraint.name, normalize_check(constraint.sqltext))
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            )
            or inspector.get_foreign_keys(name, schema=schema)
            or any(
                not row.get("duplicates_constraint")
                for row in inspector.get_indexes(name, schema=schema)
            )
        ):
            raise PaymentSchemaError("payment_schema_shape_requires_review")
    return revision


def validate_connection(connection, *, schema=None):
    return recognized_revision(connection, schema=schema) is not None


def sqlite_revision(db, *, published=False):
    rows = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    if not present_tables(rows):
        return None
    from .schema_version import SchemaVersionError, read_sqlite_schema_version

    try:
        marker = read_sqlite_schema_version(db)
    except SchemaVersionError:
        raise PaymentSchemaError("payment_schema_revision_requires_review") from None
    # Business-only backups deliberately omit the marker. Only that explicit
    # disabled/archive reader may recognize an exact unrecorded 006 shape.
    revisions = (
        [LIVE_PAYMENT_REVISION]
        if marker == LIVE_PAYMENT_REVISION
        else [PAYMENT_REVISION, LIVE_PAYMENT_REVISION]
        if marker is None and published is True
        else [PAYMENT_REVISION]
    )
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    matches = []
    for revision in revisions:
        expected_metadata = frozen_metadata(revision=revision)
        if all(
            normalize(rows[name])
            == normalize(str(CreateTable(table).compile(dialect=sqlite_dialect())))
            for name, table in expected_metadata.tables.items()
        ):
            matches.append((revision, expected_metadata))
    if len(matches) != 1:
        raise PaymentSchemaError("payment_schema_shape_requires_review")
    revision, expected_metadata = matches[0]
    for name, table in expected_metadata.tables.items():
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
    return revision


def validate_sqlite(db, *, published=False):
    return sqlite_revision(db, published=published) is not None


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
    if revisions[0] not in PAYMENT_REVISIONS:
        return False
    if not present or not (identity_tables | credit_tables) <= names:
        raise PaymentSchemaError("payment_schema_partial_family_requires_review")
    if revisions[0] == LIVE_PAYMENT_REVISION:
        # The rollback bridge must not authorize a mixed/partial future family.
        # 005 keeps its existing bounded hot path and test-only writer behavior.
        validate_connection(connection, schema=schema)
    return True
