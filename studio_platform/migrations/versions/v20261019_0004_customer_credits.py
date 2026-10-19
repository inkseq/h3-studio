"""Frozen additive customer-credit DDL; guarded adoption is the only writer."""

import sqlalchemy as sa

revision = "20261019_0004"
down_revision = "20261019_0003"
branch_labels = None
depends_on = None


def build(operations):
    operations.create_table(
        "platform_credit_accounts",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.Column("held", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint(
            "balance >= 0 AND held >= 0 AND held <= balance AND version >= 0",
            name="ck_platform_credit_accounts_balances",
        ),
    )
    operations.create_table(
        "platform_credit_entries",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("held_delta", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("held_after", sa.BigInteger(), nullable=False),
        sa.Column("reference_type", sa.String(32), nullable=False),
        sa.Column("reference_id", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("price_revision", sa.String(64), nullable=True),
        sa.Column("origin", sa.String(200), nullable=True),
        sa.Column("receipt_url", sa.String(2048), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant",
            "user_id",
            "idempotency_key",
            name="uq_platform_credit_entries_idempotency",
        ),
        sa.CheckConstraint(
            "kind IN ('grant', 'purchase', 'hold', 'capture', 'release', 'refund', 'adjust', 'expire', 'waive')",
            name="ck_platform_credit_entries_kind",
        ),
        sa.CheckConstraint(
            "balance_after >= 0 AND held_after >= 0 AND held_after <= balance_after",
            name="ck_platform_credit_entries_balances",
        ),
    )
    operations.create_table(
        "platform_credit_prices",
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("rates", sa.JSON(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("published_by", sa.String(36), nullable=False),
        sa.Column("published_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "revision"),
    )


def upgrade():
    from alembic import context, op

    if (
        context.is_offline_mode()
        or context.config.attributes.get("verified_credit_mode") is not True
        or op.get_bind().dialect.name != "postgresql"
        or not op.get_bind().in_transaction()
    ):
        raise RuntimeError("credit_verified_adoption_required")
    build(op)


def downgrade():
    raise RuntimeError("credit_destructive_downgrade_requires_review")
