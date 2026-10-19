"""Pinned DBOS3.3.0 isolated namespace; business baseline remains unchanged."""

revision = '20261019_0002'
down_revision = '20261019_0001'
branch_labels = None
depends_on = None


def upgrade():
    from alembic import context, op
    from studio_platform.workflow_schema import initialize

    if (
        context.is_offline_mode()
        or context.config.attributes.get('verified_workflow_mode') is not True
    ):
        raise RuntimeError('workflow_verified_adoption_required')
    initialize(op.get_bind())


def downgrade():
    raise RuntimeError('workflow_destructive_downgrade_requires_review')
