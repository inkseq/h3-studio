"""Hand a queued, unsubmitted job to the hosted API (#931 step 3b, #918).

Workers still claim only an exact backend, pool and configuration match, so
binding a job to another route means rewriting those fields, inside the
caller's capacity-locked transaction. Credits stay frozen with the tier, and
settlement records the vendor's actual cost on the job's original accounts.
The route is chosen from the catalog at dispatch; nothing happens unless the
credential is installed and everyframe-routing.json switches the tier on.
"""

import json
from pathlib import Path

from sqlalchemy import select, update

from ..providers.hosted_models import BACKEND, CONFIGURATIONS, MODELS, POOL, chain
from .decide import HOSTED_WINDOW_SECONDS, hosted_has_first_claim, hosted_offload

EXPECTED_RUNTIME_S = 300
# Plan fields that describe the route do not survive a rebind: claims compare
# dispatch_backend and output_delivery with the worker's own spec.
KEPT = (
    "budget_account_ids",
    "capacity_policy_snapshot",
    "credit_quote",
    "deployment_profile_id",
    "fingerprint",
)


def routing(data_dir):
    """(tiers, per_window) from the host's routing file; off if absent or malformed.

    {"tiers": ["fast"], "per_window": 5}. Environments share one vendor key and
    limit, so staging keeps a small window (#918, owner 2026-11-01). Read at each
    decision, so a change needs no restart.
    """
    path = Path(data_dir) / "runtime-secrets" / "everyframe-routing.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        tiers = frozenset(value["tiers"])
        window = value["per_window"]
        if (
            not isinstance(value["tiers"], list)
            or type(window) is not int
            or not 0 <= window <= 40
            or not all(isinstance(tier, str) for tier in tiers)
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset(), 0
    return tiers, window


def hosted_plan(execution, model_id, now):
    """The job's plan bound to one hosted model; the original is kept inside."""
    plan = {key: execution[key] for key in KEPT if key in execution}
    plan.update(
        pool=POOL,
        backend=BACKEND,
        configuration_id=MODELS[model_id].configuration_id,
        quote_known=True,
        enabled=True,
        blockers=[],
        admission_state="queued",
        expected_runtime_s=EXPECTED_RUNTIME_S,
        routed_at=now,
        primary_plan=execution,
    )
    return plan


def tier_of(job):
    """The job's billing tier; None keeps it on our own machines."""
    from ..credits_pricing import PricingError, get_tier

    try:
        return get_tier(job["request"], execution=job["execution_plan"])
    except (PricingError, KeyError, TypeError, ValueError):
        return None


def next_plan(job, now):
    """The route after a refused or failed render, or None for a direct failure.

    A refusal is model-specific (#929), so a job never retries the same model.
    After the last model it returns to its original plan on our own machines
    and stays there.
    """
    execution = job["execution_plan"]
    primary = execution.get("primary_plan")
    if not isinstance(primary, dict):
        return None
    current = CONFIGURATIONS.get(execution.get("configuration_id"))
    tried = [*execution.get("hosted_tried", []), current]
    for model_id in chain(job["request"]):
        if model_id not in tried:
            return {**hosted_plan(primary, model_id, now), "hosted_tried": tried}
    return {**primary, "hosted_tried": tried}


def used_in_window(connection, tenant, now):
    """{owner_id: jobs routed hosted in the current vendor window}."""
    from ..repository import jobs

    used = {}
    for owner, execution in connection.execute(
        select(jobs.c.owner_id, jobs.c.execution_plan).where(
            jobs.c.tenant_id == tenant,
            jobs.c.pool == POOL,
            jobs.c.updated_at >= now - HOSTED_WINDOW_SECONDS,
        )
    ):
        routed = execution.get("routed_at")
        if isinstance(routed, (int, float)) and now - routed < HOSTED_WINDOW_SECONDS:
            used[owner] = used.get(owner, 0) + 1
    return used


def live_configurations(connection, now):
    """Hosted configurations with a registered, live slot."""
    from ..repository import registered_workers

    return {
        spec.get("configuration_id")
        for spec in connection.execute(
            select(registered_workers.c.spec).where(
                registered_workers.c.pool == POOL,
                registered_workers.c.state.in_(("ready", "busy")),
                registered_workers.c.expires_at > now,
            )
        ).scalars()
    }


def rebind(repo, connection, job, model_id, now):
    """Bind one queued job with no lease to the model; no-op if it just moved."""
    from ..repository import jobs

    moved = connection.execute(
        update(jobs)
        .where(
            jobs.c.id == job["id"],
            jobs.c.status == "queued",
            jobs.c.fence == job["fence"],
            jobs.c.lease_worker_id.is_(None),
        )
        .values(
            pool=POOL,
            execution_plan=hosted_plan(job["execution_plan"], model_id, now),
            expected_runtime_s=EXPECTED_RUNTIME_S,
            fence=job["fence"] + 1,
            not_before=now,
            updated_at=now,
        )
    ).rowcount
    if moved:
        repo._emit(
            connection,
            "capacity.hosted_route",
            job["id"],
            {"model": model_id, "from_pool": job["pool"]},
        )
    return bool(moved)


def offload(service, connection, queued, now):
    """Rebind what has waited too long; the rest stays for our own machines."""
    settings = service.settings
    if not getattr(settings, "everyframe_enabled", False):
        return queued
    tiers, window = routing(settings.data_dir)
    if not tiers:
        return queued
    live = live_configurations(connection, now)
    used = used_in_window(connection, settings.tenant_id, now)
    # A job that already went through the hosted chain stays on our machines.
    fresh = [item for item in queued if "hosted_tried" not in item[0]["execution_plan"]]
    moved = set()
    for job, _ in hosted_offload(fresh, tier_of, tiers, used, now, per_window=window):
        for model_id in chain(job["request"]):
            if MODELS[model_id].configuration_id in live:
                if rebind(service.repo, connection, job, model_id, now):
                    moved.add(job["id"])
                break
    return [item for item in queued if item[0]["id"] not in moved]


def hold_for_hosted(settings, job, now):
    """A cold job waits for the hosted API instead of starting a machine for it.

    Without this a machine boots for a job that goes hosted a minute later, and
    is paid for while it idles (found on staging, 2026-11-01). Once the grace
    passes with the job still queued, machines start as before.
    """
    if not getattr(settings, "everyframe_enabled", False):
        return False
    tiers, window = routing(settings.data_dir)
    if not tiers or not window or not chain(job["request"]):
        return False
    return hosted_has_first_claim(job, tier_of(job), tiers, now)
