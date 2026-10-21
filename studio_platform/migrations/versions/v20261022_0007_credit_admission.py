"""One additive role on original reservation rows; explicit adoption only."""

import re
import sqlalchemy as sa

revision = "20261022_0007"
down_revision = "20261021_0006"
branch_labels = depends_on = None
ROLE_CHECK = "budget_role IN ('accounting', 'enforced')"


def normalize_check(value):
    # Compare finite SQLite / pg_get_constraintdef forms. Quoted tokens remain
    # byte-exact: casts, spaces, parentheses and case INSIDE a literal matter.
    tokens = sql_tokens(str(value))
    forms = {sql_tokens(ROLE_CHECK)}
    for column in ("budget_role", "(budget_role)", "(budget_role)::text"):
        for cast in ("", "::text", "::character varying"):
            array = f"ARRAY['accounting'{cast}, 'enforced'{cast}]"
            for operand in (array, f"({array})::text[]"):
                expression = f"{column} = ANY ({operand})"
                for wrappers in range(3):
                    forms.add(sql_tokens("(" * wrappers + expression + ")" * wrappers))
    for wrappers in range(1, 3):
        forms.add(sql_tokens("(" * wrappers + ROLE_CHECK + ")" * wrappers))
    if tokens is not None and tokens in forms:
        return [("budget_role", "in", "accounting,enforced")]
    return None


def sql_tokens(value):
    """Whitespace only separates tokens; never rewrite a quoted token."""
    tokens, end = [], 0
    pattern = r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|::|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[()[\],=><.]"
    for match in re.finditer(pattern, value):
        if value[end : match.start()].strip():
            return None
        token = match.group()
        tokens.append(token if token[0] in "'\"" else token.lower())
        end = match.end()
    return tuple(tokens) if not value[end:].strip() else None


def sqlite_check(sql):
    """Extract exactly one named CHECK from CREATE SQL, preserving literals."""
    tokens = sql_tokens(sql)
    if tokens is None:
        return False
    prefix = ("constraint", "ck_budget_reservation_role", "check")
    starts = [
        index + 3
        for index in range(len(tokens) - 3)
        if tokens[index : index + 3] == prefix
    ]
    if len(starts) != 1 or tokens[starts[0]] != "(":
        return False
    start, depth = starts[0], 0
    for index in range(start, len(tokens)):
        depth += (tokens[index] == "(") - (tokens[index] == ")")
        if depth == 0:
            return normalize_check(" ".join(tokens[start + 1 : index])) is not None
    return False


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
