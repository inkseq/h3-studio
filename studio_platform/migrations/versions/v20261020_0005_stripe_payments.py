"""Frozen test-payment family; only guarded explicit adoption may write DDL."""

import sqlalchemy as sa

revision = "20261020_0005"
down_revision = "20261019_0004"
branch_labels = None
depends_on = None


def build(operations):
    operations.create_table(
        "platform_payments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("pack_id", sa.String(64), nullable=False),
        sa.Column("pack_revision", sa.String(64), nullable=False),
        sa.Column("pack_snapshot", sa.JSON(), nullable=False),
        sa.Column("price_id", sa.String(200), nullable=False),
        sa.Column("credits", sa.BigInteger(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("account_scope", sa.String(200), nullable=False),
        sa.Column("livemode", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("terms_version", sa.String(64), nullable=False),
        sa.Column("terms_accepted_at", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("checkout_started_at", sa.Float(), nullable=True),
        sa.Column("checkout_bound_at", sa.Float(), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(200), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(200), nullable=True),
        sa.Column("stripe_charge_id", sa.String(200), nullable=True),
        sa.Column("checkout_url", sa.String(2048), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("paid_at", sa.Float(), nullable=True),
        sa.Column("receipt_url", sa.String(2048), nullable=True),
        sa.Column("refunded_cents", sa.BigInteger(), nullable=False),
        sa.Column("refund_credits", sa.BigInteger(), nullable=False),
        sa.Column("clawed_back_credits", sa.BigInteger(), nullable=False),
        sa.Column("unmet_credits", sa.BigInteger(), nullable=False),
        sa.Column("dispute_open", sa.Integer(), nullable=False),
        sa.Column("stripe_dispute_id", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant",
            "user_id",
            "idempotency_key",
            name="uq_platform_payments_idempotency",
        ),
        sa.UniqueConstraint(
            "stripe_checkout_session_id",
            name="uq_platform_payments_session",
        ),
        sa.UniqueConstraint(
            "stripe_payment_intent_id",
            name="uq_platform_payments_intent",
        ),
        sa.UniqueConstraint("stripe_charge_id", name="uq_platform_payments_charge"),
        sa.UniqueConstraint("stripe_dispute_id", name="uq_platform_payments_dispute"),
        sa.CheckConstraint("livemode = 0", name="ck_platform_payments_test_mode"),
        sa.CheckConstraint(
            "status IN ('created', 'paid', 'failed', 'expired', 'refunded', 'disputed')",
            name="ck_platform_payments_status",
        ),
        sa.CheckConstraint(
            "credits > 0 AND amount_cents > 0 AND refunded_cents >= 0 "
            "AND refunded_cents <= amount_cents AND refund_credits >= 0 "
            "AND refund_credits <= credits AND clawed_back_credits >= 0 "
            "AND clawed_back_credits <= refund_credits AND unmet_credits >= 0 "
            "AND unmet_credits = refund_credits - clawed_back_credits",
            name="ck_platform_payments_refund_bounds",
        ),
        sa.CheckConstraint(
            "dispute_open >= 0 AND dispute_open <= 1",
            name="ck_platform_payments_dispute_flag",
        ),
    )
    operations.create_table(
        "platform_stripe_events",
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("tenant", sa.String(200), nullable=False),
        sa.Column("account_scope", sa.String(200), nullable=False),
        sa.Column("livemode", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("verified_payload", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.Column("processed_at", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payment_id", sa.String(36), nullable=True),
        sa.Column("reason_code", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint("livemode = 0", name="ck_platform_stripe_events_test_mode"),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'deferred', 'review')",
            name="ck_platform_stripe_events_status",
        ),
    )


def upgrade():
    from alembic import context, op

    if (
        context.is_offline_mode()
        or context.config.attributes.get("verified_payment_mode") is not True
        or op.get_bind().dialect.name != "postgresql"
        or not op.get_bind().in_transaction()
    ):
        raise RuntimeError("payment_verified_adoption_required")
    build(op)


def downgrade():
    raise RuntimeError("payment_destructive_downgrade_requires_review")
