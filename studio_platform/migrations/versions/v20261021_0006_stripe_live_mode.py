"""Frozen LIVE reader shape; adoption is a separate guarded operation.

The 005 purchase/event fields, identities and bounds remain unchanged. Only the
two test-only mode checks become integer 0/1 checks. Importing/building this
definition performs no IO and does not import Stripe or Alembic.
"""

import sqlalchemy as sa

from studio_platform.migrations.versions import (
    v20261020_0005_stripe_payments as previous,
)

revision = "20261021_0006"
down_revision = previous.revision
branch_labels = None
depends_on = None

MODE_CONSTRAINTS = {
    "platform_payments": (
        "ck_platform_payments_test_mode",
        "ck_platform_payments_mode",
    ),
    "platform_stripe_events": (
        "ck_platform_stripe_events_test_mode",
        "ck_platform_stripe_events_mode",
    ),
}
MODE_CHECK = "livemode IN (0, 1)"


def build(operations):
    class LiveModeCollector:
        def create_table(self, name, *columns, **kwargs):
            old_name, new_name = MODE_CONSTRAINTS[name]
            changed = 0
            result = []
            for value in columns:
                if isinstance(value, sa.CheckConstraint) and value.name == old_name:
                    value = sa.CheckConstraint(MODE_CHECK, name=new_name)
                    changed += 1
                result.append(value)
            if changed != 1:
                raise RuntimeError("payment_live_frozen_shape_mismatch")
            return operations.create_table(name, *result, **kwargs)

    previous.build(LiveModeCollector())


def upgrade():
    from alembic import context, op

    if (
        context.is_offline_mode()
        or context.config.attributes.get("verified_payment_live_mode") is not True
        or op.get_bind().dialect.name != "postgresql"
        or not op.get_bind().in_transaction()
    ):
        raise RuntimeError("payment_live_verified_adoption_required")
    for table, (old_name, new_name) in MODE_CONSTRAINTS.items():
        op.drop_constraint(old_name, table, type_="check")
        op.create_check_constraint(new_name, table, MODE_CHECK)


def downgrade():
    raise RuntimeError("payment_live_destructive_downgrade_requires_review")
