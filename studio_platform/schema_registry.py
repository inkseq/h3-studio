"""Explicit platform schema inventory; importing this module performs no IO.

This registry does not create or migrate a database. Startup's existing owners
remain authoritative until the frozen Alembic baseline is separately adopted.
"""


def metadata_families(*, include_identity=False):
    # These three modules extend repository.metadata; register them before
    # collecting it, independent of which API/worker happened to import first.
    from . import operator_capacity, capacity_market, dstack_workspace
    from .repository import metadata as repository
    from .assets import metadata as assets
    from .storage_asset_journal import _schema as asset_journal
    from .storage_multipart import _metadata as multipart
    from .artifact_writer import _schema as artifact_writer
    from .batches import metadata as batches
    from .guided import metadata as guided
    from .project_activity import metadata as project_activity
    from .quick_chat import metadata as quick_chat
    from .auth import metadata as auth
    from .agent_connect import metadata as agent_connect

    families = dict(
        repository=repository,
        assets=assets,
        asset_journal=asset_journal,
        multipart=multipart,
        artifact_writer=artifact_writer,
        batches=batches,
        guided=guided,
        project_activity=project_activity,
        quick_chat=quick_chat,
        auth=auth,
        agent_connect=agent_connect,
    )
    if include_identity:
        from .identity_schema import metadata as identity

        families["identity"] = identity
    return families


def platform_metadata(*, include_identity=False):
    """One metadata view for validation, including auth but not legacy DBs."""
    from sqlalchemy import MetaData

    result = MetaData()
    for family in metadata_families(include_identity=include_identity).values():
        for table in family.tables.values():
            if table.key in result.tables:
                raise ValueError("duplicate_platform_table")
            table.to_metadata(result)
    return result
