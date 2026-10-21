"""Absent-only owner bindings from protected policies; never grant USD allowance."""

import string
import uuid

from sqlalchemy import insert, select

from .budget_policy import fixed_cap_retired
from .credits_pricing import PricingError
from .execution_profiles import read_profiles
from .identity import lock_account_gate
from .identity_schema import admin_audit, users
from .repository import Conflict, budget_accounts, identifier


def owner_accounts(settings, owner):
    """Read trusted templates, excluding shared and project-specific accounts."""
    from .execution_policy import read_policy

    policies = list(
        read_profiles(getattr(settings, "execution_profiles_file", None)).values()
    )
    legacy = read_policy(getattr(settings, "execution_policy_file", None))
    if legacy is not None:
        policies.append(legacy)
    result = set()
    for policy in policies:
        for template in policy["budget_accounts"]:
            fields = {
                field
                for _, field, _, _ in string.Formatter().parse(template)
                if field is not None
            }
            if "owner_id" not in fields or "project_id" in fields:
                continue
            account_id = template.format(tenant_id=settings.tenant_id, owner_id=owner)
            identifier(account_id)
            result.add(account_id)
    return sorted(result)


def ensure_owner_binding(connection, settings, user, *, actor_user_id, reason, now):
    """Caller owns the transaction; audit failure rolls back the entire boundary.

    No caller-supplied account IDs or amounts. The existing global account gate
    serializes insert-only onboarding/backfill, including concurrent callbacks.
    Existing financial fields are never updated, even when the limit is zero.
    """
    if not connection.in_transaction():
        raise ValueError("generation_budget_binding_requires_transaction")
    lock_account_gate(connection, exclusive=True)
    target = (
        connection.execute(
            select(users)
            .where(users.c.tenant == settings.tenant_id, users.c.id == user["id"])
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if (
        target is None
        or target["owner"] != user["owner"]
        or target["status"] != "active"
    ):
        raise PricingError(
            "generation_budget_owner_unavailable", "Account is unavailable.", 409
        )
    created, existing = [], []
    for account_id in owner_accounts(settings, target["owner"]):
        row = (
            connection.execute(
                select(budget_accounts)
                .where(budget_accounts.c.id == account_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is not None:
            if (row["tenant_id"], row["owner_id"], row["project_id"]) != (
                settings.tenant_id,
                target["owner"],
                None,
            ):
                raise Conflict("budget_scope_conflict")
            existing.append(account_id)
            continue
        value = {
            "id": account_id,
            "tenant_id": settings.tenant_id,
            "owner_id": target["owner"],
            "project_id": None,
            "limit_microusd": 0,
            "reserved_microusd": 0,
            "spent_microusd": 0,
        }
        if fixed_cap_retired(value):
            raise PricingError(
                "generation_budget_binding_requires_review",
                "This legacy budget needs explicit administrator review.",
                409,
            )
        connection.execute(insert(budget_accounts).values(**value))
        connection.execute(
            insert(admin_audit).values(
                id=str(uuid.uuid4()),
                tenant=settings.tenant_id,
                actor_user_id=actor_user_id,
                target_user_id=target["id"],
                action="generation budget bound",
                reason=reason,
                before={},
                after={**value, "allowance_granted": False},
                created_at=now,
            )
        )
        created.append(account_id)
    return {"user_id": target["id"], "created": created, "existing": existing}
