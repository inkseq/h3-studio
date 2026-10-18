"""No generic online Alembic connection: only the verified adoption caller.

No URL, logging configuration, startup constructor or credentials live here.
"""

from alembic import context


dialect = context.config.attributes.get("dialect_name", "postgresql")
if dialect not in {"postgresql", "sqlite"}:
    raise RuntimeError("baseline_dialect_not_supported")
if context.is_offline_mode():
    context.configure(dialect_name=dialect, literal_binds=True)
else:
    connection = context.config.attributes.get("verified_baseline_connection")
    mode = context.config.attributes.get("verified_baseline_mode")
    if (
        connection is None
        or not connection.in_transaction()
        or mode not in {"stamp", "upgrade"}
    ):
        raise RuntimeError("baseline_verified_adoption_required")
    context.configure(
        connection=connection,
        version_table_schema=context.config.attributes.get("version_table_schema"),
    )
with context.begin_transaction():
    context.run_migrations()
