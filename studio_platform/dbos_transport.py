"""DBOS owns durable wake/wait/replay; the original ledger owns execution.

No import-time I/O, provider calls, automatic migrations, new budgets or job
tables. Producers enqueue IDs in the SAME transaction as original admission.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import threading

from sqlalchemy import select

from . import workflow_status
from .repository import Conflict, Scope, identifier, jobs
from .domain_turns import DomainTurns
from .workflow_schema import (
    INKSEQ_WORKFLOW_SCHEMA as SCHEMA,  # noqa: F401 -- public workflow-schema compatibility export
    WORKFLOW_APPLICATION_NAME as APPLICATION,
    WORKFLOW_APPLICATION_VERSION as VERSION,
)

QUEUE = "inkseq-generation-v1"
WORKFLOW = "inkseq.generation.v1"
POLL_SECONDS = 10
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
# Slots without a native GPU node: no dstack demand, kept alive by heartbeat_cpu.
LOCAL_BACKENDS = frozenset({"cpu-render", "runpod-enhance"})
ACTIVE = frozenset(
    {
        "queued",
        "waiting_capacity",
        "claimed",
        "submitting",
        "running",
        "submission_unknown",
        "collecting",
        "cancel_requested",
    }
)


def workflow_id(tenant, owner, project, job_id):
    for value in (tenant, owner, project, job_id):
        identifier(value)
    digest = hashlib.sha256(
        json.dumps([tenant, owner, project, job_id], separators=(",", ":")).encode()
    ).hexdigest()
    return "inkseq-job-" + digest


class GenerationTransport:
    def __init__(self, engine, *, client=None):
        if engine.dialect.name != "postgresql":
            raise ValueError("dbos_requires_original_postgresql")
        if client is None:
            from dbos import DBOSClient
            from .workflow_schema import dbos_client_config

            client = DBOSClient(**dbos_client_config(engine))
        self.client = client

    def job_event(self, connection, job_id, event_id, *, event_type="job.created"):
        from .workflow_schema import assert_execution_allowed

        assert_execution_allowed(connection)
        row = (
            connection.execute(
                select(
                    jobs.c.id,
                    jobs.c.tenant_id,
                    jobs.c.owner_id,
                    jobs.c.project_id,
                    jobs.c.status,
                    jobs.c.fence,
                )
                .where(jobs.c.id == job_id)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        base = workflow_id(
            row["tenant_id"], row["owner_id"], row["project_id"], row["id"]
        )
        active = workflow_status.open_ids(connection, base, APPLICATION, WORKFLOW)
        if len(active) > 1:
            raise Conflict("workflow_original_job_binding_ambiguous")
        if active:
            ident = active[0]
        else:
            if row["status"] not in ACTIVE:
                return  # Historical/planned/terminal records do not acquire workflows.
            previous = workflow_status.latest_id(
                connection, base, APPLICATION, WORKFLOW
            )
            if previous and event_type not in {
                "job.queued",
                "job.waiting_capacity",
                "job.preparation_recovered",
            }:
                return  # Timers/observations cannot restart a terminal SDK run.
            # An explicit original requeue/recovery event after a terminal SDK
            # run earns a transport epoch, never a new job/native attempt/hold.
            # The same event+fence deterministically replays this epoch. Timers
            # never restart SDK errors or generate another transport identity.
            epoch = hashlib.sha256(
                json.dumps([row["fence"], event_id]).encode()
            ).hexdigest()
            ident = base + "/" + epoch if previous else base
            self.client.enqueue_in_transaction(
                connection,
                {
                    "workflow_name": WORKFLOW,
                    "queue_name": QUEUE,
                    "workflow_id": ident,
                    "app_version": VERSION,
                    "application_name": APPLICATION,
                },
                row["tenant_id"],
                row["owner_id"],
                row["project_id"],
                row["id"],
            )
        # This event is a hint. Claim/cancel/settlement still reread the original
        # row under its original fence; a duplicate message cannot submit twice.
        self.client.send_in_transaction(
            connection,
            ident,
            {"event_id": event_id},
            topic="ledger",
            idempotency_key=event_id,
        )

    def close(self):
        self.client.destroy()

    def payment_event(self, connection, tenant, event_id):
        """Wake a verified original event in the existing durable SDK queue."""
        from .payment_schema import events
        from .payments_workflows import PAYMENT_WORKFLOW, payment_workflow_id
        from .workflow_schema import assert_execution_allowed

        assert_execution_allowed(connection)
        event = connection.execute(
            select(events.c.status)
            .where(events.c.tenant == tenant, events.c.event_id == event_id)
            .with_for_update()
        ).scalar_one()
        if event in {"processed", "review"}:
            return
        ident = payment_workflow_id(tenant, event_id)
        self.client.enqueue_in_transaction(
            connection,
            {
                "workflow_name": PAYMENT_WORKFLOW,
                "queue_name": QUEUE,
                "workflow_id": ident,
                "app_version": VERSION,
                "application_name": APPLICATION,
            },
            tenant,
            event_id,
        )
        self.client.send_in_transaction(
            connection,
            ident,
            {"event_id": event_id},
            topic="ledger",
            idempotency_key="stripe-event-" + event_id,
        )

    def enqueue_account_operation(
        self, connection, tenant, operation_id, *, resume_event=None, kind="sessions"
    ):
        from .account_sessions import WORKFLOW as ACCOUNT_WORKFLOW
        from .workflow_schema import assert_execution_allowed

        if kind not in {"sessions", "deletion"}:
            raise ValueError("account_operation_kind_invalid")
        if kind == "deletion":
            from .account_deletion import WORKFLOW as ACCOUNT_WORKFLOW
        assert_execution_allowed(connection)
        identifier(tenant)
        if not isinstance(operation_id, str) or not re.fullmatch(
            r"[0-9a-f-]{36}", operation_id
        ):
            raise ValueError("account_operation_invalid")
        if resume_event is not None and (
            not isinstance(resume_event, str)
            or re.fullmatch(r"[0-9a-f]{64}", resume_event) is None
        ):
            raise ValueError("account_operation_event_invalid")
        base = (
            "inkseq-account-delete-" if kind == "deletion" else "inkseq-account-"
        ) + hashlib.sha256((tenant + "\0" + operation_id).encode()).hexdigest()
        # Always enqueue a deterministic epoch for this exact verified event.
        # An active workflow can become terminal between a status read and a
        # send, losing a wake. Native enqueue uniqueness avoids that race while
        # every epoch still reads the SAME account-operation business journal.
        ident = base + "/" + resume_event if resume_event is not None else base
        self.client.enqueue_in_transaction(
            connection,
            {
                "workflow_name": ACCOUNT_WORKFLOW,
                "queue_name": QUEUE,
                "workflow_id": ident,
                "app_version": VERSION,
                "application_name": APPLICATION,
            },
            tenant,
            operation_id,
        )

    def film_status(self, connection, tenant, owner, project, run_id):
        """Read the latest original Film transport epoch; never wake or retry it."""
        from .film_runs import VERSION as FILM_VERSION

        base = "inkseq-film-" + workflow_id(tenant, owner, project, run_id)[11:]
        return workflow_status.latest_status(
            connection, base, APPLICATION, VERSION, FILM_VERSION
        )

    def enqueue_film(self, connection, tenant, owner, project, run_id):
        from .film_runs import VERSION as FILM_VERSION
        from .workflow_schema import assert_execution_allowed

        assert_execution_allowed(connection)
        base = "inkseq-film-" + workflow_id(tenant, owner, project, run_id)[11:]
        rows = workflow_status.open_ids(connection, base, APPLICATION, FILM_VERSION)
        if len(rows) > 1:
            raise Conflict("workflow_film_binding_ambiguous")
        if rows:
            ident = rows[0]
        else:
            previous = workflow_status.latest_id(
                connection, base, APPLICATION, FILM_VERSION
            )
            # Called only by original confirmed submit/explicit resume, never a
            # timer. A transport epoch retains the same frozen run/jobs/holds.
            ident = (
                base
                if previous is None
                else base + "/" + hashlib.sha256(previous.encode()).hexdigest()
            )
            self.client.enqueue_in_transaction(
                connection,
                {
                    "workflow_name": FILM_VERSION,
                    "queue_name": FILM_VERSION,
                    "workflow_id": ident,
                    "app_version": VERSION,
                    "application_name": APPLICATION,
                },
                tenant,
                owner,
                project,
                run_id,
            )
        self.client.send_in_transaction(
            connection, ident, {"run_id": run_id}, topic="film_changed"
        )


def configure_producer(repo, *, client=None):
    if repo.generation_transport is None:
        from .workflow_schema import assert_execution_allowed, validate

        with repo.engine.connect() as connection:
            validate(connection)
            assert_execution_allowed(connection)
        repo.generation_transport = GenerationTransport(repo.engine, client=client)
    return repo.generation_transport


@dataclass
class _Slot:
    spec: object
    runner: object
    context: object
    lock: object


class DurableSlots:
    """In-process adapter registry, not a scheduler or a reconciliation service.

    dstack still owns native bindings and lifecycle. DBOS workflows call one
    bounded existing WorkerRunner turn with a selected original job ID.
    """

    def __init__(self, repo, settings, work_dir, *, runner_context=None):
        self.repo, self.settings = repo, settings
        self.work_dir = Path(work_dir)
        self._slots = {}
        self._lock = threading.RLock()
        self._fence = None
        self._runner_context = runner_context
        self._closing = False
        self._cpu_operation = threading.Lock()

    @property
    def children(self):
        # Original dstack demand checks count native slots, not permanently
        # available local CPU renderer capabilities. No idle provider polling.
        with self._lock:
            return {
                key: item
                for key, item in self._slots.items()
                if item.spec.backend not in LOCAL_BACKENDS
            }

    def heartbeat_cpu(self):
        with self._lock:
            for slot in self._slots.values():
                if slot.spec.backend in LOCAL_BACKENDS:
                    worker = slot.runner.control.get(slot.spec.worker_id)
                    if worker["expires_at"] > self.repo.clock() and worker[
                        "state"
                    ] not in {"unknown", "retired"}:
                        slot.runner.control.heartbeat(
                            slot.spec.worker_id, worker["fence"], lease_seconds=3600
                        )

    def add_render_slots(self):
        """Original bounded CPU renderer capabilities; no extra service/queue."""
        if not self.settings.render_enabled:
            return
        from .control import WorkerSpec
        from .fleet import SlotConfig
        from .render_plans import POOL, MODEL, RECIPE, CONFIGURATIONS

        for configuration in sorted(set(CONFIGURATIONS.values())):
            worker = "dbos-" + configuration
            spec = WorkerSpec(
                worker,
                POOL,
                "local-cpu",
                worker,
                (),
                (RECIPE,),
                MODEL,
                configuration,
                "cpu-render",
            )
            self.ensure(worker, SlotConfig(spec, enabled=True))
        self.heartbeat_cpu()

    def add_enhance_slot(self):
        """One CPU slot that submits enhance jobs to RunPod Serverless (#795)."""
        if not getattr(self.settings, "enhance_enabled", False):
            return
        from .control import WorkerSpec
        from .fleet import SlotConfig
        from . import enhance_plans as plan

        worker = "dbos-" + plan.CONFIGURATION
        spec = WorkerSpec(
            worker,
            plan.POOL,
            "runpod",
            worker,
            (),
            (plan.RECIPE,),
            plan.MODEL,
            plan.CONFIGURATION,
            plan.BACKEND,
        )
        self.ensure(worker, SlotConfig(spec, enabled=True))
        self.heartbeat_cpu()

    @contextmanager
    def _cpu_fence(self):
        """Fence all CPU configurations, including a surviving encoder child."""
        if os.name != "posix":
            yield True, None
            return
        import fcntl

        descriptor = os.open(
            self.work_dir / "cpu-render-operation.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077
            ):
                raise Conflict("dbos_cpu_operation_fence_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False, None
                return
            yield True, descriptor
        finally:
            # Child OFD ownership survives closing this parent FD after a crash.
            os.close(descriptor)

    def __enter__(self):
        from .slot_lock import slot_lock

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._fence = slot_lock(self.work_dir, "dstack-cpu-supervisor")
        if not self._fence.__enter__():
            self._fence.__exit__(None, None, None)
            self._fence = None
            raise ValueError("dstack_cpu_supervisor_already_active")
        return self

    def ensure(self, intent_id, slot):
        from .fleet import FleetConfig, slot_runner
        from .postgres_dispatch import record_consumer

        with self._lock:
            if self._closing or self._fence is None:
                raise Conflict("dbos_executor_stopping")
            current = self._slots.get(intent_id)
            if current is not None and current.spec != slot.spec:
                raise Conflict("dbos_native_slot_identity_changed")
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
                current = _Slot(slot.spec, runner, context, threading.Lock())
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

    def turn(self, tenant, owner, project, job_id):
        scope = Scope(tenant, owner, project)
        job = self.repo.get_job(scope, job_id)
        if job["status"] in TERMINAL or job["status"] == "recovery_hold":
            return {"state": job["status"], "done": True}
        if self._closing:
            return {
                "state": "executor_stopping",
                "done": False,
                "wait_seconds": POLL_SECONDS,
            }
        with self._lock:
            candidates = list(self._slots.values())
        # Existing attempts stay with their original worker. New work tries
        # dedicated capacity first; its original claim gates decide eligibility.
        candidates.sort(
            key=lambda slot: (
                slot.spec.worker_id != job['lease_worker_id']
                if job.get('lease_worker_id')
                else slot.spec.provider == 'modal'
            )
        )
        for slot in candidates:
            if slot.spec.pool != job["pool"] or not slot.lock.acquire(blocking=False):
                continue
            cpu = slot.spec.backend == "cpu-render"
            acquired_cpu = False
            try:
                if cpu:
                    acquired_cpu = self._cpu_operation.acquire(blocking=False)
                    if not acquired_cpu:
                        continue
                with (
                    self._cpu_fence() if cpu else nullcontext((True, None)) as (
                        physical,
                        descriptor,
                    )
                ):
                    if not physical:
                        continue  # Acquire no original job lease while CPU is busy.
                    slot.runner.selected_job_id = job_id
                    if cpu:
                        slot.runner.backend._operation_fence_fd = descriptor
                    try:
                        result = slot.runner.run_once(
                            slot.spec.worker_id, slot.spec.pool
                        )
                    finally:
                        if cpu:
                            slot.runner.backend._operation_fence_fd = None
                if slot.spec.backend in LOCAL_BACKENDS:
                    worker = slot.runner.control.get(slot.spec.worker_id)
                    if worker["expires_at"] > self.repo.clock() and worker[
                        "state"
                    ] not in {"unknown", "retired"}:
                        slot.runner.control.heartbeat(
                            slot.spec.worker_id, worker["fence"], lease_seconds=3600
                        )
                if result.get("job_id"):
                    if result["job_id"] != job_id:
                        raise Conflict("dbos_selected_job_mismatch")
                    break
            finally:
                slot.runner.selected_job_id = None
                if acquired_cpu:
                    self._cpu_operation.release()
                slot.lock.release()
        fresh = self.repo.get_job(scope, job_id)
        now = self.repo.clock()
        deadline = max(fresh["not_before"], fresh["lease_expires_at"] or now)
        return {
            "state": fresh["status"],
            "done": fresh["status"] in TERMINAL or fresh["status"] == "recovery_hold",
            "wait_seconds": max(POLL_SECONDS, min(900, deadline - now)),
        }

    def stop(self, intent_id):
        with self._lock:
            item = self._slots.get(intent_id)
            if item is None:
                return
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


def register_generation_workflow(DBOS, slots, *, domain_turns=None):
    domain_turns = domain_turns or DomainTurns()
    operation_limit = asyncio.Semaphore(4)

    @DBOS.step(name="inkseq.generation.domain-turn.v1", retries_allowed=False)
    async def domain_turn(tenant, owner, project, job_id):
        # DBOS persists this result. A process failure before that persistence
        # re-enters the ORIGINAL lease/attempt/native-tag reconciliation path.
        async with operation_limit:
            return await domain_turns.call(slots.turn, tenant, owner, project, job_id)

    @DBOS.workflow(name=WORKFLOW, max_recovery_attempts=None)
    async def generation(tenant, owner, project, job_id):
        while True:
            value = await domain_turn(tenant, owner, project, job_id)
            if value["done"]:
                return {"job_id": job_id, "state": value["state"]}
            await DBOS.recv_async("ledger", timeout_seconds=value["wait_seconds"])

    return generation


class DurableExecutor:
    """One existing-controller executor. Shutdown leaves native work untouched."""

    def __init__(
        self,
        repo,
        settings,
        work_dir,
        *,
        film_setup=None,
        account_services=None,
        runner_context=None,
    ):
        self.repo, self.settings = repo, settings
        self.slots = DurableSlots(
            repo, settings, work_dir, runner_context=runner_context
        )
        self.film_setup = film_setup
        self.account_services = account_services
        self.domain_turns = DomainTurns()
        self._DBOS = None

    def __enter__(self):
        from dbos import DBOS
        from .workflow_schema import dbos_config

        self.slots.__enter__()
        try:
            configure_producer(self.repo)
            self.slots.add_render_slots()
            self.slots.add_enhance_slot()
            # The configured environment is stable across ordinary releases.
            environment = (
                "staging"
                if self.settings.public_origin == "https://staging.inkseq.com"
                else "production"
            )
            DBOS(
                config=dbos_config(
                    self.settings.database_url,
                    executor_id="inkseq-workflows-" + environment,
                )
            )
            # The public runtime method needs a launched system DB. The public
            # client can persist the named queue before launch without DDL.
            self.repo.generation_transport.client.register_queue(
                QUEUE, polling_interval_sec=POLL_SECONDS
            )
            register_generation_workflow(
                DBOS, self.slots, domain_turns=self.domain_turns
            )
            from .payments_workflows import register_payment_workflows

            register_payment_workflows(
                DBOS, self.repo, self.settings, domain_turns=self.domain_turns
            )
            from .account_workflows import register_account_workflows

            register_account_workflows(
                DBOS,
                self.repo,
                self.settings,
                domain_turns=self.domain_turns,
                **(
                    {
                        "sessions": self.account_services[0],
                        "deletion": self.account_services[1],
                    }
                    if self.account_services is not None
                    else {}
                ),
            )
            if self.film_setup is not None:
                self.film_setup(DBOS, self.domain_turns)
            DBOS.launch()
            self._DBOS = DBOS
        except BaseException:
            self.domain_turns.drain()
            DBOS.destroy(destroy_registry=True)
            self.slots.close()
            raise
        return self

    def import_pending(self):
        """One bounded cutover scan, not an outbox replay or recurring reconciler.

        Originals keep every lease, native attempt and reservation. A workflow
        can only recover/claim these same originals after their fences permit.
        """
        total, cursor = 0, ""
        while True:
            with self.repo.transaction() as connection:
                rows = (
                    connection.execute(
                        select(jobs.c.id)
                        .where(jobs.c.status.in_(ACTIVE), jobs.c.id > cursor)
                        .order_by(jobs.c.id)
                        .limit(100)
                    )
                    .scalars()
                    .all()
                )
                for job_id in rows:
                    self.repo.generation_transport.job_event(
                        connection,
                        job_id,
                        "dbos-v1-cutover-" + job_id,
                        event_type="transport.cutover",
                    )
            if not rows:
                return total
            total += len(rows)
            cursor = rows[-1]

    def close(self):
        self.slots._closing = True
        self.domain_turns.drain()
        if self._DBOS is not None:
            # Finite domain calls have drained; durable sleeps need not finish.
            # SDK cancellation leaves them recoverable, not business-cancelled.
            # Keep slot/native fences until the executor has stopped. Existing
            # container grace remains the fallback for a stuck external I/O.
            self._DBOS.destroy(destroy_registry=True, workflow_completion_timeout_sec=0)
            self._DBOS = None
        self.slots.close()

    def __exit__(self, *_):
        self.close()
