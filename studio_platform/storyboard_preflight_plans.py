"""Plan the items of one storyboard preflight (#1102, #1130).

The project is loaded and authorized once per preflight, capabilities and the
storyboard defaults are read once, and the shots are planned with bounded
concurrency. Every shot still re-checks the project version, its source
snapshot and the defaults version right before its plan is stored.

Each plan keeps its deterministic plan ID and its own transaction, so a crash
or a concurrent replay recovers the same plans. Only this request's thread
writes the preflight document, one short transaction per finished plan, which
keeps the document's version sequence unchanged. One timing line per preflight
carries phase durations only, never story text or identifiers.
"""

import contextvars
import copy
import json
import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager

from .content_lifecycle import owner_reference_gate
from .generation_admission import PreflightReads
from .repository import Conflict, NotFound

LOG = logging.getLogger(__name__)
# Shots plan in parallel: each one mostly waits on the prompt rewrite and the
# database. Four keeps one preflight well inside the default connection pool.
MAX_WORKERS = 4
# Above this the line is a warning, so it shows with the default log level.
SLOW_MS = 1000
CLOSING = {
    "storyboard_defaults_changed",
    "document_version_conflict",
    "shot_version_conflict",
}


class PreflightTimer:
    PHASES = ("load", "capabilities", "defaults", "materialize", "plan", "store")

    def __init__(self):
        self.started = time.perf_counter()
        self.ms = dict.fromkeys(self.PHASES, 0.0)
        self.items = self.planned = self.workers = 0
        self.shot_max_ms = 0.0

    @contextmanager
    def phase(self, name):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] += (time.perf_counter() - start) * 1000

    def run(self, call):
        status = "error"
        try:
            value = call()
            status = value.get("status", "unknown")
            return value
        except Exception as error:
            status = "error:" + type(error).__name__
            raise
        finally:
            self.log(status)

    def log(self, status):
        total = (time.perf_counter() - self.started) * 1000
        LOG.log(
            logging.WARNING if total >= SLOW_MS else logging.INFO,
            "storyboard_preflight_timing status=%s items=%d planned=%d workers=%d "
            "total_ms=%d %s shot_max_ms=%d",
            status,
            self.items,
            self.planned,
            self.workers,
            total,
            " ".join(f"{name}_ms={int(value)}" for name, value in self.ms.items()),
            self.shot_max_ms,
        )


def _workers(service, pending):
    if len(pending) < 2 or service.repo._is_memory:
        # One shared in-memory SQLite connection cannot serve parallel threads.
        return 1
    return min(MAX_WORKERS, len(pending))


def _plan_one(service, principal, scope, value, item, reads):
    """(plan row, public plan, elapsed ms or None when an earlier plan is reused)."""
    start = time.perf_counter()
    try:
        plan = service.repo.get_plan(scope, item["plan_id"])
        return plan, service.admission.planning.retained_plan(plan, principal), None
    except NotFound:
        pass
    if reads.setting["version"] != value["settings_version"]:
        raise Conflict("storyboard_defaults_changed")
    result = service.admission.preflight(
        principal,
        reads.record["payload"],
        item["shot_id"],
        expected_version=value["project_version"],
        seed_override=item["seed"],
        plan_id=item["plan_id"],
        reads=reads,
    )
    plan = service.repo.get_plan(scope, item["plan_id"])
    return plan, result, (time.perf_counter() - start) * 1000


def _finished(service, principal, scope, value, pending, reads, workers):
    """Yield (item, outcome, error) as plans finish, in item order per batch."""
    if workers == 1:
        for item in pending:
            try:
                outcome = _plan_one(service, principal, scope, value, item, reads)
            except Exception as error:
                yield item, None, error
            else:
                yield item, outcome, None
        return
    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="storyboard-preflight"
    )
    try:
        # Each task runs in its own copy of the request context (Inky refresh
        # queue, tracing), as the sequential loop did.
        futures = {
            executor.submit(
                contextvars.copy_context().run,
                _plan_one,
                service,
                principal,
                scope,
                value,
                item,
                reads,
            ): index
            for index, item in enumerate(pending)
        }
        remaining = set(futures)
        while remaining:
            done, remaining = wait(remaining, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=futures.get):
                error = future.exception()
                item = pending[futures[future]]
                yield item, None if error else future.result(), error
    finally:
        # Plans already running finish under their own IDs and are reused by a
        # replay; queued ones are dropped once the preflight is closed.
        executor.shutdown(wait=True, cancel_futures=True)


