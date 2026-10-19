"""Pinned workflow schema and shared producer/executor contract.

No SDK, database connection, migration or provider is loaded on import. The
public business schema remains independently governed by its frozen baseline.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import text

INKSEQ_WORKFLOW_SCHEMA = "inkseq_workflows"
WORKFLOW_APPLICATION_VERSION = "inkseq-workflows-v1"
WORKFLOW_APPLICATION_NAME = "inkseq-workflows"
WORKFLOW_SDK_VERSION = "3.3.0"
WORKFLOW_REVISION = "20261019_0002"
WORKFLOW_POLLING_SECONDS = 10.0
DATA = Path(__file__).with_name("migrations") / "data"
CATALOG_SHA256 = "e3e6b1085a85bf4916c822c66e3a530dab3b15c056f1a82b5b4b2bba0b832f5b"


class WorkflowSchemaError(Exception):
    """Finite diagnostics only; no database/driver or workflow payload details."""


def require(value, code):
    if not value:
        raise WorkflowSchemaError(code)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def dbos_config(database_url, *, executor_id=None):
    """One exact contract for the app producer and the controller executor.

    Named queues separately use polling_interval_sec=10. The pinned SDK's
    internal queue/manager still poll at1s; no vendor mutation is performed.
    Callers must enforce deployment and restored-domain recovery gates before
    launching an executor. A configuration is not an execution authorization.
    """
    require(
        isinstance(database_url, str)
        and database_url.startswith("postgresql+psycopg://"),
        "workflow_postgresql_required",
    )
    require(
        executor_id is None
        or executor_id in {"inkseq-workflows-staging", "inkseq-workflows-production"},
        "workflow_executor_identity_invalid",
    )
    value = dict(
        name=WORKFLOW_APPLICATION_NAME,
        application_version=WORKFLOW_APPLICATION_VERSION,
        system_database_url=database_url,
        dbos_system_schema=INKSEQ_WORKFLOW_SCHEMA,
        run_migrations=False,
        use_listen_notify=True,
        notification_listener_polling_interval_sec=WORKFLOW_POLLING_SECONDS,
        scheduler_polling_interval_sec=WORKFLOW_POLLING_SECONDS,
        enable_otlp=False,
        max_executor_threads=4,
        sys_db_pool_size=5,
        conductor_key=None,
    )
    if executor_id is not None:
        value["executor_id"] = executor_id
    return value


def dbos_client_config(engine):
    """Producer only: transaction caller owns its connection/retry boundary."""
    require(engine.dialect.name == "postgresql", "workflow_postgresql_required")
    return dict(
        system_database_engine=engine,
        dbos_system_schema=INKSEQ_WORKFLOW_SCHEMA,
        application_name=WORKFLOW_APPLICATION_NAME,
        lazy=True,
        retry_connection_errors=False,
    )


def frozen_catalog():
    value = json.loads((DATA / "dbos_3_3_0_catalog.json").read_text(encoding="utf-8"))
    require(
        hashlib.sha256(canonical(value)).hexdigest() == CATALOG_SHA256,
        "workflow_frozen_catalog_changed",
    )
    return value


def assert_execution_allowed(connection):
    """Restoration keeps workflow state, but never implicitly restarts it."""
    # Full catalog validation belongs to startup/adoption, not every enqueue.
    held = connection.exec_driver_sql(
        "SELECT 1 FROM inkseq_workflows.event_dispatch_kv "
        "WHERE service_name='inkseq.recovery' AND key='restored-state' LIMIT 1"
    ).first()
    require(not held, "workflow_restored_execution_requires_review")


def schema_exists(connection):
    require(connection.dialect.name == "postgresql", "workflow_postgresql_required")
    return bool(
        connection.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname=:s"),
            {"s": INKSEQ_WORKFLOW_SCHEMA},
        ).first()
    )


def catalog(connection):
    """Metadata only, with exact fixed namespace and bounded finite inventory."""
    require(connection.dialect.name == "postgresql", "workflow_postgresql_required")
    params = {"s": INKSEQ_WORKFLOW_SCHEMA}

    def query(sql):
        rows = connection.execute(text(sql), params).mappings().fetchmany(4097)
        require(len(rows) <= 4096, "workflow_catalog_limit")
        return [dict(row) for row in rows]

    result = {}
    result["relations"] = (
        query("""SELECT c.relname,c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity,c.relispartition
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:s ORDER BY 1""")
    )
    result["columns"] = (
        query("""SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull,a.attidentity,a.attgenerated,
        pg_get_expr(d.adbin,d.adrelid) AS default_expression FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname=:s AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2""")
    )
    result["constraints"] = (
        query("""SELECT co.conname,co.contype,co.convalidated,co.condeferrable,co.condeferred,
        pg_get_constraintdef(co.oid) AS definition FROM pg_constraint co JOIN pg_namespace n ON n.oid=co.connamespace
        WHERE n.nspname=:s ORDER BY 1""")
    )
    result["indexes"] = (
        query("""SELECT c.relname,indexname,indexdef,i.indisvalid,i.indisready FROM pg_indexes p
        JOIN pg_namespace n ON n.nspname=p.schemaname JOIN pg_class c ON c.relname=p.indexname AND c.relnamespace=n.oid
        JOIN pg_index i ON i.indexrelid=c.oid WHERE p.schemaname=:s ORDER BY indexname""")
    )
    result["enums"] = (
        query("""SELECT t.typname,e.enumlabel,e.enumsortorder::float FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid
        JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=:s ORDER BY 1,3""")
    )
    rows = query("""SELECT p.proname,pg_get_function_identity_arguments(p.oid) AS arguments,p.prokind,p.prosecdef,p.proconfig,
        pg_get_functiondef(p.oid) AS definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:s ORDER BY 1,2""")
    result["routines"] = [
        {
            **{k: v for k, v in r.items() if k != "definition"},
            "definition_sha256": hashlib.sha256(r["definition"].encode()).hexdigest(),
        }
        for r in rows
    ]
    # Internal FK trigger names contain installation-specific OIDs. Bind their
    # owning constraint/type/function rather than freezing an incidental OID.
    result["triggers"] = (
        query("""SELECT c.relname,CASE WHEN t.tgisinternal THEN NULL ELSE t.tgname END AS name,
        t.tgisinternal,t.tgtype,t.tgenabled,t.tgfoid::regprocedure::text AS function,co.conname,
        CASE WHEN t.tgisinternal THEN NULL ELSE pg_get_triggerdef(t.oid) END AS definition
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_constraint co ON co.oid=t.tgconstraint WHERE n.nspname=:s
        ORDER BY c.relname,t.tgisinternal,co.conname,t.tgtype,t.tgfoid::regprocedure::text""")
    )
    result["sequences"] = (
        query("""SELECT sequencename,data_type::text,start_value,min_value,max_value,increment_by,cycle,cache_size
        FROM pg_sequences WHERE schemaname=:s ORDER BY 1""")
    )
    return result


def validate(connection, *, absent_ok=False):
    if not schema_exists(connection):
        require(absent_ok, "workflow_schema_missing")
        return None
    actual = catalog(connection)
    require(actual == frozen_catalog(), "workflow_schema_requires_review")
    owner = connection.execute(
        text(
            "SELECT pg_get_userbyid(nspowner)=current_user FROM pg_namespace WHERE nspname=:s"
        ),
        {"s": INKSEQ_WORKFLOW_SCHEMA},
    ).scalar_one()
    require(owner is True, "workflow_schema_owner_changed")
    version = (
        connection.exec_driver_sql(
            'SELECT version FROM "inkseq_workflows".dbos_migrations LIMIT 2'
        )
        .scalars()
        .all()
    )
    require(version == [123], "workflow_sdk_revision_requires_review")
    return dict(
        schema=INKSEQ_WORKFLOW_SCHEMA,
        sdk_version=WORKFLOW_SDK_VERSION,
        sdk_migration=123,
        catalog_sha256=hashlib.sha256(canonical(actual)).hexdigest(),
    )
