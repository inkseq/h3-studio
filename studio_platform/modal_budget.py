"""Modal attribution in the original ledgers, never a second cap.

Bind inside the original capacity/reservation transaction before provider I/O.
Staging hosts (guard v2) use the shared test ledger and the same
dstack_test_budget projection as Vast. Production hosts (guard v3, #688) are
funded by a production budget account and the supplier-spending day.
An exited process remains an obligation until an actual invoice is adopted.
"""

import math
import uuid

from sqlalchemy import insert, select

from .dstack_test_budget import ACCOUNT, POOLS, finite, guard_limit, validate_guard
from .modal_host import HostSpec
from .repository import (
    BudgetExceeded,
    budget_accounts,
    budget_reservations,
    instance_intents,
    scaler_receipts,
)

OPERATION = 'modal_test_budget_bound'
PRODUCTION_FIELDS = frozenset(
    {'version', 'budget_account_id', 'max_runtime_seconds', 'stop_margin_seconds'}
)


def validate_production_guard(value):
    if (
        not isinstance(value, dict)
        or set(value) != PRODUCTION_FIELDS
        or value['version'] != 3
        or not isinstance(value['budget_account_id'], str)
        or not value['budget_account_id']
        or value['budget_account_id'] == ACCOUNT
        or type(value['max_runtime_seconds']) is not int
        or not 60 <= value['max_runtime_seconds'] <= 7200
        or type(value['stop_margin_seconds']) is not int
        or not 300 <= value['stop_margin_seconds'] <= 600
    ):
        raise ValueError('modal_production_guard_invalid')
    return value


def production_guard(account, *, max_runtime):
    return validate_production_guard(
        {
            'version': 3,
            'budget_account_id': account,
            'max_runtime_seconds': max_runtime,
            'stop_margin_seconds': 300,
        }
    )


def _guard(value):
    if isinstance(value, dict) and value.get('version') == 3:
        return validate_production_guard(value)
    return validate_guard(value)


def worker_reason(connection, worker, now, expected_runtime_s):
    """Original readiness/claim/late-submit gate; never used to replay recovery."""
    from .budget_policy import effective_budget_limit

    try:
        rows = list(
            connection.execute(
                select(instance_intents).where(
                    instance_intents.c.provider == 'modal',
                    instance_intents.c.provider_instance_id == worker['instance_id'],
                )
            ).mappings()
        )
        require(len(rows) == 1 and finite(now))
        intent = rows[0]
        if intent['state'] not in {'starting', 'ready', 'busy'}:
            return 'worker_capacity_stopping'
        _, value = binding(connection, intent['id'])
        enforce(connection, intent['id'], value['spec'], now)
        account = (
            connection.execute(
                select(budget_accounts).where(
                    budget_accounts.c.id == value['guard']['budget_account_id'],
                )
            )
            .mappings()
            .one_or_none()
        )
        require(
            account is not None
            and account['reserved_microusd'] >= intent['reserved_cost_microusd']
        )
        require(
            account['spent_microusd'] + account['reserved_microusd']
            <= effective_budget_limit(account)
        )
        if now >= intent['hard_deadline']:
            return 'worker_capacity_stopping'
        if expected_runtime_s is not None:
            if not finite(expected_runtime_s) or expected_runtime_s <= 0:
                return 'job_runtime_unconfirmed'
            if now + expected_runtime_s + 120 >= intent['hard_deadline']:
                return 'job_exceeds_worker_window'
    except (BudgetExceeded, KeyError, TypeError, ValueError):
        return 'modal_test_budget_unconfirmed'
    return None


def require(ok):
    if not ok:
        raise BudgetExceeded('modal_test_budget_unconfirmed')


def binding(connection, identity):
    intent = (
        connection.execute(
            select(instance_intents).where(instance_intents.c.id == identity)
        )
        .mappings()
        .one_or_none()
    )
    records = list(
        connection.execute(
            select(scaler_receipts).where(
                scaler_receipts.c.intent_id == identity,
                scaler_receipts.c.operation == OPERATION,
            )
        ).mappings()
    )
    require(intent is not None and len(records) == 1)
    value = records[0]['facts']
    require(isinstance(value, dict) and set(value) == {'guard', 'spec', 'request_hash'})
    try:
        guard = _guard(value['guard'])
        spec = HostSpec(**value['spec'])
    except (ValueError, TypeError, KeyError):
        raise BudgetExceeded('modal_test_budget_unconfirmed') from None
    staging = guard['version'] == 2
    require(
        (
            staging
            and guard_limit(guard) <= 50_000_000
            and intent['pool'] in POOLS
            or guard['version'] == 3
            and intent['pool'] not in POOLS
        )
        and intent['provider'] == 'modal'
        and intent['physical_gpus'] == intent['slots'] == 1
        and intent['request_hash'] == value['request_hash']
    )
    created, deadline = intent['created_at'], intent['hard_deadline']
    require(
        finite(created)
        and finite(deadline)
        and 0 < deadline - created <= min(guard['max_runtime_seconds'], 7200)
    )
    amount = intent['reserved_cost_microusd']
    required = math.ceil(
        spec.max_hourly_microusd
        * (deadline - created + guard['stop_margin_seconds'])
        / 3600
    )
    require(type(amount) is int and amount >= required)
    return intent, value


