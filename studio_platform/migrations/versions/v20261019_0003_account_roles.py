"""Frozen account-role DDL; only the verified adoption caller may execute it."""

import sqlalchemy as sa

revision = "20261019_0003"
down_revision = "20261019_0002"
branch_labels = None
depends_on = None


def build(operations):
    operations.create_table(
        "platform_users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("role", sa.String(10), nullable=False, server_default="user"),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("email_verified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("workos_user_id", sa.String(200), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("terms_version", sa.String(80), nullable=True),
        sa.Column("terms_accepted_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant", "owner", name="uq_platform_users_owner"),
        sa.UniqueConstraint(
            "tenant", "workos_user_id", name="uq_platform_users_workos"
        ),
        sa.CheckConstraint("role IN ('user', 'admin')", name="ck_platform_users_role"),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name="ck_platform_users_status"
        ),
        sa.CheckConstraint(
            "email_verified >= 0 AND email_verified <= 1",
            name="ck_platform_users_email_verified",
        ),
    )
    operations.create_table(
        "platform_admin_audit",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=False),
        sa.Column("target_user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade():
    from alembic import context, op

    if (
        context.is_offline_mode()
        or context.config.attributes.get("verified_identity_mode") is not True
        or op.get_bind().dialect.name != "postgresql"
        or not op.get_bind().in_transaction()
    ):
        raise RuntimeError("identity_verified_adoption_required")
    build(op)


def downgrade():
    raise RuntimeError("identity_destructive_downgrade_requires_review")
