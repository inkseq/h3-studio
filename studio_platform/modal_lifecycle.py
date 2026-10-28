"""Reconcile original Modal hosts, native slots and fenced attempt timeouts.

Runs in the existing controller loop. Admission being OFF never disables
reconciliation. Exit and timeout do not settle invoices or recreate a host.
"""

import uuid

from sqlalchemy import insert, select

from .capacity_watchdog import reclaimed_attempt, timeout_reason
from .queue import TaskQueue
from .repository import (
    Conflict,
    InvalidTransition,
    Scope,
    attempts,
    instance_intents,
    jobs,
    request_hash,
    scaler_receipts,
)

REPORTED = 'modal_reclaim_reported'


class ModalLifecycle:
    def __init__(self, repo, hosts, bindings, slots, settings, *, report):
        self.repo, self.hosts, self.bindings = repo, hosts, bindings
        self.slots, self.settings, self.report = slots, settings, report

    def _originals(self):
        with self.repo.engine.connect() as c:
            return list(
                c.execute(
                    select(instance_intents).where(
                        instance_intents.c.provider == 'modal'
                    )
                ).mappings()
            )

    def has_hosts(self):
        return any(
            row['state'] not in {'destroyed', 'not_created'}
            for row in self._originals()
        )

    def _attempts(self, identity):
        worker = 'modal-' + identity.replace('-', '')
        with self.repo.engine.connect() as c:
            related = list(
                c.execute(
                    select(attempts).where(attempts.c.worker_id == worker)
                ).mappings()
            )
            return [
                (
                    dict(attempt),
                    dict(
                        c.execute(select(jobs).where(jobs.c.id == attempt['job_id']))
                        .mappings()
                        .one()
                    ),
                )
                for attempt in related
            ]

    def _reclaims(self, identity):
        receipts = []
        silence = getattr(self.settings, 'capacity_no_progress_seconds', 600)
        floor = getattr(self.settings, 'capacity_runtime_floor_seconds', 1800)
        for attempt, job in self._attempts(identity):
            if reclaimed_attempt(job, attempt):
                receipts.extend(
                    row
                    for row in job['result']['capacity_reclaims']
                    if row['attempt_id'] == attempt['id']
                )
                continue
            if job['current_attempt_id'] != attempt['id'] or job['status'] not in {
                'submitting',
                'submission_unknown',
                'running',
            }:
                continue
            due = timeout_reason(
                job,
                attempt,
                self.repo.clock(),
                no_progress_seconds=silence,
                runtime_floor_seconds=floor,
            )
            if not due:
                continue
            reason, last = due
            try:
                receipts.append(
                    TaskQueue(self.repo).reclaim_capacity(
                        Scope(
                            job['tenant_id'],
                            job['owner_id'],
                            job['project_id'],
                            job['actor_id'],
                        ),
                        job['id'],
                        expected_attempt_id=attempt['id'],
                        expected_fence=job['fence'],
                        reason=reason,
                        last_progress_at=last,
                        evidence_id=request_hash(
                            dict(
                                attempt=attempt['id'],
                                reason=reason,
                                last_progress_at=last,
                            )
                        ),
                        no_progress_seconds=silence,
                        runtime_floor_seconds=floor,
                    )
                )
            except (Conflict, InvalidTransition):
                # A concurrent completion, cancellation or engine progress wins.
                # No reclaim receipt means no timeout authority to stop this job.
                continue
        return receipts

    def _alerts(self, identity, receipts):
        with self.repo.engine.connect() as c:
            sent = {
                r['event_id']
                for r in c.execute(
                    select(scaler_receipts.c.facts).where(
                        scaler_receipts.c.intent_id == identity,
                        scaler_receipts.c.operation == REPORTED,
                    )
                ).scalars()
            }
        for receipt in receipts:
            if receipt['event_id'] in sent:
                continue
            try:
                accepted = self.report(identity, receipt) is True
            except Exception:
                accepted = False
            if accepted:
                with self.repo.transaction() as c:
                    self.repo._lock_capacity(c)
                    original = list(
                        c.execute(
                            select(scaler_receipts.c.facts).where(
                                scaler_receipts.c.intent_id == identity,
                                scaler_receipts.c.operation == REPORTED,
                            )
                        ).scalars()
                    )
                    if {'event_id': receipt['event_id']} not in original:
                        c.execute(
                            insert(scaler_receipts).values(
                                id=str(uuid.uuid4()),
                                intent_id=identity,
                                operation=REPORTED,
                                observed_at=self.repo.clock(),
                                facts={'event_id': receipt['event_id']},
                            )
                        )

    def _one(self, intent):
        identity = intent['id']
        receipts = self._reclaims(identity)
        self._alerts(identity, receipts)
        # A positive process exit is removal evidence only. Lost work still
        # passes through the original timeout/fence path, even after exit.
        fact = (
            self.hosts.observe(identity)
            if intent['state'] not in {'destroyed', 'not_created'}
            else {'state': intent['state']}
        )
        state = fact['state']
        if state in {'destroyed', 'not_created'}:
            self.slots.stop(identity)
            return dict(intent_id=identity, state=state)
        startup_due = (
            state == 'starting'
            and not self._attempts(identity)
            and self.repo.clock() - intent['created_at']
            >= getattr(self.settings, 'capacity_startup_timeout_seconds', 1200)
        )
        deadline_due = (
            intent['hard_deadline'] is not None
            and self.repo.clock() >= intent['hard_deadline']
        )
        if (
            receipts
            or startup_due
            or deadline_due
            or state in {'draining', 'destroying'}
        ):
            # Original reclaim committed before any stop. The provider's fixed
            # hard deadline applies independently and is never extended here.
            result = self.hosts.stop(identity)
            self.slots.stop(identity)
            return result
        if state in {'starting', 'ready', 'busy'}:
            slot = self.bindings.slot(identity)
            self.slots.ensure(identity, slot)
            if state == 'starting':
                with self.repo.transaction() as c:
                    self.repo._lock_capacity(c)
                    row = self.repo._locked(
                        c,
                        select(instance_intents).where(
                            instance_intents.c.id == identity
                        ),
                    )
                    if row['state'] == 'starting':
                        self.repo.update_instance(identity, 'ready', connection=c)
            return dict(intent_id=identity, state='ready')
        return dict(intent_id=identity, state=state)

    def sweep(self):
        result = []
        for intent in self._originals():
            try:
                result.append(self._one(intent))
            except Exception:
                result.append(dict(intent_id=intent['id'], state='unconfirmed'))
        return result
