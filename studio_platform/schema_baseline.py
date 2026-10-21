"""Schema verification and explicitly guarded baseline adoption, without startup.

Adoption only creates Alembic's version table on an exactly matching populated
database, or applies the frozen baseline to an empty database. No generic online
upgrade, downgrade, autogenerate or unverified stamp is exposed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)

from .migrations.versions import v20261019_0001_platform_baseline as baseline
from .schema_version import (
    BASELINE_REVISION,
    CREDIT_REVISION,
    CREDIT_REVISIONS,
    PAYMENT_REVISION,
    PAYMENT_REVISIONS,
    LIVE_PAYMENT_REVISION,
    IDENTITY_REVISION,
    IDENTITY_REVISIONS,
    WORKFLOW_REVISIONS,
    SchemaVersionError,
    read_schema_version,
)

REVISION = baseline.revision
if REVISION != BASELINE_REVISION:
    raise RuntimeError("baseline_reader_revision_mismatch")


class BaselineError(Exception):
    """Static code only; SQL/DSNs/driver details must not reach output."""


class _MetadataCollector:
    def __init__(self):
        self.metadata = MetaData()

    def create_table(self, name, *columns, **kwargs):
        return Table(name, self.metadata, *columns, **kwargs)

    def create_index(self, name, table_name, columns, *, unique=False, **kwargs):
        table = self.metadata.tables[table_name]
        return Index(
            name, *(table.c[column] for column in columns), unique=unique, **kwargs
        )


def frozen_metadata():
    collector = _MetadataCollector()
    baseline.build(collector)
    return collector.metadata


def _type(value, dialect):
    result = str(value.compile(dialect=dialect)).upper()
    # SQLAlchemy Float emits FLOAT on PG and reflection emits DOUBLE PRECISION;
    # both are PostgreSQL's default 53-bit float, unlike REAL/FLOAT(24).
    return (
        "DOUBLE PRECISION"
        if dialect.name == "postgresql" and result == "FLOAT"
        else result
    )


def _default(value):
    if value is None:
        return None
    value = str(value).strip()
    # Only the two literal server defaults in this baseline. Do not normalize
    # arbitrary SQL functions/casts into apparent equality.
    if value in {"'unknown'", "'unknown'::character varying"}:
        return "'unknown'"
    if value in {"0", "'0'", "'0'::integer"}:
        return "0"
    for literal in ("user", "active"):
        if value in {"'" + literal + "'", "'" + literal + "'::character varying"}:
            return "'" + literal + "'"
    return value


def _check(value):
    # Baseline CHECKs contain only conjunctions of simple integer comparisons.
    # PG deparses each term inside parentheses. Reject every other grammar
    # instead of stripping semantically meaningful parentheses/quotes in SQL.
    value = str(value)
    from .identity_schema import IdentitySchemaError, normalize_check

    try:
        return normalize_check(value)
    except IdentitySchemaError:
        pass
    from .credit_schema import CreditSchemaError, normalize_check as credit_check

    try:
        return credit_check(value)
    except CreditSchemaError:
        pass
    from .payment_schema import PaymentSchemaError, normalize_check as payment_check

    try:
        return payment_check(value)
    except PaymentSchemaError:
        pass
    if re.search(r"[^a-zA-Z_0-9\s()><=]", value):
        raise BaselineError("schema_check_expression_requires_review")
    if value.count("(") != value.count(")"):
        raise BaselineError("schema_check_expression_requires_review")
    parts = re.split(
        r"\s+AND\s+", value.replace("(", " ").replace(")", " ").strip(), flags=re.I
    )
    result = []
    for part in parts:
        match = re.fullmatch(
            r"\s*([a-z_][a-z_0-9]*)\s*(>=|<=|>)\s*(\d+)\s*", part, re.I
        )
        if not match:
            raise BaselineError("schema_check_expression_requires_review")
        result.append(match.groups())
    return sorted(result)


def _foreign(columns, referred_table, referred_columns, *, schema=None, options=None):
    return dict(
        columns=list(columns),
        table=referred_table,
        referred=list(referred_columns),
        schema=None if schema in {None, "public"} else schema,
        options=options or {},
    )


def metadata_signature(metadata, dialect):
    result = {}
    for table in metadata.sorted_tables:
        foreign, unique, checks = [], [], []
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                elements = list(constraint.elements)
                foreign.append(
                    _foreign(
                        [element.parent.name for element in elements],
                        elements[0].column.table.name,
                        [element.column.name for element in elements],
                        schema=elements[0].column.table.schema,
                        options={
                            key: value
                            for key, value in dict(
                                onupdate=constraint.onupdate,
                                ondelete=constraint.ondelete,
                                deferrable=constraint.deferrable,
                                initially=constraint.initially,
                                match=constraint.match,
                            ).items()
                            if value is not None
                        },
                    )
                )
            elif isinstance(constraint, UniqueConstraint):
                unique.append(
                    dict(columns=list(constraint.columns.keys()), name=constraint.name)
                )
            elif isinstance(constraint, CheckConstraint):
                checks.append(_check(constraint.sqltext))
        compiler = dialect.ddl_compiler(dialect, None)
        result[table.name] = dict(
            columns={
                column.name: dict(
                    type=_type(column.type, dialect),
                    nullable=column.nullable,
                    default=_default(compiler.get_column_default_string(column)),
                )
                for column in table.columns
            },
            primary=list(table.primary_key.columns.keys()),
            unique=sorted(unique, key=lambda item: json.dumps(item, sort_keys=True)),
            foreign=sorted(foreign, key=lambda item: json.dumps(item, sort_keys=True)),
            checks=sorted(checks),
            indexes=sorted(
                [
                    dict(
                        name=index.name,
                        columns=[column.name for column in index.columns],
                        unique=bool(index.unique),
                        options={},
                    )
                    for index in table.indexes
                ],
                key=lambda item: item["name"],
            ),
        )
    return result


def validate_version_table(connection, *, schema=None):
    """Recognize only this exact bookkeeping table, without business-row reads."""
    try:
        return read_schema_version(connection, schema=schema)
    except SchemaVersionError as error:
        raise BaselineError(str(error)) from None


def reflected_signature(connection, *, schema=None):
    inspector = inspect(connection)
    if connection.dialect.name not in {"sqlite", "postgresql"}:
        raise BaselineError("schema_dialect_not_supported")
    tables = inspector.get_table_names(schema=schema)
    if (
        len(tables) > 128
        or inspector.get_view_names(schema=schema)
        or inspector.get_temp_table_names()
    ):
        raise BaselineError("schema_unexpected_database_objects")
    if connection.dialect.name == "postgresql":
        from .workflow_schema import INKSEQ_WORKFLOW_SCHEMA, validate

        workflow_schema = None
        if schema is None and validate_version_table(connection) in WORKFLOW_REVISIONS:
            try:
                workflow_schema = validate(connection)
            except Exception:
                raise BaselineError('schema_workflow_catalog_requires_review') from None
        # Only the exact adopted SDK family may accompany the public baseline.
        schemas = set(inspector.get_schema_names()) - {"information_schema", "public"}
        if (
            schema is None
            and (
                connection.exec_driver_sql("SELECT current_schema()").scalar_one()
                != "public"
                or any(
                    not name.startswith("pg_")
                    and not (workflow_schema and name == INKSEQ_WORKFLOW_SCHEMA)
                    for name in schemas
                )
            )
        ) or inspector.get_materialized_view_names(schema=schema):
            raise BaselineError("schema_unexpected_database_objects")
        if connection.execute(
            text(
                "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal "
                "AND n.nspname <> :allowed LIMIT 1"
            ),
            {'allowed': INKSEQ_WORKFLOW_SCHEMA if workflow_schema else ''},
        ).first():
            raise BaselineError("schema_unexpected_database_objects")
        namespace = schema or "public"
        invalid = connection.execute(
            text("""SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relkind IN ('r','p')
            AND (c.relkind <> 'r' OR c.relpersistence <> 'p' OR c.relispartition
                 OR c.relrowsecurity OR c.relforcerowsecurity) LIMIT 1"""),
            dict(schema=namespace),
        ).first()
        invalid_constraint = connection.execute(
            text("""SELECT 1 FROM pg_constraint c
            JOIN pg_namespace n ON n.oid=c.connamespace
            WHERE n.nspname=:schema AND (NOT c.convalidated OR c.condeferrable OR c.condeferred) LIMIT 1"""),
            dict(schema=namespace),
        ).first()
        invalid_collation = connection.execute(
            text("""SELECT 1 FROM pg_attribute a
            JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_type t ON t.oid=a.atttypid WHERE n.nspname=:schema AND c.relkind='r'
            AND a.attnum>0 AND NOT a.attisdropped AND a.attcollation<>t.typcollation LIMIT 1"""),
            dict(schema=namespace),
        ).first()
        invalid_index = connection.execute(
            text("""SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema
            AND (NOT i.indisvalid OR NOT i.indisready) LIMIT 1"""),
            dict(schema=namespace),
        ).first()
        if invalid or invalid_constraint or invalid_collation or invalid_index:
            raise BaselineError("schema_unexpected_database_objects")
    elif connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1"
    ).first():
        raise BaselineError("schema_unexpected_database_objects")
    version = validate_version_table(connection, schema=schema)
    named_unique_by_table = {
        table.name: {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name
        }
        for table in _reader_metadata().tables.values()
    }
    result = {}
    for table in tables:
        if table == "alembic_version":
            continue
        columns = inspector.get_columns(table, schema=schema)
        if any(column.get("computed") or column.get("identity") for column in columns):
            raise BaselineError("schema_unexpected_generated_column")
        foreign = [
            _foreign(
                row["constrained_columns"],
                row["referred_table"],
                row["referred_columns"],
                schema=None
                if row.get("referred_schema") == schema
                else row.get("referred_schema"),
                options=row.get("options"),
            )
            for row in inspector.get_foreign_keys(table, schema=schema)
        ]
        unique = inspector.get_unique_constraints(table, schema=schema)
        if any(
            any(
                value not in (None, [], {})
                and not (key == "postgresql_nulls_not_distinct" and value is False)
                for key, value in row.get("dialect_options", {}).items()
            )
            for row in unique
        ):
            raise BaselineError("schema_unique_options_require_review")
        named_unique = named_unique_by_table.get(table, set())
        indexes = []
        for row in inspector.get_indexes(table, schema=schema):
            if row.get("duplicates_constraint"):
                continue
            # Dialect reflection may include an empty INCLUDE list; retain all
            # nonempty options so partial/expression/include drift is refused.
            options = {
                key: value
                for key, value in row.get("dialect_options", {}).items()
                if value not in (None, [], {})
            }
            if row.get("column_sorting"):
                options["column_sorting"] = row["column_sorting"]
            indexes.append(
                dict(
                    name=row["name"],
                    columns=row["column_names"],
                    unique=bool(row["unique"]),
                    options=options,
                )
            )
        result[table] = dict(
            columns={
                column["name"]: dict(
                    type=_type(column["type"], connection.dialect),
                    nullable=column["nullable"],
                    default=_default(column.get("default")),
                )
                for column in columns
            },
            primary=inspector.get_pk_constraint(table, schema=schema).get(
                "constrained_columns", []
            ),
            unique=sorted(
                [
                    dict(
                        columns=row["column_names"],
                        name=row["name"] if row["name"] in named_unique else None,
                    )
                    for row in unique
                ],
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
            foreign=sorted(foreign, key=lambda item: json.dumps(item, sort_keys=True)),
            checks=sorted(
                [
                    _check(row["sqltext"])
                    for row in inspector.get_check_constraints(table, schema=schema)
                ]
            ),
            indexes=sorted(indexes, key=lambda item: item["name"]),
        )
    return result, version


def compare_schema(connection, *, schema=None):
    expected = metadata_signature(frozen_metadata(), connection.dialect)
    actual, version = reflected_signature(connection, schema=schema)
    from .identity_schema import (
        IdentitySchemaError,
        frozen_metadata as identity_metadata,
        present_tables,
    )

    try:
        present = present_tables(actual)
    except IdentitySchemaError as error:
        raise BaselineError(str(error)) from None
    from .credit_schema import (
        CreditSchemaError,
        frozen_metadata as credit_metadata,
        present_tables as credit_present_tables,
    )

    try:
        credits_present = credit_present_tables(actual)
    except CreditSchemaError as error:
        raise BaselineError(str(error)) from None
    from .payment_schema import (
        PaymentSchemaError,
        frozen_metadata as payment_metadata,
        present_tables as payment_present_tables,
        validate_connection as payment_valid,
    )

    try:
        payments_present = payment_present_tables(actual)
    except PaymentSchemaError as error:
        raise BaselineError(str(error)) from None
    if present or version in IDENTITY_REVISIONS or credits_present or payments_present:
        expected.update(metadata_signature(identity_metadata(), connection.dialect))
    if credits_present or version in CREDIT_REVISIONS or payments_present:
        expected.update(metadata_signature(credit_metadata(), connection.dialect))
    if payments_present or version in PAYMENT_REVISIONS:
        payment_revision = (
            LIVE_PAYMENT_REVISION
            if version == LIVE_PAYMENT_REVISION
            else PAYMENT_REVISION
        )
        if payments_present and version == LIVE_PAYMENT_REVISION:
            try:
                payment_valid(connection, schema=schema)
            except PaymentSchemaError as error:
                raise BaselineError(str(error)) from None
        expected.update(
            metadata_signature(
                payment_metadata(revision=payment_revision), connection.dialect
            )
        )
    differences = []
    for name in sorted(expected.keys() | actual.keys()):
        if name not in actual:
            differences.append(dict(table=name, field="missing_table"))
        elif name not in expected:
            differences.append(dict(table=name, field="unexpected_table"))
        else:
            for field in expected[name]:
                if expected[name][field] != actual[name][field]:
                    differences.append(dict(table=name, field=field))
    fingerprint = hashlib.sha256(
        json.dumps(
            dict(schema=actual, version=version), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    result = dict(
        revision=REVISION,
        recorded_revision=version,
        authority="legacy_startup_compatibility"
        if version
        else "legacy_startup_not_adopted",
        matches=not differences,
        schema_fingerprint=fingerprint,
        table_count=len(actual),
        column_count=sum(len(table["columns"]) for table in actual.values()),
        differences=differences,
    )
    from .workflow_schema import CATALOG_SHA256

    if version in WORKFLOW_REVISIONS:
        result['workflow_catalog_sha256'] = CATALOG_SHA256
    return result


def _reader_metadata():
    """Frozen baseline plus recognized optional family, without initializing it."""
    from .identity_schema import frozen_metadata as identity_metadata
    from .credit_schema import frozen_metadata as credit_metadata
    from .payment_schema import frozen_metadata as payment_metadata

    result = frozen_metadata()
    for table in identity_metadata().tables.values():
        table.to_metadata(result)
    for table in credit_metadata().tables.values():
        table.to_metadata(result)
    for table in payment_metadata().tables.values():
        table.to_metadata(result)
    return result


def alembic_config(*, dialect="postgresql", output=None):
    from alembic.config import Config

    config = Config(output_buffer=output)
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    config.attributes["dialect_name"] = dialect
    return config


def offline_sql(dialect):
    from alembic import command

    output = io.StringIO()
    command.upgrade(alembic_config(dialect=dialect, output=output), REVISION, sql=True)
    return output.getvalue()


def adopt_baseline(connection, *, expected_fingerprint, schema=None):
    """One caller-owned transaction; tests may pass an isolated test schema.

    The operational CLI supports PostgreSQL/public only. The caller must retain the
    normal host release lock so startup cannot overlap this schema-only operation.
    """
    if not connection.in_transaction() or not re.fullmatch(
        r"[a-f0-9]{64}", expected_fingerprint or ""
    ):
        raise BaselineError("schema_adoption_requires_transaction_and_fingerprint")
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(685939796868749721)")
    elif connection.dialect.name != "sqlite":
        raise BaselineError("schema_dialect_not_supported")
    # A future live-model change cannot silently redefine this frozen baseline.
    from .schema_registry import platform_metadata

    if metadata_signature(
        platform_metadata(), connection.dialect
    ) != metadata_signature(frozen_metadata(), connection.dialect):
        raise BaselineError("schema_runtime_diff_requires_migration")
    before = compare_schema(connection, schema=schema)
    if before["schema_fingerprint"] != expected_fingerprint:
        raise BaselineError("schema_snapshot_changed")
    if before["recorded_revision"]:
        if not before["matches"]:
            raise BaselineError("schema_drift_requires_review")
        return dict(state="already_adopted", before=before, after=before)
    if before["table_count"] and not before["matches"]:
        raise BaselineError("schema_drift_requires_review")
    from alembic import command

    config = alembic_config(dialect=connection.dialect.name)
    config.attributes["verified_baseline_connection"] = connection
    config.attributes["version_table_schema"] = schema
    mode = "stamp" if before["table_count"] else "upgrade"
    config.attributes["verified_baseline_mode"] = mode
    if mode == "stamp":
        command.stamp(config, REVISION)
    else:
        command.upgrade(config, REVISION)
    after = compare_schema(connection, schema=schema)
    if not after["matches"] or after["recorded_revision"] != REVISION:
        raise BaselineError("schema_adoption_verification_failed")
    return dict(
        state="adopted_existing" if mode == "stamp" else "initialized_empty",
        before=before,
        after=after,
    )


def adopt_workflows(connection, *, expected_fingerprint):
    """Explicit additive writer under the original transaction/advisory lock.

    Deploy the updated backup/readers as both current and rollback before this
    operation. The SDK's migrate() is never called: frozen final DDL is applied
    by Alembic in this SAME transaction, then the exact family is verified.
    """
    from .workflow_schema import WORKFLOW_REVISION, WorkflowSchemaError, validate

    if (
        not connection.in_transaction()
        or connection.dialect.name != 'postgresql'
        or not re.fullmatch(r'[a-f0-9]{64}', expected_fingerprint or '')
    ):
        raise BaselineError('workflow_adoption_requires_transaction_and_fingerprint')
    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
    connection.exec_driver_sql('SELECT pg_advisory_xact_lock(685939796868749721)')
    before = compare_schema(connection)
    if before['schema_fingerprint'] != expected_fingerprint:
        raise BaselineError('schema_snapshot_changed')
    if not before['matches'] or before['recorded_revision'] not in {
        REVISION,
        WORKFLOW_REVISION,
    }:
        raise BaselineError('workflow_adoption_requires_adopted_baseline')
    if before['recorded_revision'] == WORKFLOW_REVISION:
        return dict(state='already_adopted', before=before, after=before)
    from alembic import command

    config = alembic_config()
    config.attributes.update(
        verified_baseline_connection=connection,
        verified_baseline_mode='upgrade',
        verified_workflow_mode=True,
    )
    try:
        command.upgrade(config, WORKFLOW_REVISION)
        validate(connection)
    except WorkflowSchemaError as error:
        raise BaselineError(str(error)) from None
    after = compare_schema(connection)
    if not after['matches'] or after['recorded_revision'] != WORKFLOW_REVISION:
        raise BaselineError('workflow_adoption_verification_failed')
    return dict(state='adopted_workflows', before=before, after=after)


def adopt_identity(
    connection, *, expected_fingerprint, tenant_id, admin_owners=(), clock=time.time
):
    """Atomically adopt users and bootstrap legacy identities exactly once.

    Operational prerequisite: under the existing host release.lock, verify that
    both the current and previous approved images contain the identity reader
    bridge before invoking this command. The app container cannot inspect the
    host's release authority; accepting a self-asserted flag is not such proof.
    This function never constructs services, modifies password rows or settles
    jobs, rentals or money. A repeated adopted call does not bootstrap again.
    """
    from .auth import accounts
    from .identity import lock_account_gate
    from .identity_schema import (
        IdentitySchemaError,
        TABLE_NAMES,
        users,
        validate_connection,
    )
    from .schema_version import WORKFLOW_REVISION

    if (
        not connection.in_transaction()
        or connection.dialect.name != 'postgresql'
        or not re.fullmatch(r'[a-f0-9]{64}', expected_fingerprint or '')
        or not isinstance(tenant_id, str)
        or not 1 <= len(tenant_id) <= 200
        or not isinstance(admin_owners, tuple)
        or any(
            not isinstance(owner, str) or not 1 <= len(owner) <= 80
            for owner in admin_owners
        )
        or len(set(admin_owners)) != len(admin_owners)
    ):
        raise BaselineError('identity_adoption_requires_verified_transaction')
    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
    # The account gate precedes schema, account, job and capacity locks. Reader
    # A also takes its shared side before pre-adoption admission/claims.
    lock_account_gate(connection, exclusive=True)
    connection.exec_driver_sql('SELECT pg_advisory_xact_lock(685939796868749721)')
    before = compare_schema(connection)
    if before['schema_fingerprint'] != expected_fingerprint:
        raise BaselineError('schema_snapshot_changed')
    if not before['matches'] or before['recorded_revision'] not in {
        WORKFLOW_REVISION,
        *IDENTITY_REVISIONS,
    }:
        raise BaselineError('identity_adoption_requires_adopted_workflows')
    if before['recorded_revision'] in IDENTITY_REVISIONS:
        return dict(
            state='already_adopted', before=before, after=before, backfilled_users=0
        )
    if TABLE_NAMES & set(inspect(connection).get_table_names()):
        raise BaselineError('identity_unrecorded_family_requires_review')
    legacy = list(
        connection.execute(
            select(accounts.c.tenant, accounts.c.username, accounts.c.disabled)
            .order_by(accounts.c.tenant, accounts.c.username)
            .with_for_update()
        ).mappings()
    )
    if any(
        not isinstance(row['tenant'], str)
        or not 1 <= len(row['tenant']) <= 200
        or not isinstance(row['username'], str)
        or not 1 <= len(row['username']) <= 80
        or type(row['disabled']) is not int
        for row in legacy
    ):
        raise BaselineError('identity_legacy_accounts_require_review')
    if not any(
        row['tenant'] == tenant_id
        and row['username'] in admin_owners
        and not row['disabled']
        for row in legacy
    ):
        raise BaselineError('identity_bootstrap_active_admin_required')
    now = clock()
    if type(now) not in (float, int) or not 0 < now < 100_000_000_000:
        raise BaselineError('identity_bootstrap_clock_invalid')
    from alembic import command

    config = alembic_config()
    config.attributes.update(
        verified_baseline_connection=connection,
        verified_baseline_mode='upgrade',
        verified_identity_mode=True,
    )
    command.upgrade(config, IDENTITY_REVISION)
    for row in legacy:
        # Stable internal ids do not replace or rewrite business owner strings.
        ident = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                'inkseq.users.v1:'
                + json.dumps([row['tenant'], row['username']], separators=(',', ':')),
            )
        )
        connection.execute(
            users.insert().values(
                id=ident,
                tenant=row['tenant'],
                owner=row['username'],
                role='admin'
                if row['tenant'] == tenant_id and row['username'] in admin_owners
                else 'user',
                status='suspended' if row['disabled'] else 'active',
                display_name=row['username'],
                email=None,
                email_verified=0,
                workos_user_id=None,
                created_at=now,
                updated_at=now,
                terms_version=None,
                terms_accepted_at=None,
            )
        )
    try:
        validate_connection(connection)
    except IdentitySchemaError as error:
        raise BaselineError(str(error)) from None
    after = compare_schema(connection)
    if not after['matches'] or after['recorded_revision'] != IDENTITY_REVISION:
        raise BaselineError('identity_adoption_verification_failed')
    return dict(
        state='adopted_identity',
        before=before,
        after=after,
        backfilled_users=len(legacy),
    )


def adopt_credits(connection, *, expected_fingerprint):
    """Add customer tables under the existing locks, without minting money.

    As for identity adoption, both current and previous approved images must
    first contain this reader bridge, verified under the host release.lock.
    Repeating adoption never creates accounts, grants, prices or policy rows.
    """
    from .identity import lock_account_gate
    from .credit_schema import CreditSchemaError, TABLE_NAMES, validate_connection

    if (
        not connection.in_transaction()
        or connection.dialect.name != 'postgresql'
        or not re.fullmatch(r'[a-f0-9]{64}', expected_fingerprint or '')
    ):
        raise BaselineError('credit_adoption_requires_verified_transaction')
    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
    lock_account_gate(connection, exclusive=True)
    connection.exec_driver_sql('SELECT pg_advisory_xact_lock(685939796868749721)')
    before = compare_schema(connection)
    if before['schema_fingerprint'] != expected_fingerprint:
        raise BaselineError('schema_snapshot_changed')
    if not before['matches'] or before['recorded_revision'] not in {
        IDENTITY_REVISION,
        *CREDIT_REVISIONS,
    }:
        raise BaselineError('credit_adoption_requires_adopted_identity')
    if before['recorded_revision'] in CREDIT_REVISIONS:
        return dict(state='already_adopted', before=before, after=before)
    if TABLE_NAMES & set(inspect(connection).get_table_names()):
        raise BaselineError('credit_unrecorded_family_requires_review')
    from alembic import command

    config = alembic_config()
    config.attributes.update(
        verified_baseline_connection=connection,
        verified_baseline_mode='upgrade',
        verified_credit_mode=True,
    )
    command.upgrade(config, CREDIT_REVISION)
    try:
        validate_connection(connection)
    except CreditSchemaError as error:
        raise BaselineError(str(error)) from None
    after = compare_schema(connection)
    if not after['matches'] or after['recorded_revision'] != CREDIT_REVISION:
        raise BaselineError('credit_adoption_verification_failed')
    return dict(state='adopted_credits', before=before, after=after)


def adopt_payments(connection, *, expected_fingerprint):
    """Create only the empty test-payment family in the original transaction.

    Under release.lock the operator first verifies payments are OFF and both
    current/previous approved images contain this exact reader bridge. The CLI
    also checks trusted Settings; no self-asserted proof or mode flag is accepted.
    This function changes no pack configuration, credit balance or event row.
    """
    from .identity import lock_account_gate
    from .payment_schema import PaymentSchemaError, TABLE_NAMES, validate_connection

    if (
        not connection.in_transaction()
        or connection.dialect.name != 'postgresql'
        or not re.fullmatch(r'[a-f0-9]{64}', expected_fingerprint or '')
    ):
        raise BaselineError('payment_adoption_requires_verified_transaction')
    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
    lock_account_gate(connection, exclusive=True)
    connection.exec_driver_sql('SELECT pg_advisory_xact_lock(685939796868749721)')
    before = compare_schema(connection)
    if before['schema_fingerprint'] != expected_fingerprint:
        raise BaselineError('schema_snapshot_changed')
    if not before['matches'] or before['recorded_revision'] not in CREDIT_REVISIONS:
        raise BaselineError('payment_adoption_requires_adopted_credits')
    if before['recorded_revision'] in PAYMENT_REVISIONS:
        return dict(state='already_adopted', before=before, after=before)
    if TABLE_NAMES & set(inspect(connection).get_table_names()):
        raise BaselineError('payment_unrecorded_family_requires_review')
    from alembic import command

    config = alembic_config()
    config.attributes.update(
        verified_baseline_connection=connection,
        verified_baseline_mode='upgrade',
        verified_payment_mode=True,
    )
    command.upgrade(config, PAYMENT_REVISION)
    try:
        validate_connection(connection)
    except PaymentSchemaError as error:
        raise BaselineError(str(error)) from None
    after = compare_schema(connection)
    if not after['matches'] or after['recorded_revision'] != PAYMENT_REVISION:
        raise BaselineError('payment_adoption_verification_failed')
    return dict(state='adopted_payments', before=before, after=after)


def adopt_payment_live(connection, *, expected_fingerprint, tenant_id):
    """Widen only the two frozen payment mode checks, with purchasing OFF.

    The host operator holds release.lock and verifies both approved rollback
    readers before calling this explicit path. Stripe mode is checked by the
    CLI before connecting; the persisted charging policy is checked under the
    original account/migration locks. No payment, event or credit row changes.
    """
    from .credits import CreditError, credit_policy
    from .identity import lock_account_gate
    from .payment_schema import PaymentSchemaError, recognized_revision

    if (
        not connection.in_transaction()
        or connection.dialect.name != 'postgresql'
        or not isinstance(expected_fingerprint, str)
        or not re.fullmatch(r'[a-f0-9]{64}', expected_fingerprint)
        or not isinstance(tenant_id, str)
        or not 1 <= len(tenant_id) <= 200
    ):
        raise BaselineError('payment_live_adoption_requires_verified_transaction')
    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
    lock_account_gate(connection, exclusive=True)
    connection.exec_driver_sql('SELECT pg_advisory_xact_lock(685939796868749721)')
    before = compare_schema(connection)
    if before['schema_fingerprint'] != expected_fingerprint:
        raise BaselineError('schema_snapshot_changed')
    if not before['matches'] or before['recorded_revision'] not in PAYMENT_REVISIONS:
        raise BaselineError('payment_live_adoption_requires_adopted_payments')
    try:
        charging = credit_policy(connection, tenant_id)
    except CreditError:
        raise BaselineError('payment_live_adoption_credit_policy_unavailable') from None
    if charging['enabled']:
        raise BaselineError('payment_live_adoption_requires_charging_off')
    if before['recorded_revision'] == LIVE_PAYMENT_REVISION:
        return dict(state='already_adopted', before=before, after=before)
    from alembic import command

    config = alembic_config()
    config.attributes.update(
        verified_baseline_connection=connection,
        verified_baseline_mode='upgrade',
        verified_payment_live_mode=True,
    )
    command.upgrade(config, LIVE_PAYMENT_REVISION)
    try:
        if recognized_revision(connection) != LIVE_PAYMENT_REVISION:
            raise BaselineError('payment_live_adoption_verification_failed')
    except PaymentSchemaError as error:
        raise BaselineError(str(error)) from None
    after = compare_schema(connection)
    if not after['matches'] or after['recorded_revision'] != LIVE_PAYMENT_REVISION:
        raise BaselineError('payment_live_adoption_verification_failed')
    return dict(state='adopted_payment_live', before=before, after=after)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Schema-only baseline verification and guarded adoption"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "check", help="Read existing configured PostgreSQL schema without startup/DDL"
    )
    sql = commands.add_parser(
        "sql", help="Print offline baseline DDL without a database connection"
    )
    sql.add_argument(
        "--dialect", choices=("postgresql", "sqlite"), default="postgresql"
    )
    adopt = commands.add_parser(
        "adopt", help="Adopt an exact checked schema under the existing migration lock"
    )
    adopt.add_argument("--expected-fingerprint", required=True)
    workflows = commands.add_parser(
        'adopt-workflows',
        help='Guarded pinned SDK schema adoption after reader bridge release',
    )
    workflows.add_argument('--expected-fingerprint', required=True)
    identity = commands.add_parser(
        'adopt-identity',
        help='Adopt users after current and previous approved reader images are verified under release.lock',
    )
    identity.add_argument('--expected-fingerprint', required=True)
    credits = commands.add_parser(
        'adopt-credits',
        help='Add customer-credit tables after both approved reader images are verified under release.lock',
    )
    credits.add_argument('--expected-fingerprint', required=True)
    payments = commands.add_parser(
        'adopt-payments',
        help='Add empty test-payment tables with payments OFF after both reader images are verified under release.lock',
    )
    payments.add_argument('--expected-fingerprint', required=True)
    live = commands.add_parser(
        'adopt-payment-live',
        help='Adopt exact live-mode checks with Stripe/charging OFF after both approved reader images are verified under release.lock',
    )
    live.add_argument('--expected-fingerprint', required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        if args.command == "sql":
            print(offline_sql(args.dialect), end="")
            return 0
        if args.command == 'adopt-payments':
            from .settings import Settings

            settings = Settings.from_environment()
            # Reader A has no payment routes or mode setting; absence is OFF.
            if getattr(settings, 'stripe_mode', 'off') != 'off':
                raise BaselineError('payment_adoption_requires_payments_off')
        if args.command == 'adopt-payment-live':
            from .settings import Settings

            settings = Settings.from_environment()
            if getattr(settings, 'stripe_mode', None) != 'off':
                raise BaselineError('payment_live_adoption_requires_payments_off')
        from .settings import database_url_from_environment

        url = database_url_from_environment()
        if not url.startswith("postgresql+psycopg://"):
            raise BaselineError("schema_check_requires_explicit_postgresql_config")
        engine = create_engine(
            url, hide_parameters=True, connect_args={"connect_timeout": 10}
        )
        with engine.connect() as connection, connection.begin():
            if args.command == "check":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
                result = compare_schema(connection)
            elif args.command == 'adopt-workflows':
                result = adopt_workflows(
                    connection, expected_fingerprint=args.expected_fingerprint
                )
            elif args.command == 'adopt-identity':
                from .settings import Settings

                settings = Settings.from_environment()
                result = adopt_identity(
                    connection,
                    expected_fingerprint=args.expected_fingerprint,
                    tenant_id=settings.tenant_id,
                    admin_owners=tuple(
                        sorted(
                            set(settings.operator_admin_owners)
                            | set(settings.operator_capacity_owners)
                        )
                    ),
                )
            elif args.command == 'adopt-credits':
                result = adopt_credits(
                    connection, expected_fingerprint=args.expected_fingerprint
                )
            elif args.command == 'adopt-payments':
                result = adopt_payments(
                    connection, expected_fingerprint=args.expected_fingerprint
                )
            elif args.command == 'adopt-payment-live':
                result = adopt_payment_live(
                    connection,
                    expected_fingerprint=args.expected_fingerprint,
                    tenant_id=settings.tenant_id,
                )
            else:
                result = adopt_baseline(
                    connection, expected_fingerprint=args.expected_fingerprint
                )
        print(json.dumps(result, sort_keys=True))
        return (
            0
            if args.command
            in {
                "adopt",
                'adopt-workflows',
                'adopt-identity',
                'adopt-credits',
                'adopt-payments',
                'adopt-payment-live',
            }
            or result["matches"]
            else 1
        )
    except BaselineError as error:
        print(json.dumps(dict(code=str(error))), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps(dict(code="schema_baseline_check_failed")), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
