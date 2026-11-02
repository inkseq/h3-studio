"""DBOS owns durable wake/wait/replay; the original ledger owns execution.

No import-time I/O, provider calls, automatic migrations, new budgets or job
tables. Producers enqueue IDs in the SAME transaction as original admission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from sqlalchemy import select

from . import workflow_status
from .repository import Conflict, identifier, jobs
from .domain_turns import DomainTurns
from .providers.registry import LOCAL_BACKENDS
from .local_slots import LocalSlotOwner, LOCAL_SLOT_FAMILIES  # noqa: F401 -- existing import
from .slot_turns import (
    Slot as _Slot,  # noqa: F401 -- existing test/import identity
    SlotContexts,
    selected_job_turn,
    run_selected_slot,
    POLL_SECONDS,
    TERMINAL as TERMINAL,
)
from .workflow_schema import (
    INKSEQ_WORKFLOW_SCHEMA as SCHEMA,  # noqa: F401 -- public workflow-schema compatibility export
    WORKFLOW_APPLICATION_NAME as APPLICATION,
    WORKFLOW_APPLICATION_VERSION as VERSION,
)

QUEUE = "inkseq-generation-v1"
WORKFLOW = "inkseq.generation.v1"
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


class DurableSlots(SlotContexts):
    """Combined in-process facade; native lifecycle stays controller-owned.

    LocalSlotOwner is reusable without the SDK, but is not yet a separate
    service/cgroup. All records retain one original dictionary, lock and fence.
    """

    def __init__(self, repo, settings, work_dir, *, runner_context=None):
        super().__init__(repo, settings, work_dir, runner_context=runner_context)
        self.local = LocalSlotOwner(self)

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

    def ensure(self, intent_id, slot):
        self.open(intent_id, slot)

    def add_local_slots(self):
        self.local.add_slots()

    def heartbeat_cpu(self):
        self.local.heartbeat()

    def turn(self, tenant, owner, project, job_id):
        return selected_job_turn(
            self, tenant, owner, project, job_id, run_selected=self._run_selected
        )

    def _run_selected(self, slot, job_id):
        if slot.spec.backend in LOCAL_BACKENDS:
            return self.local.run_selected_slot(slot, job_id)
        return run_selected_slot(slot, job_id)


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
            self.slots.add_local_slots()
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
