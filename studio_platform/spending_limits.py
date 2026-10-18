"""Editable admission limits projected from the original reservation ledger.

Settings and attribution are not a second money ledger. Layered reservations
count once; unresolved charges carry forward until ordinary reconciliation.
All checks and the original reservation commit in the same SQL transaction.
"""
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import math
from zoneinfo import ZoneInfo

from sqlalchemy import and_, insert, or_, select, update

from .repository import (BudgetExceeded, Conflict, NotFound, budget_accounts,
    budget_reservations, jobs, money, spending_settings, spending_origins,
    spending_confirmations)
from .dstack_test_budget import day_window

USER_DEFAULTS = {"daily_limit_microusd": 0, "ask_above_microusd": 0, "connection_limits": {}}
ADMIN_DEFAULTS = {"test_task_limit_microusd": 20_000_000,
    "max_hourly_price_microusd": 1_000_000, "fleet_hourly_limit_microusd": 1_000_000, "idle_shutdown_seconds": 600,
    "max_runtime_seconds": 7200}
ADMIN_OWNER = "__admin__"


@dataclass(frozen=True)
class SubmissionSpendingCheck:
    required: bool
    version: int

    def __bool__(self):
        return self.required


def admin_defaults(service=None):
    if service is None:
        return dict(ADMIN_DEFAULTS)
    policy, profiles = service.policy, service.profiles.values()
    guard = policy.get("test_gpu_guard", {})
    return {"test_task_limit_microusd": guard.get("task_limit_microusd", guard.get("daily_limit_microusd", ADMIN_DEFAULTS["test_task_limit_microusd"])),
        "max_hourly_price_microusd": max((p["max_price_microusd"] for p in profiles), default=ADMIN_DEFAULTS["max_hourly_price_microusd"]),
        "fleet_hourly_limit_microusd": policy.get("max_hourly_cost_microusd", ADMIN_DEFAULTS["fleet_hourly_limit_microusd"]),
        "idle_shutdown_seconds": policy.get("idle_shutdown_seconds", ADMIN_DEFAULTS["idle_shutdown_seconds"]),
        "max_runtime_seconds": guard.get("max_runtime_seconds", ADMIN_DEFAULTS["max_runtime_seconds"])}


class SpendingConfirmationRequired(Conflict):
    def __init__(self, plan_id, amount, *, plan_ids=None):
        super().__init__("spending_confirmation_required")
        self.plan_id, self.amount = plan_id, amount
        self.plan_ids = plan_ids or [plan_id]


def lock_owner(connection, tenant, owner):
    if connection.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(f"spending:{tenant}:{owner}".encode()).digest()[:8], "big", signed=True)
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (key,))


def read_settings(connection, tenant, owner, *, admin_defaults=None):
    row = connection.execute(select(spending_settings).where(
        spending_settings.c.tenant_id == tenant, spending_settings.c.owner_id == owner)).mappings().first()
    defaults = (admin_defaults or ADMIN_DEFAULTS) if owner == ADMIN_OWNER else USER_DEFAULTS
    value = dict(row["value"]) if row else {**defaults, **({"connection_limits": {}} if owner != ADMIN_OWNER else {})}
    return {"version": row["version"] if row else 0,
            "value": canonical_admin(value) if owner == ADMIN_OWNER else value}


def canonical_admin(value):
    """Read old audited settings without a migration or changing their cap."""
    if isinstance(value, dict) and "test_daily_limit_microusd" in value and "test_task_limit_microusd" not in value:
        value = {**value, "test_task_limit_microusd": value["test_daily_limit_microusd"]}
        del value["test_daily_limit_microusd"]
    return value


def validate_settings(value, *, admin=False):
    if admin:
        value = canonical_admin(value)
    fields = ADMIN_DEFAULTS if admin else USER_DEFAULTS
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("spending_settings_invalid")
    for key in set(fields) - {"connection_limits"}:
        money(value[key])  # rejects bool, negative and storage overflow
        if admin and value[key] == 0:
            raise ValueError("spending_settings_invalid")
    if admin:
        if not 1 <= value["idle_shutdown_seconds"] <= 86400 or not 60 <= value["max_runtime_seconds"] <= 86400:
            raise ValueError("spending_settings_invalid")
    else:
        limits = value["connection_limits"]
        if not isinstance(limits, dict) or len(limits) > 1000:
            raise ValueError("spending_settings_invalid")
        for key, cap in limits.items():
            if not isinstance(key, str) or not key.startswith("key:") or len(key) > 200:
                raise ValueError("spending_settings_invalid")
            money(cap)
    return value


