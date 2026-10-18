"""Direct CPU slot consumers; PostgreSQL notifications are only wake hints.

The historical hatchet-v1 route remains an immutable execution-plan identity.
No broker delivery, notification payload or timeout authorizes inference.
"""

import copy
import signal
import threading
from pathlib import Path

from sqlalchemy import case, func, select, update

from .demand_wakeup import DemandWakeup
from .dispatch_telemetry import _host_telemetry_context
from .drain_safe_runner import collection_slot
from .operator_capacity import operator_nodes
from .repository import jobs
from .worker import WorkerRunner

ROUTE = "hatchet-v1"
IDLE_SECONDS = 30


def record_consumer(repo, control, backend, worker_id):
    """Called by the consumer while holding its original physical slot lock."""
    if backend.kind != "wangp-worker":
        return  # Local fake adapters have no managed node.
    with repo.transaction() as connection:
        repo._lock_capacity(connection)
        worker = control._worker(connection, worker_id, lock=True)
        node = (
            connection.execute(
                select(operator_nodes).where(
                    operator_nodes.c.intent_id == backend.slot_key
                )
            )
            .mappings()
            .one_or_none()
        )
        now = repo.clock()
        if node is None:
            raise ValueError("queue_consumer_node_missing")
        payload = copy.deepcopy(node["payload"])
        binding, spec = payload.get("dstack", {}), worker["spec"]
        incarnation = getattr(backend, "expected_incarnation", None)
        if (
            payload.get("capacity_backend") != "dstack-v1"
            or not incarnation
            or binding.get("runtime_incarnation") != incarnation
            or spec.get("dispatch_backend") != ROUTE
            or binding.get("provider_instance_id") != worker["instance_id"]
            or binding.get("manifest_digest") != spec.get("engine_manifest_digest")
            or binding.get("configuration_id") != spec.get("configuration_id")
            or binding.get("model_id") != spec.get("model_id")
        ):
            raise ValueError("queue_consumer_binding_changed")
        binding["consumer_observation"] = {
            "ready": worker["state"] not in {"unknown", "retired"}
            and worker["expires_at"] > now
            and not worker["drain_requested"],
            "worker_id": worker_id,
            "worker_fence": worker["fence"],
            "instance_id": worker["instance_id"],
            "runtime_incarnation": incarnation,
            "observed_at": now,
        }
        # A liveness write is not business demand; do not emit self-wake events.
        connection.execute(
            update(operator_nodes)
            .where(operator_nodes.c.intent_id == node["intent_id"])
            .values(payload=payload)
        )
        return copy.deepcopy(binding["consumer_observation"])


def withdraw_consumer(repo, control, backend, worker_id, observation):
    """Withdraw only this process's last exact liveness proof, never worker drain."""
    if backend.kind != "wangp-worker" or observation is None:
        return
    with repo.transaction() as connection:
        repo._lock_capacity(connection)
        worker = control._worker(connection, worker_id, lock=True)
        node = repo._locked(
            connection,
            select(operator_nodes).where(
                operator_nodes.c.intent_id == backend.slot_key
            ),
        )
        if node is None:
            return
        payload = copy.deepcopy(node["payload"])
        binding = payload.get("dstack", {})
        if (
            binding.get("consumer_observation") != observation
            or binding.get("runtime_incarnation") != observation["runtime_incarnation"]
            or worker["fence"] != observation["worker_fence"]
            or worker["instance_id"] != observation["instance_id"]
        ):
            return
        binding["consumer_observation"]["ready"] = False
        connection.execute(
            update(operator_nodes)
            .where(operator_nodes.c.intent_id == node["intent_id"])
            .values(payload=payload)
        )