def bind_test_guard(repo, connection, identity, guard, spec):
    """One immutable receipt in the SAME transaction as the original reservation."""
    repo._lock_capacity(connection)
    intent = repo._locked(
        connection, select(instance_intents).where(instance_intents.c.id == identity)
    )
    require(intent is not None)
    expected = {'guard': guard, 'spec': spec, 'request_hash': intent['request_hash']}
    existing = list(
        connection.execute(
            select(scaler_receipts).where(
                scaler_receipts.c.intent_id == identity,
                scaler_receipts.c.operation == OPERATION,
            )
        ).mappings()
    )
    if existing:
        require(len(existing) == 1 and existing[0]['facts'] == expected)
    else:
        require(intent['state'] == 'reserved')
        connection.execute(
            insert(scaler_receipts).values(
                id=str(uuid.uuid4()),
                intent_id=identity,
                operation=OPERATION,
                observed_at=repo.clock(),
                facts=expected,
            )
        )
    enforce(connection, identity, spec, repo.clock())


def enforce(connection, identity, spec, now):
    """Called again inside ModalHosts' original pre-create capacity lock."""
    from .dstack_test_budget import projected_cost

    intent, value = binding(connection, identity)
    require(value['spec'] == spec)
    account = value['guard']['budget_account_id']
    rows = list(
        connection.execute(
            select(budget_reservations).where(
                budget_reservations.c.account_id == account,
                budget_reservations.c.reference_type == 'instance',
                budget_reservations.c.reference_id == identity,
            )
        ).mappings()
    )
    require(
        len(rows) == 1
        and rows[0]['state'] == 'reserved'
        and rows[0]['amount_microusd'] == intent['reserved_cost_microusd']
    )
    if value['guard']['version'] == 2:
        require(
            projected_cost(connection, value['guard'], now)
            <= guard_limit(value['guard'])
        )


def projected_reservation(connection, row, guard, now):
    """Count only the shared-account layer; malformed attribution fails closed."""
    require(row['reference_type'] == 'instance' and row['account_id'] == ACCOUNT)
    intent, value = binding(connection, row['reference_id'])
    require(
        guard['version'] == 2
        and finite(now)
        and finite(row['created_at'])
        and row['created_at'] <= now
    )
    if value['guard']['task_id'] != guard['task_id']:
        return 0
    amount, actual = row['amount_microusd'], row['actual_cost_microusd']
    require(type(amount) is int and amount == intent['reserved_cost_microusd'])
    if row['state'] in {'settled', 'released'}:
        require(
            type(actual) is int
            and actual >= 0
            and finite(row['settled_at'])
            and row['created_at'] <= row['settled_at'] <= now
        )
        return actual
    require(row['state'] in {'reserved', 'invoice_pending'} and actual is None)
    if intent['state'] == 'destroyed':
        bound = exit_bound(connection, intent, value, now)
        if bound is not None:
            return bound
    elapsed = now - intent['created_at'] + value['guard']['stop_margin_seconds']
    return max(amount, math.ceil(value['spec']['max_hourly_microusd'] * elapsed / 3600))


def exit_bound(connection, intent, value, now):
    """Upper cost bound from the original provider-exit receipt, else None.

    Only that receipt bounds the lifetime. A stop acknowledgment or edited
    intent status is not removal evidence.
    """
    exits = []
    for receipt in connection.execute(
        select(scaler_receipts).where(
            scaler_receipts.c.intent_id == intent['id'],
            scaler_receipts.c.operation == 'observed',
        )
    ).mappings():
        facts, stamp = receipt['facts'], receipt['observed_at']
        if (
            isinstance(facts, dict)
            and facts.get('id') == intent['provider_instance_id']
            and type(facts.get('exit_code')) is int
            and finite(stamp)
            and intent['created_at'] <= stamp <= now
        ):
            exits.append(stamp)
    if not exits:
        return None
    elapsed = min(exits) - intent['created_at'] + value['guard']['stop_margin_seconds']
    return math.ceil(value['spec']['max_hourly_microusd'] * elapsed / 3600)


def settle_destroyed(repo, identity):
    """Settle a destroyed host's held test budget at its exit-receipt bound.

    Modal has no per-sandbox invoice here, so the conservative bound becomes
    the actual. Without an exit receipt the hold stays for manual review.
    """
    with repo.transaction() as c:
        repo._lock_capacity(c)
        intent = repo._locked(
            c, select(instance_intents).where(instance_intents.c.id == identity)
        )
        if intent is None or intent['state'] != 'destroyed':
            return None
        held = c.execute(
            select(budget_reservations.c.id).where(
                budget_reservations.c.reference_type == 'instance',
                budget_reservations.c.reference_id == identity,
                budget_reservations.c.state == 'reserved',
            )
        ).first()
        if held is None:
            return None
        _, value = binding(c, identity)
        cost = exit_bound(c, intent, value, repo.clock())
        if cost is None:
            return None
        repo.settle_instance_cost(identity, actual_cost_microusd=cost, connection=c)
        return cost