def daily_projection(connection, tenant, owner, now, *, actor_id=None):
    start, _ = day_window(now)
    # A job's original owner also covers historical shared-only reservations.
    query = select(budget_reservations, jobs.c.actor_id.label("job_actor"),
        spending_origins.c.actor_id.label("origin_actor")).join(
        budget_accounts, budget_accounts.c.id == budget_reservations.c.account_id).outerjoin(
        jobs, and_(budget_reservations.c.reference_type == "job", jobs.c.id == budget_reservations.c.reference_id)).outerjoin(
        spending_origins, and_(spending_origins.c.reference_type == budget_reservations.c.reference_type,
            spending_origins.c.reference_id == budget_reservations.c.reference_id)).where(
        budget_accounts.c.tenant_id == tenant, or_(budget_accounts.c.owner_id == owner,
            and_(jobs.c.tenant_id == tenant, jobs.c.owner_id == owner),
            and_(spending_origins.c.tenant_id == tenant, spending_origins.c.owner_id == owner),
            and_(budget_accounts.c.owner_id.is_(None), jobs.c.owner_id.is_(None), spending_origins.c.owner_id.is_(None))))
    amounts = {}
    for row in connection.execute(query).mappings():
        attribution = row["origin_actor"] or row["job_actor"]
        # Historic Quick Chat collapsed actors. Its unknown attribution cannot
        # be used to promise spare capacity on any individual connection.
        if actor_id and attribution not in (actor_id, None, "quick-chat-execution"):
            continue
        if row["state"] in {"reserved", "invoice_pending"}:
            amount = money(row["amount_microusd"])
            if row["actual_cost_microusd"] is not None:
                raise BudgetExceeded("spending_usage_unconfirmed")
        elif row["state"] in {"settled", "released"}:
            amount = money(row["actual_cost_microusd"])
            if row["settled_at"] is not None and max(row["created_at"], row["settled_at"]) < start:
                continue
        else:
            raise BudgetExceeded("spending_usage_unconfirmed")
        key = (row["reference_type"], row["reference_id"])
        amounts[key] = max(amounts.get(key, 0), amount)
    return sum(amounts.values())


