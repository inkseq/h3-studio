"""Hand a newly queued shot to the hosted API without waiting for a controller pass.

The hosted offload ran only near the end of each controller pass
(dstack_demand_start._claim), so a new shot stayed in its machine pool, where
no worker can claim it, until the pass in progress finished: 18-35 s on
staging, and over 100 s behind one 127 s pass (2026-11-18, #1252).

This loop looks every few seconds for queued jobs it has not considered yet
and runs the same offload for them under the same capacity lock. It adds no
rule: a job the offload leaves alone is not looked at again here, and
time-based offload (a job that has waited too long) stays with the pass.
"""

import logging
import threading

from sqlalchemy import select

from .providers.hosted_models import POOL
from .repository import jobs

POLL_SECONDS = 3
_LOG = logging.getLogger(__name__)


def new_arrivals(service, seen):
    """Queued, claimable-now jobs outside the hosted pool, not considered yet.

    Also forgets jobs that are no longer waiting, so seen stays small.
    """
    now = service.repo.clock()
    with service.repo.engine.connect() as connection:
        current = {
            (row.id, row.fence)
            for row in connection.execute(
                select(jobs.c.id, jobs.c.fence).where(
                    jobs.c.tenant_id == service.settings.tenant_id,
                    jobs.c.status == "queued",
                    jobs.c.lease_worker_id.is_(None),
                    jobs.c.not_before <= now,
                    jobs.c.pool != POOL,
                )
            )
        }
    seen &= current
    return current - seen


def offload_once(service, *, eligible=None):
    """One capacity-locked offload, exactly as the controller pass does it."""
    from .scheduling.rebind import offload

    if eligible is None:
        from .dstack_demand import eligible_jobs as eligible
    now = service.repo.clock()
    with service.repo.transaction() as connection:
        service.repo._lock_capacity(connection)
        queued = eligible(service, connection, now)
        if not queued:
            return 0
        return len(queued) - len(offload(service, connection, queued, now))


def run(service, stop, *, poll=POLL_SECONDS, eligible=None):
    seen = set()
    while not stop.wait(poll):
        try:
            if not getattr(service.settings, "everyframe_enabled", False):
                continue
            fresh = new_arrivals(service, seen)
            if not fresh:
                continue
            try:
                moved = offload_once(service, eligible=eligible)
            finally:
                # Considered once, moved or not; the pass remains the retry.
                seen |= fresh
            if moved:
                _LOG.warning("hosted_fast_offload moved=%d", moved)
        except Exception as error:
            _LOG.warning(
                "hosted_fast_offload_failed error_type=%s", type(error).__name__
            )


def start_hosted_fast_offload(service, stop):
    thread = threading.Thread(
        target=run, args=(service, stop), name="hosted-fast-offload", daemon=True
    )
    thread.start()
    return thread
