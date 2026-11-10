"""Original slot contexts and finite turns, independent of the DBOS SDK."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import threading

from .repository import Conflict, Scope
from .providers.registry import CPU_RENDER, LOCAL_BACKENDS

POLL_SECONDS = 10
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class Slot:
    spec: object
    runner: object
    context: object
    lock: object


class SlotContexts:
    """One authoritative ordered registry and original supervisor fence."""

    def __init__(self, repo, settings, work_dir, *, runner_context=None):
        self.repo, self.settings = repo, settings
        self.work_dir = Path(work_dir)
        self._slots = {}
        self._lock = threading.RLock()
        self._fence = None
        self._runner_context = runner_context
        self._closing = False
        # Slots the controller asked to stop. Queued-job turns skip them, so a
        # busy queue cannot hold the slot lock forever and keep renewing the
        # registration of a removed host (#1140).
        self._retiring = set()

    def __enter__(self):
        from .slot_lock import slot_lock

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._fence = slot_lock(self.work_dir, "dstack-cpu-supervisor")
        if not self._fence.__enter__():
            self._fence.__exit__(None, None, None)
            self._fence = None
            raise ValueError("dstack_cpu_supervisor_already_active")
        return self

    def open(self, intent_id, slot):
        from .fleet import FleetConfig, slot_runner
        from .postgres_dispatch import record_consumer

        with self._lock:
            if self._closing or self._fence is None:
                raise Conflict("dbos_executor_stopping")
            current = self._slots.get(intent_id)
            if current is not None and current.spec != slot.spec:
                raise Conflict("dbos_native_slot_identity_changed")
            # A fresh native observation re-opens the slot for new turns.
            self._retiring.discard(intent_id)
            if current is None:
                config = FleetConfig(
                    self.work_dir, (slot,), enabled=True, max_children=1
                )
                context = (self._runner_context or slot_runner)(
                    config, slot.spec.worker_id, self.settings, repository=self.repo
                )
                runner = context.__enter__()
                if runner is None:
                    context.__exit__(None, None, None)
                    raise Conflict("dbos_slot_disabled")
                current = Slot(slot.spec, runner, context, threading.Lock())
                self._slots[intent_id] = current
            # The existing controller calls ensure after its fresh native
            # observation. Idle DBOS slots have no WorkerRunner turn to renew
            # registration. Keep only an already-live lease/fence; expiry or
            # UNKNOWN still needs explicit native idle proof in the controller.
            if slot.spec.backend not in LOCAL_BACKENDS:
                worker = current.runner.control.get(slot.spec.worker_id)
                if worker["expires_at"] > self.repo.clock() and worker["state"] not in {
                    "unknown",
                    "retired",
                }:
                    current.runner.control.heartbeat(
                        slot.spec.worker_id, worker["fence"]
                    )
            # Original incarnation/fence checked by this existing observation.
            if slot.spec.dispatch_backend == "hatchet-v1":
                current.runner._consumer_observation = record_consumer(
                    self.repo,
                    current.runner.control,
                    current.runner.backend,
                    slot.spec.worker_id,
                )

    def stop(self, intent_id):
        with self._lock:
            item = self._slots.get(intent_id)
            if item is None:
                self._retiring.discard(intent_id)
                return
            # No new turn may take this slot; the in-flight one finishes and
            # the controller's next tick completes the stop.
            self._retiring.add(intent_id)
            if not item.lock.acquire(blocking=False):
                raise Conflict("dbos_domain_turn_still_active")
            try:
                from .postgres_dispatch import withdraw_consumer

                if item.spec.dispatch_backend == "hatchet-v1":
                    withdraw_consumer(
                        self.repo,
                        item.runner.control,
                        item.runner.backend,
                        item.spec.worker_id,
                        getattr(item.runner, "_consumer_observation", None),
                    )
                item.context.__exit__(None, None, None)
                del self._slots[intent_id]
                self._retiring.discard(intent_id)
            finally:
                item.lock.release()

    def close(self):
        self._closing = True
        for key in list(self._slots):
            self.stop(key)
        if self._fence is not None:
            self._fence.__exit__(None, None, None)
            self._fence = None

    def __exit__(self, *_):
        self.close()


def selected_job_turn(contexts, tenant, owner, project, job_id, *, run_selected):
    scope = Scope(tenant, owner, project)
    job = contexts.repo.get_job(scope, job_id)
    if job["status"] in TERMINAL or job["status"] == "recovery_hold":
        return {"state": job["status"], "done": True}
    if contexts._closing:
        return {
            "state": "executor_stopping",
            "done": False,
            "wait_seconds": POLL_SECONDS,
        }
    with contexts._lock:
        candidates = [
            slot
            for key, slot in contexts._slots.items()
            if key not in contexts._retiring
        ]
    # Existing attempts stay with their original worker. New work tries
    # dedicated capacity first; its original claim gates decide eligibility.
    candidates.sort(
        key=lambda slot: (
            slot.spec.worker_id != job['lease_worker_id']
            if job.get('lease_worker_id')
            else slot.spec.provider == 'modal'
        )
    )
    claimed, pooled, refused = False, [], {}
    for slot in candidates:
        if slot.spec.pool != job["pool"]:
            continue
        pooled.append(slot)
        reason = _refusal(contexts, slot, job)
        if reason is not None:
            refused[slot.spec.worker_id] = reason
            continue
        if not slot.lock.acquire(blocking=False):
            continue
        try:
            if run_selected(slot, job_id):
                claimed = True
                break
        finally:
            slot.lock.release()
    fresh = contexts.repo.get_job(scope, job_id)
    if not claimed and fresh["status"] == "queued":
        _count_refusal(contexts, pooled, fresh, refused)
    now = contexts.repo.clock()
    deadline = max(fresh["not_before"], fresh["lease_expires_at"] or now)
    return {
        "state": fresh["status"],
        "done": fresh["status"] in TERMINAL or fresh["status"] == "recovery_hold",
        "wait_seconds": max(POLL_SECONDS, min(900, deadline - now)),
    }


def _refusal(contexts, slot, job):
    """Why a turn on this slot cannot claim this queued job (#1199), else None.

    Read-only, before the slot lock: a job that cannot claim must not hold the
    lock the claimable job needs. None, or any error, runs the turn as before.
    """
    try:
        if (
            job["status"] != "queued"
            or job.get("lease_worker_id")
            or slot.spec.backend in LOCAL_BACKENDS
        ):
            return None
        with contexts.repo.engine.connect() as connection:
            reason = slot.runner.control.queued_turn_refusal(
                connection, slot.spec.worker_id, job
            )
    except Exception:
        return None
    return reason if isinstance(reason, str) else None


def _count_refusal(contexts, slots, job, refused=None):
    """Why no slot took a queued job this turn (#1140 item 4); never raises."""
    from .capacity_reasons import count

    if not slots:
        return count("claim", "no_worker_for_pool")
    try:
        for slot in slots:
            known = (refused or {}).get(slot.spec.worker_id)
            if known is not None:
                count("claim", known, slot.spec.worker_id)
                continue
            control = getattr(slot.runner, "control", None)
            if control is None:
                continue
            with contexts.repo.engine.connect() as connection:
                worker = control._worker(connection, slot.spec.worker_id)
                reason = control.claim_reason(connection, worker, job)
            # Eligible but another job is first in the fair claim order.
            count("claim", reason or "not_first_in_order", slot.spec.worker_id)
    except Exception as error:
        count("claim", "check_failed_" + type(error).__name__)


def run_selected_slot(slot, job_id, *, local_owner=None):
    """Called under the original slot lock; return only the old loop-break choice."""
    cpu = slot.spec.backend == CPU_RENDER.id
    acquired_cpu = False
    try:
        if cpu:
            acquired_cpu = local_owner.operation_lock.acquire(blocking=False)
            if not acquired_cpu:
                return False
        with (
            local_owner.cpu_fence() if cpu else nullcontext((True, None)) as (
                physical,
                descriptor,
            )
        ):
            if not physical:
                return False
            slot.runner.selected_job_id = job_id
            if cpu:
                slot.runner.backend._operation_fence_fd = descriptor
            try:
                result = slot.runner.run_once(slot.spec.worker_id, slot.spec.pool)
            finally:
                if cpu:
                    slot.runner.backend._operation_fence_fd = None
        if local_owner is not None:
            local_owner.heartbeat_slot(slot)
        if result.get("job_id"):
            if result["job_id"] != job_id:
                raise Conflict("dbos_selected_job_mismatch")
            return True
        return False
    finally:
        slot.runner.selected_job_id = None
        if acquired_cpu:
            local_owner.operation_lock.release()