def _closes(error):
    # Only deterministic validation/source conflicts close an unpaid preflight.
    # Storage/network/runtime unknowns retain their original operation for
    # explicit reconciliation.
    return isinstance(error, ValueError) or (
        isinstance(error, Conflict) and str(error) in CLOSING
    )


def _block(service, principal, project_id, scope, ident, error):
    with service.repo.transaction() as conn:
        owner_reference_gate(conn, service.tenant, principal.owner)
        latest = service._document(conn, scope, "storyboard_preflight", ident)
        payload = copy.deepcopy(latest["payload"])
        payload["blocked"] = (
            "source_changed"
            if isinstance(error, Conflict)
            else "invalid_generation_input"
        )
        service._put(
            conn,
            scope,
            "storyboard_preflight",
            ident,
            payload,
            latest["version"] + 1,
            True,
        )
    return service.get_preflight(principal, project_id, ident)


def _store(service, principal, scope, ident, item, plan, result):
    """Project one finished plan; the blocked payload when the document is full."""
    runtime = (
        plan["execution_plan"].get("expected_runtime_s")
        if plan["execution_plan"].get("quote_known") is True
        else None
    )
    with service.repo.transaction() as conn:
        owner_reference_gate(conn, service.tenant, principal.owner)
        latest = service._document(conn, scope, "storyboard_preflight", ident)
        payload = copy.deepcopy(latest["payload"])
        target = next(v for v in payload["items"] if v["plan_id"] == item["plan_id"])
        target.update(plan=result, machine_seconds=runtime)
        if (
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            > service.settings.max_project_bytes
        ):
            payload = copy.deepcopy(latest["payload"])
            payload["blocked"] = "preflight_document_size_exceeded"
        service._put(
            conn,
            scope,
            "storyboard_preflight",
            ident,
            payload,
            latest["version"] + 1,
            True,
        )
    return payload if payload.get("blocked") else None


def shared_reads(service, principal, record, caps, setting, timer):
    """Capabilities and defaults once per preflight."""
    from .storyboard_versions import preferences

    if caps is None:
        with timer.phase("capabilities"):
            caps = service.admission.capabilities_provider()
    if setting is None:
        with timer.phase("defaults"):
            setting = preferences(service.repo, service.tenant, principal.owner)
    return PreflightReads(record=dict(record), capabilities=caps, setting=setting)


def plan_items(service, principal, project_id, scope, ident, value, shared, timer):
    """Plan every pending item; the public preflight. ``shared`` is
    (record, capabilities or None, defaults or None) from this request."""
    pending = [item for item in value["items"] if not item.get("plan")]
    timer.items = len(value["items"])
    if not pending:
        return service.get_preflight(principal, project_id, ident)
    reads = shared_reads(service, principal, *shared, timer)
    timer.workers = _workers(service, pending)
    plan_start = time.perf_counter()
    finished = _finished(
        service, principal, scope, value, pending, reads, timer.workers
    )
    try:
        for item, outcome, error in finished:
            if error is not None:
                if not _closes(error):
                    raise error
                return _block(service, principal, project_id, scope, ident, error)
            plan, result, elapsed = outcome
            if elapsed is not None:
                timer.planned += 1
                timer.shot_max_ms = max(timer.shot_max_ms, elapsed)
            with timer.phase("store"):
                blocked = _store(service, principal, scope, ident, item, plan, result)
            if blocked:
                return service._public_preflight(blocked, principal)
    finally:
        finished.close()
        planning = (time.perf_counter() - plan_start) * 1000
        timer.ms["plan"] += planning - timer.ms["store"]
    return service.get_preflight(principal, project_id, ident)