def admit_spending(repo, connection, scope, reference_type, reference_id, amount, *, actor_id, record_origin=True, confirmation_required=False, spending_settings_version=None):
    lock_owner(connection, scope.tenant_id, scope.owner_id)
    origin = connection.execute(select(spending_origins).where(
        spending_origins.c.reference_type == reference_type,
        spending_origins.c.reference_id == reference_id)).mappings().first()
    if origin:
        if (origin['tenant_id'], origin['owner_id']) != (scope.tenant_id, scope.owner_id):
            raise Conflict('spending_origin_changed')
        actor_id = origin['actor_id']
    increment = amount
    if record_origin and origin:
        # A new budget layer attributes the same obligation; it does not buy it
        # again. Rolling instance funding passes a true increment separately.
        previous = list(connection.execute(select(budget_reservations).where(
            budget_reservations.c.reference_type == reference_type,
            budget_reservations.c.reference_id == reference_id)).mappings())
        if any(row['state'] != 'reserved' for row in previous):
            raise Conflict('budget_already_settled')
        increment = max(0, amount - max((money(row['amount_microusd']) for row in previous), default=0))
    if (increment or not origin) and actor_id.startswith('key:'):
        from .agent_management import paused,connection_for_actor
        from .auth import personal_keys
        grant=connection_for_actor(connection,scope.tenant_id,scope.owner_id,actor_id)
        if grant is not None:
            import json
            if json.loads(grant['authorization']).get('permission')=='organise':raise Conflict('connection_permission_denied')
            key=connection.execute(select(personal_keys).where(personal_keys.c.tenant==scope.tenant_id,
                personal_keys.c.owner==scope.owner_id,personal_keys.c.id==actor_id[4:]).with_for_update()).mappings().first()
            if key is None or key['revoked_at'] is not None or key['expires_at']<=repo.clock() or grant['revoked_at'] is not None:
                raise Conflict('connection_unavailable')
            if paused(connection,scope.tenant_id,scope.owner_id,actor_id[4:]):raise Conflict('connection_paused')
    settings = read_settings(connection, scope.tenant_id, scope.owner_id)
    if spending_settings_version is not None and settings["version"] != spending_settings_version:
        raise Conflict("spending_settings_changed")
    value, now = settings["value"], repo.clock()
    if increment and value["daily_limit_microusd"] and daily_projection(connection, scope.tenant_id, scope.owner_id, now) + increment > value["daily_limit_microusd"]:
        raise BudgetExceeded("user_daily_limit_reached")
    cap = value["connection_limits"].get(actor_id, 0)
    if increment and cap and daily_projection(connection, scope.tenant_id, scope.owner_id, now, actor_id=actor_id) + increment > cap:
        raise BudgetExceeded("connection_daily_limit_reached")
    if reference_type == "job" and (not origin or increment) and (confirmation_required or value["ask_above_microusd"]):
        job = connection.execute(select(jobs).where(jobs.c.id == reference_id)).mappings().one()
        from .repository import plans
        plan = connection.execute(select(plans).where(plans.c.id == job["plan_id"])).mappings().one()
        condition = and_(spending_confirmations.c.tenant_id == scope.tenant_id,
            spending_confirmations.c.owner_id == scope.owner_id, spending_confirmations.c.actor_id == actor_id,
            spending_confirmations.c.plan_id == plan["id"])
        confirmation = connection.execute(select(spending_confirmations).where(condition).with_for_update()).mappings().first()
        valid = confirmation and confirmation["plan_hash"] == plan["plan_hash"] and confirmation["settings_version"] == settings["version"] and confirmation["expires_at"] > now and confirmation["consumed_reference"] in (None, reference_id) and amount <= plan["estimated_cost_microusd"]
        if not valid and (confirmation_required or amount > value["ask_above_microusd"]):
            raise SpendingConfirmationRequired(plan["id"], amount)
        if valid:
            connection.execute(update(spending_confirmations).where(condition).values(consumed_reference=reference_id))
    if record_origin and not origin:
        connection.execute(insert(spending_origins).values(reference_type=reference_type,
            reference_id=reference_id, tenant_id=scope.tenant_id, owner_id=scope.owner_id,
            actor_id=actor_id, created_at=now))


_USAGE_FIELDS = ("settled_today_microusd", "reserved_microusd",
    "awaiting_invoice_estimate_microusd", "awaiting_invoice_count",
    "prior_day_pending_estimate_microusd", "legacy_settled_unknown_date_microusd",
    "unconfirmed_exposure_microusd", "unconfirmed_reference_count", "exposure_microusd")


def empty_usage_breakdown(*, actor_id=None, connected=True):
    """Display-only ledger amounts; zero is not a supplier-cost assertion."""
    value = {"schema_version": 1, "attribution_scope": "connection" if actor_id else "account",
        "attribution_status": "recorded" if connected else "not_connected",
        **dict.fromkeys(_USAGE_FIELDS, 0),
        "by_kind": {kind: dict.fromkeys(_USAGE_FIELDS, 0) for kind in ("job", "instance")}}
    if not actor_id:
        # The first amount is disjoint from account exposure. The second is a
        # subset of known-owner exposure, never another additive cost layer.
        value.update(unattributed_exposure_microusd=0, unattributed_actor_exposure_microusd=0)
    return value


