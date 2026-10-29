"""Explicit Modal fallback admission using original job and rental ledgers.

No alternate queue, job mutation or provider retry. A new host is bound to one
original demand observation; an uncertain/terminated host is never re-created.
The existing Vast lookahead planner runs independently of this fallback.
"""

from dataclasses import asdict
import math
import uuid

from sqlalchemy import insert, select

from .control import WorkerControl
from .dstack_test_budget import ACCOUNT, PROJECTS, finite, task_guard
from .modal_budget import bind_test_guard, production_guard, require
from .modal_capacity import BACKEND, admission, precreate
from .modal_runtime import PROFILE
from .repository import (
    BudgetExceeded,
    Conflict,
    Scope,
    instance_intents,
    registered_workers,
    scaler_receipts,
)
from .runtime_catalog import engine_manifest
from .worker_admission import worker_window_reason

OPERATION = 'modal_demand_bound'


def dedicated_ready(connection, job, now):
    """Only a live, compatible, idle original worker can suppress fallback."""
    workers = connection.execute(
        select(registered_workers).where(
            registered_workers.c.provider.in_(('vast', 'runpod')),
            registered_workers.c.pool == job['pool'],
            registered_workers.c.state == 'ready',
            registered_workers.c.current_job_id.is_(None),
            registered_workers.c.expires_at > now,
        )
    ).mappings()
    return any(
        not worker['drain_requested']
        and WorkerControl.matches(worker, job)
        and worker_window_reason(
            connection,
            worker,
            now,
            expected_runtime_s=job['execution_plan'].get('expected_runtime_s'),
            deployment_profile_id=job['request'].get('deployment_profile_id'),
        )
        is None
        for worker in workers
    )


def available_modal(connection, job, now, selections):
    """Count compatible original starts and live idle hosts, once per input."""
    result = set()
    expected = job['execution_plan'].get('expected_runtime_s')
    if not finite(expected) or expected <= 0:
        return result
    for intent in connection.execute(
        select(instance_intents).where(
            instance_intents.c.provider == 'modal',
            instance_intents.c.pool == job['pool'],
            instance_intents.c.state.in_(
                ('reserved', 'creating', 'creation_unknown', 'starting', 'ready')
            ),
        )
    ).mappings():
        selection = selections.get(intent['id'], {})
        if (
            selection.get('configuration_id')
            != job['execution_plan'].get('configuration_id')
            or selection.get('manifest_digest')
            != job['execution_plan'].get('engine_manifest_digest')
            or not finite(intent['hard_deadline'])
            or now + expected + 120 >= intent['hard_deadline']
        ):
            continue
        if intent['state'] != 'ready':
            result.add(intent['id'])
            continue
        workers = connection.execute(
            select(registered_workers).where(
                registered_workers.c.provider == 'modal',
                registered_workers.c.instance_id == intent['provider_instance_id'],
                registered_workers.c.state == 'ready',
                registered_workers.c.current_job_id.is_(None),
                registered_workers.c.expires_at > now,
            )
        ).mappings()
        if any(
            not worker['drain_requested']
            and WorkerControl.matches(worker, job)
            and worker_window_reason(
                connection, worker, now, expected_runtime_s=expected
            )
            is None
            for worker in workers
        ):
            result.add(intent['id'])
    return result


