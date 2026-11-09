"""Bounded DBOS slot runner and original native-consumer liveness hooks.

The historical hatchet-v1 route remains an immutable execution-plan identity.
No broker delivery, notification payload or timeout authorizes inference.
"""

import copy
from pathlib import Path

from sqlalchemy import select, update

from .dispatch_telemetry import _host_telemetry_context
from .drain_safe_runner import collection_slot
from .operator_capacity import operator_nodes
from .worker import WorkerRunner

ROUTE = "hatchet-v1"


def _live_readiness(repo, backend):
    """This consumer's own native readiness read, before the capacity lock.

    Returns ``(read_started_at, HostReadiness)`` or None. The read start
    bounds its age; a failed or odd read proves nothing (#1148 item 2).
    """
    from .inference.wangp_contract import HostReadiness

    read = getattr(backend, "live_readiness", None)
    if not callable(read):
        return None
    started = repo.clock()
    try:
        value = read()
    except Exception:
        return None
    return (started, value) if isinstance(value, HostReadiness) else None


def _runtime_ready_at(live, node, binding, incarnation, now):
    """Start of a live read proving this exact bound runtime, else None."""
    if live is None:
        return None
    started, value = live
    if (
        value.slot_key == node["intent_id"]
        and value.manifest_digest == binding.get("manifest_digest")
        and value.incarnation == incarnation
        and type(value.idle) is bool
        and type(started) in (int, float)
        and 0 <= now - started
    ):
        return started
    return None


def record_consumer(repo, control, backend, worker_id, *, probe=False):
    """Called by the consumer while holding its original physical slot lock.

    With ``probe`` the consumer first reads its runtime's readiness itself,
    outside the capacity lock, and records the read under the lock as
    ``runtime_ready_at``. Admission accepts that as a fresh readiness proof
    only next to a recent controller observation of the same running run, so
    a claim and its submit check no longer wait for the next controller tick
    (#1148 item 2).
    """
    if backend.kind != "wangp-worker":
        return  # Local fake adapters have no managed node.
    live = _live_readiness(repo, backend) if probe else None
    with repo.transaction() as connection:
        repo._lock_capacity(connection)
        worker = control._worker(connection, worker_id, lock=True)
        if worker['provider'] == 'modal':
            from .modal_dispatch import record

            return record(repo, connection, worker, backend)
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
            "runtime_ready_at": _runtime_ready_at(
                live, node, binding, incarnation, now
            ),
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
        if worker['provider'] == 'modal':
            from .modal_dispatch import withdraw

            return withdraw(repo, connection, worker, backend, observation)
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
    def __init__(self, *args, collection_lock_dir, **kwargs):
        super().__init__(*args, **kwargs)
        if self.control is None:
            raise ValueError("queue_requires_durable_worker_control")
        self.collection_lock_dir = Path(collection_lock_dir)
        if not self.collection_lock_dir.is_absolute():
            raise ValueError("absolute_shared_collection_lock_required")
        self.worker_id = None
        self._consumer_observation = None

    def _run_once(self, worker_id, pool):
        # WorkerRunner invokes this under the physical lock, after registration
        # renewal. Neither the parent supervisor nor a notification can attest it.
        self.worker_id = worker_id
        self._consumer_observation = record_consumer(
            self.repo, self.control, self.backend, worker_id, probe=True
        )
        return super()._run_once(worker_id, pool)

    def _claim(self, worker_id, pool, *, purpose):
        claim = super()._claim(worker_id, pool, purpose=purpose)
        if claim:
            # Claim advances the worker fence. Publish that exact new fence
            # before the original late submission guard checks eligibility.
            self._consumer_observation = record_consumer(
                self.repo, self.control, self.backend, worker_id, probe=True
            )
        return claim

    def _submission_allowed(self, job):
        self._consumer_observation = record_consumer(
            self.repo, self.control, self.backend, self.worker_id, probe=True
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

    def run_forever(self, *args, **kwargs):
        raise ValueError("dbos_required_for_continuous_execution")
