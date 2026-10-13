"""CPU reconciliation of owned dstack runs; never retries a rental or GPU POST.

The original database owns leases, budgets, jobs and drain decisions. dstack
owns capacity. Native readiness owns the private model endpoint. Hatchet carries
identity-only wakeups. All dependencies are injected; imports have no I/O.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import argparse
import signal
import threading

from sqlalchemy import select

from .control import WorkerControl, worker_spec_payload
from .dstack_capacity import idle_action
from .dstack_operator import BACKEND_MARKER, _request, _cold_unadmitted_runtime, _window_open
from .operator_capacity import operator_nodes
from .repository import BudgetExceeded, attempts, instance_intents, jobs, registered_workers, scaler_actions
from .worker import _slot_lock

TERMINAL = {"succeeded", "failed", "cancelled"}


class DstackController:
    def __init__(self, service, runtime, *, worker_process=None, broker_readiness=None):
        if service.capacity is None:
            raise ValueError("dstack_capacity_disabled")
        self.service, self.repo, self.runtime = service, service.repo, runtime
        self.capacity = service.capacity
        self.capacity.readiness = runtime.readiness
        self.control = WorkerControl(self.repo, capacity_usage=service.registration_capacity_usage)
        self.worker_process = worker_process
        self.broker_readiness = broker_readiness

    def _nodes(self):
        with self.repo.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(operator_nodes)).mappings()
                if row["payload"].get("capacity_backend") == BACKEND_MARKER
                and row["payload"].get("tenant_id") == self.service.settings.tenant_id]

    def _obligations(self, intent):
        from .worker_admission import matching_dstack_queued_jobs
        with self.repo.engine.connect() as connection:
            workers = list(connection.execute(select(registered_workers).where(
                registered_workers.c.provider == intent["provider"],
                registered_workers.c.instance_id == intent["provider_instance_id"])).mappings())
            ids = [worker["id"] for worker in workers]
            linked = list(connection.execute(select(jobs).where(jobs.c.lease_worker_id.in_(ids))).mappings()) if ids else []
            related = list(connection.execute(select(attempts).where(attempts.c.worker_id.in_(ids))).mappings()) if ids else []
            by_id = {job["id"]: job for job in linked}
            for attempt in related:
                if attempt["job_id"] not in by_id:
                    by_id[attempt["job_id"]] = self.repo._job(connection, attempt["job_id"])
            node = connection.execute(select(operator_nodes).where(
                operator_nodes.c.intent_id == intent["id"])).mappings().one()
            for job in matching_dstack_queued_jobs(connection,intent,node):
                by_id.setdefault(job["id"],job)
        active = sum(job["status"] not in TERMINAL for job in by_id.values())
        unsafe = sum(attempt["submission_started_at"] is not None and attempt["upstream_stopped"] != 1 for attempt in related)
        last = max([intent["created_at"], *[job["updated_at"] for job in by_id.values()]])
        return workers, active, unsafe, last

    def _request_stop(self, node, *, reason_code="dstack_idle_expired"):
        # Idle expiry is an internal original-ledger command, never an HTTP
        # principal impersonation. Preserve the original deadline/reservation.
        from sqlalchemy import update
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            current = self.repo._locked(connection, select(operator_nodes).where(operator_nodes.c.intent_id == node["intent_id"]))
            if current["desired_state"] == "running":
                if reason_code == "dstack_idle_expired":
                    from .worker_admission import matching_dstack_queued_jobs
                    intent = self.repo._locked(connection,select(instance_intents).where(
                        instance_intents.c.id == node["intent_id"]))
                    if matching_dstack_queued_jobs(connection,intent,current):
                        return False
                payload = dict(current["payload"])
                payload["stop_reason_code"] = reason_code
                connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == node["intent_id"])
                    .values(desired_state="stopped", runtime_state="draining", payload=payload, updated_at=self.repo.clock()))
                self.repo._emit(connection, "dstack.idle_drain_requested", node["intent_id"],
                    {"intent_id": node["intent_id"], "reason_code": reason_code})
                return True
            return current["desired_state"] == "stopped"

    def _tick_node(self, node):
        intent_id = node["intent_id"]
        binding = self.capacity.store.load(intent_id)
        funded = True
        if node["payload"].get("lifecycle", {}).get("version") == 2 and node["desired_state"] == "running":
            with self.repo.engine.connect() as connection:
                prior = connection.execute(select(instance_intents).where(
                    instance_intents.c.id == intent_id)).mappings().one()
            if binding.get("apply_started") and prior["state"] in {"draining", "destroying", "destroyed"}:
                # Upstream-only terminal evidence can end a stale projection;
                # it cannot bootstrap, reopen admission, or fund stopped work.
                # Any nonterminal/unknown read still meets the original funding
                # refusal below before native activity can occur.
                terminal = self.capacity.observe_terminal(intent_id)
                if terminal is not None:
                    return {"node_id": intent_id, "state": terminal["state"]}
            try:
                # Funding precedes native bootstrap as well as admission. Time
                # spent downloading/holding/uncertain still consumes runway.
                with self.repo.transaction() as connection:
                    self.repo._lock_capacity(connection)
                    current = self.repo._locked(connection, select(operator_nodes).where(operator_nodes.c.intent_id == intent_id))
                    intent = self.repo._locked(connection, select(instance_intents).where(instance_intents.c.id == intent_id))
                    if current["desired_state"] == "running":
                        self.service.fund_window(connection, intent, current, self.repo.clock())
            except BudgetExceeded:
                # The failed transaction rolled back all funding mutations.
                # Preserve any running task/result and request normal drain.
                funded = False
                self._request_stop(node, reason_code="dstack_spend_limit_reached")
            with self.repo.engine.connect() as connection:
                node = dict(connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id == intent_id)).mappings().one())
        if not binding.get("apply_started"):
            with self.repo.engine.connect() as connection:
                intent = connection.execute(select(instance_intents).where(instance_intents.c.id == intent_id)).mappings().one()
                action = connection.execute(select(scaler_actions).where(scaler_actions.c.intent_id == intent_id)).mappings().one()
            # BOTH original journals must prove that no request was sent.
            # begin_apply repeats these checks atomically before any mutation.
            if intent["state"] != "reserved" or action["create_started_at"] is not None:
                return {"node_id": intent_id, "state": "reconciliation_required", "code": "dstack_apply_journal_mismatch"}
            if node["desired_state"] == "stopped" or not _window_open(intent, node, self.repo.clock()):
                self._request_stop(node)
                cancelled = self.capacity.store.cancel_unapplied(intent_id)
                return {"node_id": intent_id, "state": "never_applied" if cancelled else "reconciliation_required"}
            observation = self.capacity.start(_request(node["payload"]["request"]), self.service.config["ssh_public_key"])
            return {"node_id": intent_id, "state": observation["state"]}
        if binding.get("stop_started"):
            # Exact read + bounded journaled retry belongs to capacity.stop.
            # stop_started alone cannot prove that its HTTP request arrived.
            observation = self.capacity.stop(intent_id)
            return {"node_id": intent_id, "state": observation["state"]}
        observation = self.capacity.observe(intent_id)
        binding = self.capacity.store.load(intent_id)
        with self.repo.engine.connect() as connection:
            current = dict(connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id == intent_id)).mappings().one())
            intent = dict(connection.execute(select(instance_intents).where(instance_intents.c.id == intent_id)).mappings().one())
        workers, active, unsafe, last = self._obligations(intent)
        payload = current["payload"]
        now = self.repo.clock()
        observed = binding.get("observed_at")
        fresh = type(observed) in (int, float) and 0 <= now-observed <= self.service.policy["observation_fresh_seconds"]
        last = max(last, binding.get("first_runtime_ready_at", now))
        lifecycle = payload.get("lifecycle", {})
        action = idle_action(now=now, hard_deadline=payload["hard_deadline"], hold_until=payload["hold_until"],
            last_business_activity=last, idle_seconds=lifecycle.get("idle_shutdown_seconds", self.service.policy["idle_shutdown_seconds"]),
            active_jobs=active, unsafe_attempts=unsafe, collection_holds=int(binding.get("state") == "busy"),
            observations_fresh=bool(fresh), lifecycle_version=lifecycle.get("version", 1),
            keep_on=lifecycle.get("keep_on", "idle"), budget_available=funded)
        if action in {"stop", "drain"}:
            if not self._request_stop(current):
                action = "retain"
        stopping = current["desired_state"] == "stopped" or action in {"stop", "drain"}
        if stopping:
            if (active or unsafe) and binding.get("state") in {"ready", "busy"} and fresh and self.worker_process:
                # Restore the same CPU consumer for reconciliation/collection;
                # its drained registration cannot acquire new generation work.
                self.worker_process.ensure(intent_id, self.runtime.slot(intent_id))
            for worker in workers:
                if worker["state"] != "retired":
                    self.control.drain(worker["id"])
            # Retiring while a result is uncollected would lose its recovery
            # binding. Keep its CPU worker alive until the original job ends.
            if binding.get("state") == "ready" and fresh and not active and not unsafe:
                for worker in workers:
                    if worker["state"] != "retired":
                        self.control.retire(worker["id"], upstream_idle_confirmed=True)
                if self.worker_process:
                    self.worker_process.stop(intent_id)
                result = self.capacity.stop(intent_id)
                return {"node_id": intent_id, "state": result["state"]}
            # Cold model preparation has never admitted execution. It need not
            # finish downloading/loading merely to become eligible for stop.
            # begin_stop rechecks the same evidence under the capacity lock.
            cold_unadmitted = _cold_unadmitted_runtime(binding, workers,
                intent.get("provider_instance_id"), now, self.service.policy["observation_fresh_seconds"])
            if (not active and not unsafe and cold_unadmitted) or (
                    not binding.get("bootstrap_launch_started", binding.get("bootstrap_started"))
                    and not workers and binding.get("runtime_incarnation") is None
                    and binding.get("first_runtime_ready_at") is None):
                result = self.capacity.stop(intent_id)
                return {"node_id": intent_id, "state": result["state"]}
            return {"node_id": intent_id, "state": "draining"}
        if binding.get("state") == "ready" and fresh:
            slot = self.runtime.slot(intent_id)
            try:
                registered = self.control.register(slot.spec)
            except BudgetExceeded as error:
                if error.args == ("global_capacity_exceeded",):
                    self.capacity.store.record_registration(intent_id, slot.spec.worker_id, "global_capacity_exceeded")
                raise
            self.capacity.store.record_registration(intent_id, slot.spec.worker_id)
            if registered["state"] != "ready" and not registered["current_job_id"] and not registered["drain_requested"]:
                self.control.mark_ready(slot.spec.worker_id, upstream_idle_confirmed=True)
            if self.worker_process:
                self.worker_process.ensure(intent_id, slot)
            if self.broker_readiness:
                self.capacity.store.record_broker(intent_id,slot.spec.worker_id,
                    self.broker_readiness.projection(slot))
        elif binding.get("state") == "busy" and fresh and workers:
            # Running original inference must not age out its live consumer's
            # proof. Refresh transport eligibility without marking it idle.
            slot=self.runtime.slot(intent_id)
            if self.worker_process:
                self.worker_process.ensure(intent_id,slot)
            if self.broker_readiness:
                self.capacity.store.record_broker(intent_id,slot.spec.worker_id,
                    self.broker_readiness.projection(slot))
        return {"node_id": intent_id, "state": observation["state"]}

    def tick(self):
        results = []
        for node in self._nodes():
            try:
                # A task terminal is not an invoice or provider destruction proof.
                if node["runtime_state"] == "stopped":
                    with self.repo.engine.connect() as connection:
                        intent = dict(connection.execute(select(instance_intents).where(instance_intents.c.id == node["intent_id"])).mappings().one())
                    _, active, unsafe, _ = self._obligations(intent)
                    if not active and not unsafe:
                        if self.worker_process:
                            self.worker_process.stop(node["intent_id"])
                        self.runtime.close(node["intent_id"])
                    continue
                results.append(self._tick_node(node))
            except Exception as error:
                code = ("global_capacity_exceeded" if isinstance(error, BudgetExceeded)
                    and error.args == ("global_capacity_exceeded",) else "dstack_controller_observation_failed")
                results.append({"node_id": node["intent_id"], "state": "reconciliation_required", "code": code})
        return {"state": "running", "nodes": results}


class HatchetProcesses:
    """Only CPU worker processes. No arbitrary command or GPU process signal."""
    def __init__(self, work_dir, broker_config, *, popen=subprocess.Popen):
        self.work_dir, self.broker_config = Path(work_dir), Path(broker_config)
        if not self.work_dir.is_absolute() or not self.broker_config.is_absolute():
            raise ValueError("absolute_dstack_worker_paths_required")
        self.popen, self.children = popen, {}
        self._supervisor_lock = None

    def __enter__(self):
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._supervisor_lock = _slot_lock(self.work_dir,"dstack-cpu-supervisor")
        if not self._supervisor_lock.__enter__():
            self._supervisor_lock.__exit__(None,None,None)
            self._supervisor_lock = None
            raise ValueError("dstack_cpu_supervisor_already_active")
        return self

    def ensure(self, intent_id, slot):
        if self._supervisor_lock is None:
            raise ValueError("dstack_cpu_supervisor_lock_required")
        prior = self.children.get(intent_id)
        if prior and prior.poll() is None:
            return
        if prior:
            prior.wait()  # Reap an exited child before replacing this exact slot.
        directory = self.work_dir/intent_id
        directory.mkdir(parents=True, exist_ok=True)
        item = asdict(slot)
        item.update(worker_spec_payload(slot.spec))
        item.pop("spec")
        value = {"version": 2, "work_dir": str(self.work_dir), "enabled": True,
            "max_children": 1, "shutdown_grace_s": 210, "slots": [item]}
        path = directory/"fleet.json"
        temporary = directory/"fleet.tmp"
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
        self.children[intent_id] = self.popen([sys.executable, "-m", "studio_platform.hatchet_dispatch", "worker",
            "--broker-config", str(self.broker_config), "--fleet-config", str(path), "--worker-id", slot.spec.worker_id],
            stdin=subprocess.DEVNULL)

    def stop(self, intent_id):
        process = self.children.get(intent_id)
        if process and process.poll() is None:
            process.terminate()  # Original job is already terminal and native idle.
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()  # Our CPU child only; GPU job/tag remains durable.
                process.wait(timeout=5)
        elif process:
            process.wait()
        self.children.pop(intent_id, None)

    def close(self):
        failure = None
        try:
            for intent_id in list(self.children):
                try:
                    self.stop(intent_id)
                except Exception as error:
                    # Still reap the other children if one refuses shutdown.
                    failure = failure or error
        finally:
            if self._supervisor_lock is not None:
                self._supervisor_lock.__exit__(None,None,None)
                self._supervisor_lock = None
        if failure is not None:
            raise failure

    def __exit__(self, *_):
        self.close()


def main(argv=None):
    """CPU-only supervisor; construction never creates schemas or budgets."""
    parser=argparse.ArgumentParser(description="Original-ledger dstack reconciliation and native slot supervisor")
    parser.add_argument("--once", action="store_true")
    args=parser.parse_args(argv)
    if os.name != "posix":
        raise ValueError("dstack_controller_requires_linux")
    from .settings import Settings
    from .repository import Repository
    from .dstack_operator import from_environment
    from .dstack_factory import create_runtime
    settings=Settings.from_environment()
    repo=Repository(settings.database_url)
    service=None; runtime=None; processes=None
    try:
        service=from_environment(repo,settings)
        if service.capacity is None:
            raise ValueError("dstack_capacity_disabled")
        runtime,broker,work_dir=create_runtime(repo,service.capacity.store)
        from .hatchet_readiness import BrokerReadiness
        from .hatchet_dispatch import read_broker_config
        readiness=BrokerReadiness(read_broker_config(broker),clock=repo.clock)
        processes=HatchetProcesses(work_dir,broker).__enter__()
        controller=DstackController(service,runtime,worker_process=processes,broker_readiness=readiness)
        stop=threading.Event()
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT,signal.SIGTERM):
                signal.signal(signum,lambda *_:stop.set())
        while not stop.is_set():
            print(json.dumps(controller.tick(),sort_keys=True),flush=True)
            if args.once:
                return 0
            stop.wait(15)
    finally:
        # Closing local tunnels is safe; durable broker/original attempts remain
        # recoverable. Do not translate process shutdown into paid-host stop.
        try:
            if processes:
                processes.close()
        finally:
            try:
                if runtime:
                    runtime.close()
            finally:
                try:
                    if service and service.capacity:
                        service.capacity.client.close()
                finally:
                    repo.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit("dstack_controller_failed; inspect protected service diagnostics") from None