def _display_reference(rows, start, now):
    """One original obligation, regardless of its owner/global account layers."""
    value = dict.fromkeys(_USAGE_FIELDS, 0)
    states = {row["state"] for row in rows}
    amounts = [money(row["amount_microusd"]) for row in rows]
    actuals = [money(row["actual_cost_microusd"]) for row in rows
               if row["actual_cost_microusd"] is not None]
    amount = max(amounts + actuals)
    state = next(iter(states)) if len(states) == 1 else None
    times = [row["created_at"] for row in rows]
    valid_times = all(type(at) in (int, float) and math.isfinite(at) and 0 <= at <= now for at in times)
    if state in {"reserved", "invoice_pending"} and not actuals and valid_times:
        field = "reserved_microusd" if state == "reserved" else "awaiting_invoice_estimate_microusd"
        value[field] = amount
        if state == "invoice_pending":
            value["awaiting_invoice_count"] = 1
            if min(times) < start:
                value["prior_day_pending_estimate_microusd"] = amount
    elif state in {"settled", "released"} and len(actuals) == len(rows) and len(set(actuals)) == 1 and valid_times:
        settled = {row["settled_at"] for row in rows}
        if settled == {None}:
            value["legacy_settled_unknown_date_microusd"] = actuals[0]
        elif len(settled) == 1 and all(type(at) in (int, float) and math.isfinite(at) and 0 <= at <= now for at in settled):
            if next(iter(settled)) >= start:
                value["settled_today_microusd"] = actuals[0]
        else:
            state = None
    else:
        state = None
    if state is None:
        # Partial/inconsistent historical account layers stay visibly unknown;
        # do not add both their estimate and actual or silently pick a state.
        value["unconfirmed_exposure_microusd"] = amount
        value["unconfirmed_reference_count"] = 1
    value["exposure_microusd"] = sum(value[field] for field in (
        "settled_today_microusd", "reserved_microusd", "awaiting_invoice_estimate_microusd",
        "legacy_settled_unknown_date_microusd", "unconfirmed_exposure_microusd"))
    return value


def usage_breakdown(connection, tenant, owner, now, *, actor_id=None, excluded_references=None):
    """Strict display attribution, independent of conservative admission caps.

    Account exposure includes known-owner obligations with unknown old actors.
    A connection receives only its original exact actor, never another actor's
    or a shared/unknown dollar amount. All reads preserve the original ledger.
    """
    start, _ = day_window(now)
    query = select(budget_reservations, budget_accounts.c.owner_id.label("account_owner"),
        jobs.c.tenant_id.label("job_tenant"), jobs.c.owner_id.label("job_owner"), jobs.c.actor_id.label("job_actor"),
        spending_origins.c.tenant_id.label("origin_tenant"), spending_origins.c.owner_id.label("origin_owner"),
        spending_origins.c.actor_id.label("origin_actor")).join(
        budget_accounts, budget_accounts.c.id == budget_reservations.c.account_id).outerjoin(
        jobs, and_(budget_reservations.c.reference_type == "job", jobs.c.id == budget_reservations.c.reference_id)).outerjoin(
        spending_origins, and_(spending_origins.c.reference_type == budget_reservations.c.reference_type,
            spending_origins.c.reference_id == budget_reservations.c.reference_id)).where(budget_accounts.c.tenant_id == tenant)
    references = {}
    for row in connection.execute(query).mappings():
        references.setdefault((row["reference_type"], row["reference_id"]), []).append(row)
    value = empty_usage_breakdown(actor_id=actor_id)
    if excluded_references is not None:
        value.update(waived_reference_count=0, waived_exposure_microusd=0)
    for (kind, _), rows in references.items():
        owners = {row["account_owner"] for row in rows if row["account_owner"] is not None}
        tenant_conflict = False
        for row in rows:
            for source in ("job", "origin"):
                if row[source + "_owner"] is not None:
                    tenant_conflict |= row[source + "_tenant"] != tenant
                    owners.add(row[source + "_owner"])
        if owners and owner not in owners:
            continue
        if tenant_conflict or kind not in value["by_kind"] or len(owners) > 1:
            raise BudgetExceeded("spending_usage_unconfirmed")
        row = rows[0]
        actor = row["origin_actor"] or row["job_actor"]
        actor = actor if actor not in (None, "quick-chat-execution") else None
        if actor_id and (owners != {owner} or actor != actor_id):
            continue
        current = _display_reference(rows, start, now)
        if excluded_references is not None and (kind, row["reference_id"]) in excluded_references:
            value["waived_reference_count"] += 1
            value["waived_exposure_microusd"] += current["exposure_microusd"]
            continue
        if not owners:
            value["unattributed_exposure_microusd"] += current["exposure_microusd"]
            continue
        if not actor_id and actor is None:
            value["unattributed_actor_exposure_microusd"] += current["exposure_microusd"]
        for field in _USAGE_FIELDS:
            value[field] += current[field]
            value["by_kind"][kind][field] += current[field]
    return value


