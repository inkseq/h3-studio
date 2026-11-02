"""Trusted business-backup schemas and stable errors; no construction-time I/O."""

from __future__ import annotations
from .schema_version import (
    PAYMENT_REVISION,
    CREDIT_ADMISSION_REVISIONS,
)

MIB = 1024 * 1024

AUTH_TABLES = frozenset(
    {
        "platform_accounts",
        "platform_sessions",
        "platform_service_clients",
        "platform_login_limits",
        "platform_personal_api_keys",
        "platform_agent_connections",
        "platform_agent_exchange_limits",
        "platform_agent_connection_audit",
    }
)


class BackupError(Exception):
    """Stable code only; never attach DB URLs, subprocess output, or secrets."""


def _schemas(
    *,
    include_identity=True,
    include_credits=True,
    include_payments=True,
    payment_revision=PAYMENT_REVISION,
):
    # Register the same additive business tables as the application, without
    # constructing services or creating/migrating a source schema.
    from . import operator_capacity, capacity_market, dstack_workspace  # noqa: F401 -- register additive backup schema tables
    from .repository import metadata
    from .assets import metadata as assets
    from .storage_asset_journal import _schema as quota
    from .storage_multipart import _metadata as multipart
    from .artifact_writer import _schema as writer
    from .batches import metadata as batches
    from .guided import metadata as guided
    from .quick_chat import metadata as quick_chat
    from .project_activity import metadata as project_activity
    from .identity_schema import metadata as identity
    from .credit_schema import metadata as credits
    from .payment_schema import frozen_metadata as payment_metadata

    if payment_revision in CREDIT_ADMISSION_REVISIONS:
        from sqlalchemy import MetaData
        from .migrations.versions.v20261022_0007_credit_admission import extend

        original = metadata
        metadata = MetaData()
        for table in original.tables.values():
            table.to_metadata(metadata)
        extend(metadata)
    payments = payment_metadata(revision=payment_revision)

    return (
        (
            metadata,
            assets,
            quota,
            multipart,
            writer,
            batches,
            guided,
            quick_chat,
            project_activity,
        )
        + ((identity,) if include_identity else ())
        + ((credits,) if include_credits else ())
        + ((payments,) if include_payments else ())
    )


def _tables(
    *,
    include_identity=True,
    include_credits=True,
    include_payments=True,
    payment_revision=PAYMENT_REVISION,
):
    return {
        table.name: table
        for schema in _schemas(
            include_identity=include_identity,
            include_credits=include_credits,
            include_payments=include_payments,
            payment_revision=payment_revision,
        )
        for table in schema.sorted_tables
    }


# Preserve the original public exception identity for retained serialized errors.
BackupError.__module__ = "studio_platform.backup"
