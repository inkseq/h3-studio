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

from sqlalchemy import func, select, update

from ..providers.balances import BalanceObservation, read_everyframe_balance
from ..providers.hosted_models import BACKEND, CONFIGURATIONS, MODELS, POOL, chain
from .decide import (
    ECONOMY_WARMS_MACHINE_AT as ECONOMY_WARMS_MACHINE_AT,
    HOSTED_BALANCE_RESERVE_USD as HOSTED_BALANCE_RESERVE_USD,
    HOSTED_BALANCE_WARN_USD,
    HOSTED_WARMS_MACHINE_AT as HOSTED_WARMS_MACHINE_AT,
    HOSTED_WINDOW_SECONDS,
    SCENARIOS,
    hosted_has_first_claim,
    hosted_offload,
)

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


def _routing(data_dir):
    path = Path(data_dir) / "runtime-secrets" / "everyframe-routing.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        tiers = frozenset(value["tiers"])
        window = value["per_window"]
        first = value.get("hosted_first", False)
        if (
            not isinstance(value["tiers"], list)
            or type(window) is not int
            or not 0 <= window <= 40
            or not all(isinstance(tier, str) for tier in tiers)
            or type(first) is not bool
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return frozenset(), 0, False
    return tiers, window, first


def routing(data_dir):
    """(tiers, per_window) from the host's routing file; off if absent or malformed.

    {"tiers": ["fast"], "per_window": 5}. Environments share one vendor key and
    limit, so staging keeps a small window (#918, owner 2026-11-01). Read at each
    decision, so a change needs no restart.
    """
    return _routing(data_dir)[:2]


def hosted_first(data_dir):
    """Whether the file says `"hosted_first": true` (#1222, owner 2026-11-11).

    Then a job a live hosted model can take is routed on the tick that sees it
    and no machine of ours starts or warms for it; our machines stay for what
    hosted cannot render and for what its chain gave back.
    """
    return _routing(data_dir)[2]


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


def _economy():
    from ..repository import jobs

    return jobs.c.request["priority_class"].as_string() == "economy"


def used_in_window(connection, tenant, now, *, clicks_only=False):
    """{owner_id: hosted renders in the vendor window, plus any about to start}.

    Every submitted attempt counts, including ones a later model replaced: the
    vendor's limit counts each of them. `clicks_only` drops economy work, which
    never warms a machine by this count.
    """
    from ..repository import attempts, jobs

    used = {}
    submitted = select(jobs.c.owner_id).select_from(
        attempts.join(jobs, attempts.c.job_id == jobs.c.id)
    )
    waiting = select(jobs.c.owner_id).where(
        jobs.c.tenant_id == tenant,
        jobs.c.pool == POOL,
        jobs.c.status.in_(("queued", "claimed")),
    )
    if clicks_only:
        submitted = submitted.where(func.coalesce(_economy(), False).is_(False))
        waiting = waiting.where(func.coalesce(_economy(), False).is_(False))
    for (owner,) in connection.execute(
        submitted.where(
            jobs.c.tenant_id == tenant,
            attempts.c.worker_id.like("dbos-everyframe-%"),
            attempts.c.submission_started_at >= now - HOSTED_WINDOW_SECONDS,
        )
    ):
        used[owner] = used.get(owner, 0) + 1
    for (owner,) in connection.execute(waiting):
        used[owner] = used.get(owner, 0) + 1
    return used


def economy_backlog(connection, tenant):
    """Economy shots still waiting for a route."""
    from ..repository import jobs

    return connection.execute(
        select(func.count())
        .select_from(jobs)
        .where(
            jobs.c.tenant_id == tenant,
            jobs.c.status == "queued",
            jobs.c.pool != POOL,
            _economy(),
        )
    ).scalar_one()


def economy_in_flight(connection, tenant):
    """Whether an economy shot already holds the one hosted place."""
    from ..repository import jobs

    return (
        connection.execute(
            select(jobs.c.id)
            .where(
                jobs.c.tenant_id == tenant,
                jobs.c.pool == POOL,
                jobs.c.status.notin_(("succeeded", "failed", "cancelled")),
                _economy(),
            )
            .limit(1)
        ).first()
        is not None
    )


def economy_allowed(settings, compiled):
    """The economy tier is 768P only, and off until its routing switch names it."""
    return (
        getattr(settings, "everyframe_enabled", False)
        and "economy" in routing(settings.data_dir)[0]
        and compiled.get("request", {}).get("resolution") == "768P"
    )


BALANCE_READ_SECONDS = 4 * 3600
_balance = {"usd": None, "read_at": None}


def refresh_balance(settings, now, *, reader=None):
    """Read the vendor balance every 4 h (owner 2026-11-01), outside any transaction.

    An unknown balance leaves hosted open: a refusal costs nothing and the job
    falls back to our machines. The value is never logged.
    """
    if not getattr(settings, "everyframe_enabled", False):
        return None
    read_at = _balance["read_at"]
    if read_at is not None and now - read_at < BALANCE_READ_SECONDS:
        return _balance["usd"]
    _balance["read_at"] = now
    try:
        status, amount = (200, reader(settings)) if reader else _read_balance(settings)
    except Exception:
        print("hosted_balance_unavailable", flush=True)
        status, amount = None, None
    amount = amount if isinstance(amount, (int, float)) else None
    _balance["observation"] = BalanceObservation(now, status, amount)
    usd = amount if status == 200 else None
    _balance["usd"] = usd if isinstance(usd, (int, float)) else None
    if _balance["usd"] is not None and _balance["usd"] < HOSTED_BALANCE_WARN_USD:
        print("hosted_balance_low", flush=True)
    return _balance["usd"]


def balance_observation(settings):
    """Immutable copy of the existing cache's read, including a failed attempt."""
    sample = _balance.get("observation")
    if (
        getattr(settings, "everyframe_enabled", False)
        and sample is not None
        and sample.read_at == _balance["read_at"]
    ):
        return sample
    return None


def _read_balance(settings):
    import httpx

    from ..providers.everyframe import FILENAME, load_config

    config = load_config(Path(settings.data_dir) / "runtime-secrets" / FILENAME)
    with httpx.Client(
        timeout=5,
        trust_env=False,
        follow_redirects=False,
    ) as session:
        return read_everyframe_balance(session, config.api_key)


def balance_allows_hosted():
    return SCENARIOS["low_vendor_balance"].offload(_balance["usd"])


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
    tiers, window, first = _routing(settings.data_dir)
    if not tiers or not balance_allows_hosted():
        return queued
    live = live_configurations(connection, now)
    used = used_in_window(connection, settings.tenant_id, now)
    # A job that already went through the hosted chain stays on our machines.
    fresh = [item for item in queued if "hosted_tried" not in item[0]["execution_plan"]]
    economy = [item for item in fresh if tier_of(item[0]) == "economy"]
    clicks = [item for item in fresh if tier_of(item[0]) != "economy"]
    moved = set()

    def move(job):
        for model_id in chain(job["request"]):
            if MODELS[model_id].configuration_id in live:
                if rebind(service.repo, connection, job, model_id, now):
                    moved.add(job["id"])
                break

    for job, _ in hosted_offload(
        clicks, tier_of, tiers, used, now, per_window=window, first=first
    ):
        move(job)
    # Vivix renders one at a time across all vendor customers, so one economy
    # shot goes at a time; a backlog this size goes to a machine instead,
    # unless the hosted API comes first.
    if (
        economy
        and (
            first
            or SCENARIOS["small_economy_backlog"].offload(
                economy_backlog(connection, settings.tenant_id)
            )
        )
        and not economy_in_flight(connection, settings.tenant_id)
    ):
        used = used_in_window(connection, settings.tenant_id, now)
        for job, _ in hosted_offload(
            economy, tier_of, tiers, used, now, per_window=window, first=first
        )[:1]:
            move(job)
    return [item for item in queued if item[0]["id"] not in moved]


def hold_for_hosted(settings, connection, job, now):
    """A cold job waits for the hosted API instead of starting a machine for it.

    Without this a machine boots for a job that goes hosted a minute later, and
    is paid for while it idles (found on staging, 2026-11-01). Sustained hosted
    use stops the wait: a machine of ours is then cheaper, and once warm it takes
    new jobs before they reach their limit. After the grace, machines start as
    before.

    With the hosted API first the job waits for as long as a live hosted model
    can still take it, a full window included.
    """
    if not getattr(settings, "everyframe_enabled", False):
        return False
    tiers, window, first = _routing(settings.data_dir)
    models = chain(job["request"])
    if not tiers or not window or not balance_allows_hosted() or not models:
        return False
    tier = tier_of(job)
    if first:
        live = live_configurations(connection, now)
        return SCENARIOS["hosted_first"].hold(
            job,
            tier,
            tiers,
            any(MODELS[model_id].configuration_id in live for model_id in models),
        )
    if tier == "economy":
        # Auto-queue never hurries: no machine until the backlog pays for one.
        return (
            tier in tiers
            and "hosted_tried" not in job["execution_plan"]
            and SCENARIOS["large_economy_backlog"].hold(
                economy_backlog(connection, settings.tenant_id)
            )
        )
    used = used_in_window(connection, settings.tenant_id, now, clicks_only=True)
    if not SCENARIOS["sustained_clicks"].hold(used):
        return False
    return hosted_has_first_claim(job, tier, tiers, now)
