"""Four additive non-unique lookup indexes; no row rewrite or startup writer."""

revision = "20261026_0008"
down_revision = "20261022_0007"
branch_labels = depends_on = None


def upgrade():
    from alembic import context, op
    from studio_platform.lookup_indexes import plan

    if (
        context.is_offline_mode()
        or context.config.attributes.get("verified_lookup_indexes") is not True
        or op.get_bind().dialect.name != "postgresql"
        or not op.get_bind().in_transaction()
    ):
        raise RuntimeError("lookup_indexes_verified_adoption_required")
    for item in plan(op.get_bind()):
        if not item["existing"]:
            op.create_index(item["name"], item["table"], item["columns"], unique=False)


def downgrade():
    raise RuntimeError("lookup_index_downgrade_requires_review")
