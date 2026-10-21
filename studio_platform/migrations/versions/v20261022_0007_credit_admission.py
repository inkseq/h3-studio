"""One additive role on original reservation rows; explicit adoption only."""

import re
import sqlalchemy as sa

revision = "20261022_0007"
down_revision = "20261021_0006"
branch_labels = depends_on = None
ROLE_CHECK = "budget_role IN ('accounting', 'enforced')"


def normalize_check(value):
    compact = re.sub(
        r"\s+|[()]|::character varying|::text\[\]|::text", "", str(value)
    ).lower()
    if compact in {
        "budget_rolein'accounting','enforced'",
        "budget_role=anyarray['accounting','enforced']",
    }:
        return [("budget_role", "in", "accounting,enforced")]
    return None


def extend(metadata):
    table = metadata.tables["platform_budget_reservations"]
    table.append_column(
        sa.Column(
            "budget_role",
            sa.String(20),
            nullable=False,
            server_default="enforced",
        )
    )
    table.append_constraint(
        sa.CheckConstraint(ROLE_CHECK, name="ck_budget_reservation_role")
    )


def upgrade():
    from alembic import context, op

    if (
        context.is_offline_mode()
        or context.config.attributes.get("verified_credit_admission_mode") is not True
        or op.get_bind().dialect.name != "postgresql"
        or not op.get_bind().in_transaction()
    ):
        raise RuntimeError("credit_admission_verified_adoption_required")
    op.add_column(
        "platform_budget_reservations",
        sa.Column(
            "budget_role",
            sa.String(20),
            nullable=False,
            server_default="enforced",
        ),
    )
    op.create_check_constraint(
        "ck_budget_reservation_role", "platform_budget_reservations", ROLE_CHECK
    )


def downgrade():
    raise RuntimeError("credit_admission_destructive_downgrade_requires_review")