class PostgresSlotRunner(WorkerRunner):
    def __init__(
        self, *args, collection_lock_dir, wakeup_factory=DemandWakeup, **kwargs
    ):
        super().__init__(*args, **kwargs)
        if self.control is None:
            raise ValueError("queue_requires_durable_worker_control")
        self.collection_lock_dir = Path(collection_lock_dir)
        if not self.collection_lock_dir.is_absolute():
            raise ValueError("absolute_shared_collection_lock_required")
        self.wakeup_factory = wakeup_factory
        self.worker_id = None
        self._consumer_observation = None

    def _run_once(self, worker_id, pool):
        # WorkerRunner invokes this under the physical lock, after registration
        # renewal. Neither the parent supervisor nor a notification can attest it.
        self.worker_id = worker_id
        self._consumer_observation = record_consumer(
            self.repo, self.control, self.backend, worker_id
        )
        return super()._run_once(worker_id, pool)

    def _claim(self, worker_id, pool, *, purpose):
        claim = super()._claim(worker_id, pool, purpose=purpose)
        if claim:
            # Claim advances the worker fence. Publish that exact new fence
            # before the original late submission guard checks eligibility.
            self._consumer_observation = record_consumer(
                self.repo, self.control, self.backend, worker_id
            )
        return claim

    def _submission_allowed(self, job):
        self._consumer_observation = record_consumer(
            self.repo, self.control, self.backend, self.worker_id
        )
        return super()._submission_allowed(job)

    def _telemetry_context(self, job, lease):
        result = super()._telemetry_context(job, lease)
        worker = self.control.get(lease.worker_id)
        result.update(
            provider="local"
            if worker["spec"]["provider"] == "mock"
            else worker["spec"]["provider"],
            node_id=self.backend.slot_key,
        )
        result.update(
            _host_telemetry_context(
                self.repo,
                node_id=self.backend.slot_key,
                worker_id=lease.worker_id,
                spec=worker["spec"],
                job_id=job["id"],
            )
        )
        return result

    def _collect(self, job, lease, tag, task_id, heartbeat):
        with collection_slot(self.collection_lock_dir, heartbeat):
            return super()._collect(job, lease, tag, task_id, heartbeat)

    def wait_seconds(self, worker_id, pool):
        """Timers also wake: retry eligibility, lease recovery and registration."""
        now = self.repo.clock()
        worker = self.control.get(worker_id)
        delay = min(IDLE_SECONDS, max(1, (worker["expires_at"] - now) / 2))
        with self.repo.engine.connect() as connection:
            deadlines = connection.execute(
                select(
                    func.min(case((jobs.c.not_before > now, jobs.c.not_before))),
                    func.min(
                        case((jobs.c.lease_expires_at > now, jobs.c.lease_expires_at))
                    ),
                ).where(
                    jobs.c.pool == pool,
                    jobs.c.execution_plan["dispatch_backend"].as_string() == ROUTE,
                    jobs.c.status.in_(
                        (
                            "queued",
                            "claimed",
                            "submitting",
                            "running",
                            "submission_unknown",
                            "collecting",
                            "cancel_requested",
                        )
                    ),
                )
            ).one()
            for deadline in deadlines:
                if deadline is not None:
                    delay = min(delay, deadline - now)
        return max(0.05, delay)

    def run_forever(self, worker_id, pool, *, poll_interval_s=1):
        wakeup = self.wakeup_factory(self.repo, self._drain)
        prior = {}
        process_shutdown = False
        completed = False

        def shutdown(*_):
            nonlocal process_shutdown
            process_shutdown = True
            self.drain()
            wakeup.interrupt()

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                prior[signum] = signal.getsignal(signum)
                signal.signal(signum, shutdown)
        try:
            wakeup.subscribe()  # Scan after subscription, including after restart.
            while not self._drain.is_set():
                self.run_once(worker_id, pool)
                if not self._drain.is_set():
                    wakeup.wait(self.wait_seconds(worker_id, pool))
            completed = True
            return {"state": "draining"}
        finally:
            try:
                wakeup.close()
                # A CPU service restart is not an operator request to retire
                # capacity. Explicit flags/errors retain durable draining.
                explicit_stop = (
                    self.stop_requested is not None and self.stop_requested() is True
                )
                if not (completed and process_shutdown) or explicit_stop:
                    self.control.drain(worker_id)
                withdraw_consumer(
                    self.repo,
                    self.control,
                    self.backend,
                    worker_id,
                    self._consumer_observation,
                )
            finally:
                for signum, handler in prior.items():
                    signal.signal(signum, handler)
                if hasattr(self.backend, "close"):
                    self.backend.close()