def public_usage(connection, tenant, owner, now, *, actor_id=None):
    from .repository import billing_adjustments
    waived = {(r.reference_type, r.reference_id) for r in connection.execute(select(
        billing_adjustments.c.reference_type, billing_adjustments.c.reference_id).where(
            billing_adjustments.c.tenant_id == tenant, billing_adjustments.c.owner_id == owner,
            billing_adjustments.c.kind == "full_waiver"))}
    value = {"projected_microusd": daily_projection(connection, tenant, owner, now, actor_id=actor_id),
        "day": datetime.fromtimestamp(now, timezone.utc).astimezone(ZoneInfo("Australia/Sydney")).date().isoformat(),
        "timezone": "Australia/Sydney", "includes_unresolved_prior_days": True,
        "breakdown": usage_breakdown(connection, tenant, owner, now, actor_id=actor_id),
        "charge_breakdown": usage_breakdown(connection, tenant, owner, now, actor_id=actor_id, excluded_references=waived)}
    if actor_id is None:
        from .invoice_checks import public_reconciliation
        value["reconciliation"] = public_reconciliation(connection, tenant, owner, now)
    return value


def check_submission_confirmation(repo, tenant, principal, plan_ids):
    """Ask for the aggregate selected batch before admitting its first item."""
    from .repository import plans
    with repo.transaction() as connection:
        lock_owner(connection, tenant, principal.owner)
        settings = read_settings(connection, tenant, principal.owner)
        threshold = settings["value"]["ask_above_microusd"]
        if not threshold or not plan_ids:
            return SubmissionSpendingCheck(False, settings["version"])
        selected = []
        for plan_id in plan_ids:
            plan = connection.execute(select(plans).where(plans.c.id == plan_id,
                plans.c.tenant_id == tenant, plans.c.owner_id == principal.owner)).mappings().first()
            if not plan:
                raise NotFound("plan_not_found")
            selected.append(plan)
        total = sum(plan["estimated_cost_microusd"] for plan in selected)
        if total <= threshold:
            return SubmissionSpendingCheck(False, settings["version"])
        for plan in selected:
            grant = connection.execute(select(spending_confirmations).where(
                spending_confirmations.c.tenant_id == tenant, spending_confirmations.c.owner_id == principal.owner,
                spending_confirmations.c.actor_id == principal.actor_id, spending_confirmations.c.plan_id == plan["id"])).mappings().first()
            if not grant or grant["plan_hash"] != plan["plan_hash"] or grant["settings_version"] != settings["version"] or grant["expires_at"] <= repo.clock() or grant["consumed_reference"]:
                raise SpendingConfirmationRequired(selected[0]["id"], total, plan_ids=[p["id"] for p in selected])
        return SubmissionSpendingCheck(True, settings["version"])


def unquoted_spend_blocked(connection, tenant, principal, *, now=None):
    import time
    import json
    from .agent_management import connection_for_actor,paused
    from .auth import personal_keys
    lock_owner(connection,tenant,principal.owner)
    grant=connection_for_actor(connection,tenant,principal.owner,principal.actor_id)
    if grant is not None:
        key=connection.execute(select(personal_keys).where(personal_keys.c.tenant==tenant,
            personal_keys.c.owner==principal.owner,personal_keys.c.id==grant['key_id'])).mappings().first()
        if (grant['revoked_at'] is not None or key is None or key['revoked_at'] is not None
                or key['expires_at']<=(time.time() if now is None else now)
                or paused(connection,tenant,principal.owner,grant['key_id'])
                or json.loads(grant['authorization']).get('permission')=='organise'):
            return True
    value = read_settings(connection, tenant, principal.owner)["value"]
    return bool(value["daily_limit_microusd"] or value["ask_above_microusd"]
                or value["connection_limits"].get(principal.actor_id, 0))
