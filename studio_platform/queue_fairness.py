"""Per-owner FIFO rotation over the existing scheduler cursor, without a queue.

Claims hold the original pool row lock and advance last_owner in the same SQL
transaction as the attempt. Advisory reads use the same order without writes.
"""

from collections import defaultdict

from sqlalchemy import select

from .capacity_settings import adopted
from .repository import scheduler_state, spending_settings
from .spending_limits import ADMIN_OWNER


def fair_generation_order(connection, ordered, pool, active, owner_key):
    """Replace only adopted tenants' positions; keep legacy candidates in place."""
    if not ordered:
        return ordered
    tenants = {job["tenant_id"] for job in ordered}
    enabled = {
        row["tenant_id"]
        for row in connection.execute(
            select(spending_settings).where(
                spending_settings.c.tenant_id.in_(tenants),
                spending_settings.c.owner_id == ADMIN_OWNER,
            )
        ).mappings()
        if adopted(row)
        and row["value"].get("per_user_machine_share_policy") == "equal_active_users"
        and row["value"].get("fair_share", True) is True
    }
    if not enabled:
        return ordered
    last_owner = connection.execute(
        select(scheduler_state.c.last_owner).where(scheduler_state.c.pool == pool)
    ).scalar_one_or_none()
    groups = defaultdict(list)
    for job in sorted(ordered, key=lambda job: (job["created_at"], job["id"])):
        if job["tenant_id"] in enabled:
            groups[owner_key(job)].append(job)

    # Stable ring survives restarts and a departing last owner. Occupancy gives
    # a user without a slot precedence, without forbidding parallel work when
    # other eligible owners already have the same number of active attempts.
    owners = sorted(
        groups,
        key=lambda key: (
            active.get(key, 0),
            bool(last_owner and key <= last_owner),
            key,
        ),
    )
    ranks = {key: rank for rank, key in enumerate(owners)}
    fair = iter(
        job
        for _, _, job in sorted(
            (depth, ranks[key], job)
            for key, group in groups.items()
            for depth, job in enumerate(group)
        )
    )
    return [next(fair) if job["tenant_id"] in enabled else job for job in ordered]
