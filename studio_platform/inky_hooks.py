"""Collect committed edit/preflight triggers within one HTTP request.

Use Starlette's existing response background tasks, not a second scheduler.
The durable evaluation/budget claims live in InkyStore/InkyBudget. A crash may
omit optional advice but must not replay an unknown paid operation.
"""

from contextvars import ContextVar
import logging

from starlette.concurrency import run_in_threadpool

PENDING = ContextVar("inky_pending", default=None)
LOG = logging.getLogger(__name__)


def queue_refresh(principal, project_id):
    pending = PENDING.get()
    if pending is not None and not principal.support_viewer:
        pending[(principal.owner, project_id)] = (principal, project_id)


def handler(original, app):
    async def wrapped(scope, receive, send):
        worker = getattr(app.state, "inky_worker", None)
        if worker is None or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            return await original(scope, receive, send)
        pending = {}
        status = None

        async def observed(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        token = PENDING.set(pending)
        try:
            await original(scope, receive, observed)
        finally:
            PENDING.reset(token)
        if status is not None and 200 <= status < 300:
            # Same executor/lifecycle as Starlette BackgroundTasks, after the
            # original response (including its existing background work).
            for principal, project_id in pending.values():
                await run_in_threadpool(run, worker, principal, project_id)

    return wrapped


def run(worker, principal, project_id):
    try:
        worker.refresh(principal, project_id)
    except Exception:
        # A committed user save remains successful; no sensitive exception text.
        LOG.warning("inky_background_unavailable")


class AdviceBoundary:
    def __init__(self, app, owner):
        self.app = handler(app, owner)

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


def install(app):
    # Pure ASGI wrapping also covers composed APIRouters. Keep their original
    # response serialization, projections, streams and background tasks intact.
    app.add_middleware(AdviceBoundary, owner=app)
