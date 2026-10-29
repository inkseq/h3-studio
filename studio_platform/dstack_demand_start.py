"""One controller decision, journaled on the original operator-command table.

Planning is a rate-limited read, not a rental. A frozen preview/start key is
retained across response loss; only the original start transaction may allocate.
This module neither settles invoices nor changes an accepted generation job.
"""

import copy
import math
import uuid

from sqlalchemy import insert, select, update

from .dstack_capacity import DstackReadRateLimited
from .dstack_operator import _actor
from .operator_capacity import operator_commands, operator_nodes
from .repository import request_hash

KIND = 'dstack_demand'
LEASE_SECONDS = 60
STATES = {'planning', 'starting', 'observing', 'backoff', 'blocked'}
MAX_DOWNLOAD_REPLACEMENTS = 2
DOWNLOAD_RETRY_LIMIT = 'download_retry_limit'
DOWNLOAD_TIMEBOX = 'dstack_download_timebox_exceeded'


def download_journal(connection, tenant, job_id):
    """Read the original per-job journal; malformed retry state grants no rental."""
    actor = 'dstack-demand:' + request_hash({'tenant': tenant})[:40]
    row = (
        connection.execute(
            select(operator_commands).where(
                operator_commands.c.actor == actor,
                operator_commands.c.kind == KIND,
                operator_commands.c.idempotency_key == 'demand-job-' + job_id,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    from .operator_capacity import require

    payload = row['payload']
    stops = payload.get('download_stops', [])
    replacements = payload.get('download_replacements', 0)
    require(
        payload.get('version') == 1
        and payload.get('tenant_id') == tenant
        and payload.get('job_id') == job_id
        and type(replacements) is int
        and 0 <= replacements <= MAX_DOWNLOAD_REPLACEMENTS
        and isinstance(stops, list)
        and len(stops) <= MAX_DOWNLOAD_REPLACEMENTS + 1
        and all(
            isinstance(s, dict)
            and set(s) == {'node_id', 'machine_id', 'at'}
            and isinstance(s['node_id'], str)
            and 1 <= len(s['node_id']) <= 100
            and isinstance(s['machine_id'], str)
            and s['machine_id'].isdigit()
            and 1 <= len(s['machine_id']) <= 18
            and type(s['at']) in (int, float)
            and math.isfinite(s['at'])
            and s['at'] > 0
            for s in stops
        )
        and len({s['node_id'] for s in stops}) == len(stops),
        'dstack_demand_download_state_invalid',
    )
    return dict(row)


def excluded_download_machines(connection, tenant, job_id):
    row = download_journal(connection, tenant, job_id)
    if row is None:
        return set()
    return {s['machine_id'] for s in row['payload'].get('download_stops', [])}


def _delay(job_id, error=None):
    retry = (
        getattr(error, 'retry_after', 0)
        if isinstance(error, DstackReadRateLimited)
        else 0
    )
    retry = retry if type(retry) in (int, float) and math.isfinite(retry) else 0
    return (
        min(86400, max(LEASE_SECONDS, retry)) + int(request_hash(job_id)[:4], 16) % 16
    )


def _start_key(identity, cycle):
    return 'demand-start-' + request_hash({'identity': identity, 'cycle': cycle})[:48]


def _result(state, reason=None, command=None):
    value = {'state': state}
    if reason:
        value['reason_code'] = reason
    if command is not None:
        value.update(command_id=command['id'], node_id=command['payload']['node_id'])
    return value


def _start_command(connection, service, profile, key):
    row = (
        connection.execute(
            select(operator_commands).where(
                operator_commands.c.actor
                == _actor(service.settings.tenant_id, profile['owner_id']),
                operator_commands.c.idempotency_key == key,
                operator_commands.c.kind == 'dstack_start',
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _ended_with_proof(node):
    """Existing committed positive evidence, never an inferred zero invoice."""
    if node is None or node['runtime_state'] != 'stopped':
        return False
    binding = node['payload'].get('dstack', {})
    if binding.get('state') != 'stopped':
        return False
    if binding.get('billing_state') == 'no_charge_confirmed':
        from .dstack_allocation_proof import validate_proof

        if validate_proof(binding.get('no_allocation_proof'), binding) is not None:
            return True
        return (
            binding.get('reason_code') == 'dstack_never_applied'
            and binding.get('apply_started') is not True
            and not binding.get('run_id')
            and not binding.get('provider_instance_id')
        )
    proof = binding.get('provider_removal_proof')
    return (
        isinstance(proof, dict)
        and proof.get('version') == 1
        and proof.get('complete') is True
        and proof.get('absent') is True
        and binding.get('dstack_status') in {'done', 'failed', 'terminated', 'aborted'}
        and proof.get('backend') == binding.get('backend')
        and proof.get('run_id') == binding.get('run_id')
        and bool(proof.get('provider_instance_id'))
        and proof['provider_instance_id'] == binding.get('provider_instance_id')
    )


def _ended_for_replacement(connection, node):
    if _ended_with_proof(node):
        return True
    if node is None or node['desired_state'] != 'stopped':
        return False
    from .capacity_watchdog import fenced_node_stop
    from .repository import instance_intents

    intent = (
        connection.execute(
            select(instance_intents).where(instance_intents.c.id == node['intent_id'])
        )
        .mappings()
        .one()
    )
    # Execution fencing permits replacement immediately. This does not alter
    # prior rental usage/reservations or manufacture physical removal evidence.
    return fenced_node_stop(connection, node, intent)


def _write(connection, row, payload, now):
    connection.execute(
        update(operator_commands)
        .where(operator_commands.c.id == row['id'])
        .values(
            payload=payload,
            state=payload['phase'],
            reason_code=payload.get('reason_code'),
            updated_at=now,
        )
    )


# A Modal host that is not ready this long after its reservation no longer
# holds its job back from Vast (owner: either provider covers a failed start).
MODAL_START_GRACE_SECONDS = 600


def modal_covered_jobs(connection, now):
    """Queued jobs a live Modal host was started for (#784): one provider per job.

    Modal admission runs first in the controller tick. Vast only starts for a job
    Modal refused, once its Modal host is going away or gone, or when the host
    has not become ready within MODAL_START_GRACE_SECONDS.
    """
    from .modal_capacity import OPERATION
    from .repository import instance_intents, scaler_receipts

    rows = connection.execute(
        select(
            scaler_receipts.c.facts,
            instance_intents.c.state,
            instance_intents.c.created_at,
        )
        .join(
            instance_intents,
            instance_intents.c.id == scaler_receipts.c.intent_id,
        )
        .where(
            scaler_receipts.c.operation == OPERATION,
            instance_intents.c.provider == 'modal',
            instance_intents.c.state.not_in(
                ('destroyed', 'not_created', 'draining', 'destroying')
            ),
        )
    ).all()
    return {
        facts.get('job_id')
        for facts, state, created in rows
        if isinstance(facts, dict)
        and (
            state in ('ready', 'busy')
            or isinstance(created, (int, float))
            and now - created < MODAL_START_GRACE_SECONDS
        )
    }


def vast_share(policy, queued, work, now):
    """With Modal first (owner 2026-10-29), Vast takes the queue only when it is
    long, or a job that has waited past the fallback; otherwise Modal serves it."""
    if not policy or len(queued) >= policy['vast_min_queue']:
        return work
    return [
        item
        for item in work
        if now - item[0]['created_at'] >= policy['vast_after_seconds']
    ]


def _claim(service):
    from .dstack_demand import (
        _nodes,
        demand_configuration,
        eligible_jobs,
        node_stage,
        profile_hash,
    )
    from .dstack_demand_policy import read_runtime

    tenant, now = service.settings.tenant_id, service.repo.clock()
    actor = 'dstack-demand:' + request_hash({'tenant': tenant})[:40]
    with service.repo.transaction() as connection:
        service.repo._lock_capacity(connection)
        if not read_runtime(connection, tenant)['enabled']:
            return _result('disabled'), None
        if demand_configuration(service.config, now=now) is None:
            return _result('blocked', 'authority_unavailable'), None
        covered = modal_covered_jobs(connection, now)
        queued = eligible_jobs(service, connection, now)
        work = [item for item in queued if item[0]['id'] not in covered]
        work = vast_share(getattr(service, 'modal_first', None), queued, work, now)
        if not work:
            return _result('idle'), None
        journals = list(
            connection.execute(
                select(operator_commands).where(
                    operator_commands.c.actor == actor,
                    operator_commands.c.kind == KIND,
                )
            ).mappings()
        )
        if any(
            (
                row['payload'].get('phase') in {'planning', 'starting'}
                or row['payload'].get('phase') == 'backoff'
                and row['payload'].get('reason_code')
                in {'allocation_uncertain', 'backoff'}
            )
            and row['payload'].get('next_decision_at', 0) > now
            for row in journals
        ):
            return _result('blocked', 'backoff'), None
        by_key = {row['idempotency_key']: dict(row) for row in journals}
        observing = None
        covered_groups = set()
        for job, profile in work:
            key = 'demand-job-' + job['id']
            identity = request_hash(
                {
                    'tenant': tenant,
                    'job_id': job['id'],
                    'profile_hash': profile_hash(service.config, profile),
                }
            )
            row = by_key.get(key)
            if row is not None and row['request_hash'] != identity:
                observing = _result('blocked', 'policy_changed')
                continue
            if row is None:
                payload = dict(
                    version=1,
                    tenant_id=tenant,
                    job_id=job['id'],
                    profile_id=profile['id'],
                    phase='backoff',
                    cycle=0,
                    preview_id=None,
                    next_decision_at=0,
                )
                row = dict(
                    id=str(uuid.uuid4()),
                    actor=actor,
                    idempotency_key=key,
                    request_hash=identity,
                    kind=KIND,
                    state='backoff',
                    payload=payload,
                    reason_code=None,
                    created_at=now,
                    updated_at=now,
                )
                connection.execute(insert(operator_commands).values(**row))
            payload = copy.deepcopy(row['payload'])
            if payload.get('version') != 1 or payload.get('phase') not in STATES:
                return _result('blocked', 'policy_changed'), None
            checked = download_journal(connection, tenant, job['id'])
            if checked is None:
                return _result('blocked', 'policy_changed'), None
            if payload['phase'] == 'blocked':
                observing = _result(
                    'blocked', payload.get('reason_code') or DOWNLOAD_RETRY_LIMIT
                )
                continue
            start_key = _start_key(identity, payload['cycle'])
            original = _start_command(connection, service, profile, start_key)
            if original is not None:
                node = (
                    connection.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == original['payload']['node_id']
                        )
                    )
                    .mappings()
                    .first()
                )
                if _ended_for_replacement(connection, node):
                    timed_out = (
                        node['payload'].get('stop_reason_code') == DOWNLOAD_TIMEBOX
                    )
                    replacements = payload.get('download_replacements', 0)
                    if timed_out and replacements >= MAX_DOWNLOAD_REPLACEMENTS:
                        payload.update(
                            phase='blocked',
                            reason_code=DOWNLOAD_RETRY_LIMIT,
                            next_decision_at=0,
                            prior_node_id=node['intent_id'],
                        )
                        _write(connection, row, payload, now)
                        observing = _result('blocked', DOWNLOAD_RETRY_LIMIT)
                        continue
                    payload.update(
                        cycle=payload['cycle'] + 1,
                        preview_id=None,
                        phase='backoff',
                        next_decision_at=now + _delay(job['id']),
                        reason_code='backoff',
                        prior_node_id=original['payload']['node_id'],
                        download_replacements=replacements + int(timed_out),
                    )
                    _write(connection, row, payload, now)
                    return _result('blocked', 'backoff'), None
                observing = _result('observing', None, original)
                continue
            if payload['next_decision_at'] > now:
                observing = _result('blocked', 'backoff')
                continue
            group = (
                profile['owner_id'],
                profile['pool'],
                profile['configuration_id'],
                profile['mode'],
            )
            if group in covered_groups:
                continue
            # Original commands and durable backoff are read before any live
            # provider GET. Only a genuinely new allocation needs capacity.
            usage = service.prelaunch_capacity_usage(connection)
            counted = {
                item['intent_id']: item
                for item in usage['observations']
                if item['counted']
            }
            uncertainty = {
                n['intent_id']
                for n in _nodes(connection, tenant)
                if n['intent_id'] in counted
                and not _ended_for_replacement(connection, n)
            }
            stages = [
                node_stage(n, now) or 'unknown'
                for n in _nodes(connection, tenant)
                if n['intent_id'] in uncertainty
            ]
            if 'unknown' in stages or any(
                item['reason_code']
                in {
                    'dstack_capacity_unconfirmed',
                    'dstack_terminal_provider_unconfirmed',
                }
                for identity, item in counted.items()
                if identity in uncertainty
            ):
                payload.update(
                    phase='backoff',
                    next_decision_at=now + _delay(job['id']),
                    reason_code='allocation_uncertain',
                )
                _write(connection, row, payload, now)
                return _result('blocked', 'allocation_uncertain'), None
            from .control import (
                check_registration_capacity,
                registration_capacity_projection,
            )
            from .repository import BudgetExceeded

            try:
                limits = service.prelaunch_capacity_limits(
                    connection, profile=profile, for_demand=True
                )
                check_registration_capacity(
                    registration_capacity_projection(usage), limits
                )
            except BudgetExceeded:
                return _result('blocked', 'capacity_limit'), None
            from .capacity_lookahead_runtime import enabled, evaluate, record_decision

            if enabled(service):
                snapshot = evaluate(service, connection, job, profile, work, usage, now)
                record_decision(
                    connection,
                    tenant,
                    job['id'],
                    snapshot,
                    now,
                    prior=payload.get('lookahead'),
                )
                payload['lookahead'] = snapshot
                if snapshot['decision']['start_count'] == 0:
                    covered_groups.add(group)
                    payload.update(
                        phase='backoff',
                        next_decision_at=now + _delay(job['id']),
                        reason_code='lookahead_covered',
                    )
                    _write(connection, row, payload, now)
                    observing = _result('blocked', 'lookahead_covered')
                    continue
            action = 'start' if payload.get('preview_id') else 'plan'
            payload.update(
                phase='starting' if action == 'start' else 'planning',
                token=str(uuid.uuid4()),
                next_decision_at=now + _delay(job['id']),
                reason_code=None,
            )
            _write(connection, row, payload, now)
            return None, dict(
                row_id=row['id'],
                token=payload['token'],
                job_id=job['id'],
                profile=profile,
                start_key=start_key,
                preview_id=payload.get('preview_id'),
                action=action,
            )
        return observing or _result('idle'), None


def _finish(service, claim, **changes):
    with service.repo.transaction() as connection:
        service.repo._lock_capacity(connection)
        row = (
            connection.execute(
                select(operator_commands)
                .where(operator_commands.c.id == claim['row_id'])
                .with_for_update()
            )
            .mappings()
            .one()
        )
        payload = copy.deepcopy(row['payload'])
        if payload.get('token') != claim['token']:
            return False
        payload.update(changes)
        _write(connection, row, payload, service.repo.clock())
        return True


def _reason(error):
    code = getattr(error, 'code', None) or (error.args[0] if error.args else '')
    code = code if isinstance(code, str) else ''
    if code in {'tenant_daily_limit_reached', 'tenant_daily_usage_unconfirmed'}:
        return code
    if code in {'dstack_capacity_limit_exceeded', 'global_capacity_exceeded'}:
        return 'capacity_limit'
    if 'budget' in code or 'spend' in code or 'limit_reached' in code:
        return 'budget_limit'
    if 'authority' in code or 'disabled' in code or 'demand_policy' in code:
        return 'authority_unavailable'
    if 'changed' in code or 'expired' in code:
        return 'policy_changed'
    return (
        'backoff' if isinstance(error, DstackReadRateLimited) else 'offer_unavailable'
    )


LADDER_STEP_SECONDS = 180
LADDER_STEP_FRACTION = 0.15


def ladder_price(service, profile, job_id):
    """Requested hourly price for a waiting job, or None to keep the profile price.

    A fixed cap starved the first paid production job while 5090 offers sat at
    0.77-1.0 USD/h (h3-studio#798 B13). Each 3 minutes a job waits adds 15% of
    the profile price, never above the lowest of the profile price ceiling, the
    admin mode ceiling and the admin hourly maximum. Read-only; it only shapes
    the preview body that the original guarded start path validates again.
    """
    from .operator_prices import OperatorPrices, mode_price_ceiling
    from .repository import jobs

    with service.repo.engine.connect() as connection:
        created = connection.execute(
            select(jobs.c.created_at).where(jobs.c.id == job_id)
        ).scalar()
        price, _, _ = OperatorPrices(service.repo, service.settings).read(
            connection, profile
        )
        limits = service._admin_limits(connection)
    base, ceiling = price["max_price_microusd"], price["price_ceiling_microusd"]
    caps = [ceiling, limits["value"]["max_hourly_price_microusd"]]
    if limits["adopted"] or service.policy.get("demand_start"):
        caps.append(mode_price_ceiling(profile, limits))
    if not all(type(v) is int and v > 0 for v in [base, *caps]) or type(
        created
    ) not in (int, float):
        return None
    steps = max(0, int((service.repo.clock() - created) // LADDER_STEP_SECONDS))
    target = min(int(base * (1 + LADDER_STEP_FRACTION * steps)), *caps)
    return target if target > base else None


def start_once(service):
    """At most one original allocation; unknown calls keep their frozen input."""
    result, claim = _claim(service)
    if claim is None:
        return result
    profile, job_id = claim['profile'], claim['job_id']
    try:
        if claim['action'] == 'plan':
            from .capacity_lookahead_runtime import idle_seconds

            with service.repo.engine.connect() as connection:
                idle = idle_seconds(service, connection, profile)
            body = dict(
                profile_id=profile['id'],
                keep_on='idle',
                idle_shutdown_seconds=idle,
            )
            task = service.policy['demand_start'].get('test_task_id')
            if task is not None:
                body['test_task_id'] = task
            raised = ladder_price(service, profile, job_id)
            if raised is not None:
                body['max_price_microusd'] = raised
            preview = service._preview_for_owner(
                profile['owner_id'], body, demand_job_id=job_id
            )
            claim['preview_id'] = preview['preview_id']
            if not _finish(
                service, claim, preview_id=claim['preview_id'], phase='starting'
            ):
                return _result('observing', 'backoff')
            # From this point an exception is a start outcome, even when this
            # decision also planned the preview. Never replace its input.
            claim['action'] = 'start'
        result = service._start_for_owner(
            profile['owner_id'],
            'service:dstack-demand',
            {'preview_id': claim['preview_id']},
            claim['start_key'],
            demand_job_id=job_id,
        )
        _finish(service, claim, phase='observing', reason_code=None)
        return dict(
            state='accepted', command_id=result['command_id'], node_id=result['node_id']
        )
    except Exception as error:
        # Read the original start first: an exception may follow its committed
        # intent or an accepted provider call. Never choose a fresh input then.
        with service.repo.transaction() as connection:
            service.repo._lock_capacity(connection)
            original = _start_command(connection, service, profile, claim['start_key'])
        if original is not None:
            _finish(
                service, claim, phase='observing', reason_code='allocation_uncertain'
            )
            return _result('observing', 'allocation_uncertain', original)
        reason = _reason(error)
        changes = dict(
            phase='backoff' if claim['action'] == 'plan' else 'starting',
            reason_code=reason,
            next_decision_at=service.repo.clock() + _delay(job_id, error),
        )
        # A completed guarded start rejection is positive evidence that this
        # exact preview did not commit. Unknown exceptions keep the preview.
        from .operator_capacity import OperatorError
        from .repository import BudgetExceeded

        if isinstance(error, (OperatorError, BudgetExceeded)):
            changes.update(phase='backoff', preview_id=None)
        _finish(service, claim, **changes)
        return _result(
            'blocked',
            reason
            if claim['action'] == 'plan'
            or isinstance(error, (OperatorError, BudgetExceeded))
            else 'allocation_uncertain',
        )