class ModalAdmission:
    def __init__(
        self,
        service,
        hosts,
        *,
        environment=None,
        enabled=False,
        budget_account_id=None,
        start_after_seconds=0,
    ):
        self.service, self.hosts, self.repo = service, hosts, service.repo
        # Backstop delay: a Modal host starts only for work queued this long
        # (owner: Vast first, Modal covers a slow or failed start; #688).
        self.start_after_seconds = start_after_seconds
        self.production = environment == 'production'
        self.budget_account_id = budget_account_id
        self.enabled = (
            enabled is True
            and environment in {'staging', 'production'}
            and environment == hosts.environment
            and hosts.enabled
            and (not self.production or isinstance(budget_account_id, str))
        )
        hosts.capacity_check = lambda c, intent: precreate(service, hosts, c, intent)

    def _reserve(self, c, job, profile, now):
        from .dstack_demand import validate_start

        # Repeat the original demand runtime, owner, accepted-profile, job lease,
        # unresolved-attempt and policy checks inside the reservation transaction.
        demand = validate_start(self.service, c, job['id'], profile['id'])
        request, execution = job['request'], job['execution_plan']
        mode = request.get('request', {}).get('mode')
        require(
            profile['runtime_profile_id']
            == request.get('deployment_profile_id')
            == PROFILE
        )
        require(mode == profile['mode'] and mode in {'fl', 'ref'})
        manifest = engine_manifest(PROFILE, mode)
        require(execution.get('engine_manifest_digest') == manifest.digest)
        require(request.get('recipe_id') == manifest.document['generation_recipe_id'])
        limits = self.service._admin_limits(c)['value']
        if self.production:
            # A customer job: funded by the production account its profile
            # already names, never the staging test ledger.
            require(
                self.budget_account_id in profile['budget_account_ids']
                and profile['project_id'] not in PROJECTS
            )
            guard = production_guard(
                self.budget_account_id,
                max_runtime=min(limits['max_runtime_seconds'], 7200),
            )
        else:
            require(
                ACCOUNT in profile['budget_account_ids']
                and profile['project_id'] in PROJECTS
            )
            policy = self.service.policy
            guard = task_guard(
                policy['test_gpu_guard'],
                policy['demand_start']['test_task_id'],
                limit=limits['test_task_limit_microusd'],
                max_runtime=limits['max_runtime_seconds'],
            )
        duration = min(guard['max_runtime_seconds'], 7200)
        expected = execution.get('expected_runtime_s')
        require(
            finite(expected)
            and expected > 0
            and duration > expected + guard['stop_margin_seconds']
        )
        cost = math.ceil(
            self.hosts.spec.max_hourly_microusd
            * (duration + guard['stop_margin_seconds'])
            / 3600
        )
        snapshot, usage = admission(
            self.service,
            c,
            profile,
            self.hosts.spec,
            production=self.production,
            additional_microusd=cost if self.production else 0,
        )
        intent = self.repo.reserve_instance_intent(
            Scope(
                self.service.settings.tenant_id,
                profile['owner_id'],
                profile['project_id'],
            ),
            profile['pool'],
            'modal-demand-' + job['id'],
            provider='modal',
            hard_deadline=now + duration,
            reserved_cost_microusd=cost,
            budget_account_ids=profile['budget_account_ids'],
            dry_run=False,
            connection=c,
            capacity_usage=usage,
            active_instance_ids=usage['active_instance_ids'],
            capacity_policy_snapshot=snapshot,
            capacity_provenance=(
                dict(
                    capacity_backend=BACKEND,
                    lifecycle_version=2,
                    tenant_id=self.service.settings.tenant_id,
                    owner_id=profile['owner_id'],
                    project_id=profile['project_id'],
                    pool=profile['pool'],
                    provider='modal',
                )
                if snapshot is not None
                else None
            ),
        )
        require(intent['created'])
        bind_test_guard(self.repo, c, intent['id'], guard, asdict(self.hosts.spec))
        c.execute(
            insert(scaler_receipts).values(
                id=str(uuid.uuid4()),
                intent_id=intent['id'],
                operation=OPERATION,
                observed_at=now,
                facts={
                    'job_id': job['id'],
                    'demand': demand,
                    'mode': mode,
                    'configuration_id': execution['configuration_id'],
                    'manifest_digest': manifest.digest,
                    'capacity_backend': BACKEND,
                    'capacity_policy_snapshot': snapshot,
                },
            )
        )
        return intent['id']

    def _select(self, c):
        from .dstack_demand import eligible_jobs

        self.repo._lock_capacity(c)
        now = self.repo.clock()
        work = eligible_jobs(self.service, c, now)
        bindings, selections = {}, {}
        for receipt in c.execute(
            select(scaler_receipts).where(
                scaler_receipts.c.operation == OPERATION,
            )
        ).mappings():
            job_id = receipt['facts']['job_id']
            require(job_id not in bindings)
            bindings[job_id] = receipt['intent_id']
            require(receipt['intent_id'] not in selections)
            selections[receipt['intent_id']] = receipt['facts']
        active = list(
            c.execute(
                select(instance_intents.c.id).where(
                    instance_intents.c.provider == 'modal',
                    instance_intents.c.state.not_in(('destroyed', 'not_created')),
                )
            ).scalars()
        )
        original = None
        # Bound queued inputs already consume their original starting/idle slot.
        # Remaining compatible slots each cover at most one unbound input.
        consumed = {bindings[job['id']] for job, _ in work if job['id'] in bindings}
        for job, profile in work:
            if dedicated_ready(c, job, now):
                continue
            if job['id'] in bindings:
                original = original or bindings[job['id']]
                continue
            spare = available_modal(c, job, now, selections) - consumed
            if spare:
                consumed.add(sorted(spare)[0])
                continue
            if now - job['created_at'] < self.start_after_seconds:
                continue
            if len(active) >= self.hosts.spec.max_containers:
                return {
                    'state': 'blocked',
                    'reason_code': 'modal_container_limit',
                }, None
            return {'new': True}, self._reserve(c, job, profile, now)
        return (
            ({'state': 'idle'}, None)
            if original is None
            else ({'new': False}, original)
        )

    def tick(self):
        if not self.enabled:
            return {'state': 'disabled'}
        try:
            with self.repo.transaction() as c:
                result, identity = self._select(c)
            # Provider I/O only AFTER the original funding/binding commit.
            if identity is None:
                return result
            return (
                self.hosts.start(identity)
                if result['new']
                else self.hosts.observe(identity)
            )
        except (BudgetExceeded, Conflict, ValueError, KeyError, TypeError):
            # No raw provider, request or policy values in controller logs.
            return {'state': 'blocked', 'reason_code': 'modal_admission_refused'}
