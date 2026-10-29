"""Original Modal consumer liveness, never a fabricated dstack node.

The existing receipt journal stores bounded finite-host heartbeat observations.
The capacity lock serializes their sequence; stale withdrawal cannot erase a
new consumer or a newer worker fence. No observation starts or replays work.
"""

import math
import re
import uuid

from sqlalchemy import insert, select

from .modal_budget import binding, require
from .modal_capacity import selection
from .modal_runtime import selected_profile
from .repository import BudgetExceeded, scaler_receipts
from .runtime_catalog import engine_manifest, profile_output_delivery

OPERATION = 'modal_consumer_observed'
FIELDS = {
    'sequence',
    'ready',
    'worker_id',
    'worker_fence',
    'instance_id',
    'runtime_incarnation',
    'observed_at',
}


def _original(c, worker):
    identity = worker['id'].removeprefix('modal-')
    require(re.fullmatch(r'[0-9a-f]{32}', identity) is not None)
    identity = str(uuid.UUID(identity))
    intent, _ = binding(c, identity)
    original = selection(c, identity)
    runtime = selected_profile(original)
    manifest = engine_manifest(runtime, original['mode'])
    spec = worker['spec']
    require(worker['provider'] == spec['provider'] == 'modal')
    require(worker['id'] == 'modal-' + identity.replace('-', ''))
    require(worker['pool'] == spec['pool'] == intent['pool'])
    require(
        worker['instance_id'] == spec['instance_id'] == intent['provider_instance_id']
    )
    require(
        spec['backend'] == 'wangp-worker' and spec['dispatch_backend'] == 'hatchet-v1'
    )
    require(spec['physical_gpu_ids'] == ['gpu0'])
    require(spec['recipe_ids'] == [manifest.document['generation_recipe_id']])
    require(spec['model_id'] == manifest.document['model_id'])
    require(spec['configuration_id'] == original['configuration_id'])
    require(
        spec['engine_manifest_digest'] == original['manifest_digest'] == manifest.digest
    )
    require(spec['output_delivery'] == profile_output_delivery(runtime))
    ready = list(
        c.execute(
            select(scaler_receipts.c.facts).where(
                scaler_receipts.c.intent_id == identity,
                scaler_receipts.c.operation == 'modal_runtime_bound',
            )
        ).scalars()
    )
    require(len(ready) == 1)
    require(ready[0]['instance_id'] == intent['provider_instance_id'])
    require(ready[0]['manifest_digest'] == manifest.digest)
    require(re.fullmatch(r'[0-9a-f]{32}', ready[0]['runtime_incarnation']) is not None)
    return intent, ready[0]


def _latest(c, identity):
    rows = list(
        c.execute(
            select(scaler_receipts.c.facts).where(
                scaler_receipts.c.intent_id == identity,
                scaler_receipts.c.operation == OPERATION,
            )
        ).scalars()
    )
    require(
        all(
            isinstance(row, dict)
            and set(row) == FIELDS
            and type(row['sequence']) is int
            and row['sequence'] > 0
            and type(row['ready']) is bool
            and type(row['worker_fence']) is int
            and row['worker_fence'] >= 0
            and type(row['observed_at']) in (int, float)
            and math.isfinite(row['observed_at'])
            for row in rows
        )
    )
    require(len({row['sequence'] for row in rows}) == len(rows))
    return max(rows, key=lambda row: row['sequence']) if rows else None


def _append(repo, c, identity, facts, prior):
    facts = {**facts, 'sequence': prior['sequence'] + 1 if prior else 1}
    c.execute(
        insert(scaler_receipts).values(
            id=str(uuid.uuid4()),
            intent_id=identity,
            operation=OPERATION,
            observed_at=repo.clock(),
            facts=facts,
        )
    )
    return facts


def record(repo, c, worker, backend):
    intent, ready = _original(c, worker)
    incarnation = getattr(backend, 'expected_incarnation', None)
    require(backend.slot_key == 'modal-' + intent['id'])
    require(incarnation == ready['runtime_incarnation'])
    now = repo.clock()
    require(type(now) in (int, float) and math.isfinite(now))
    observation = {
        'ready': worker['state'] not in {'unknown', 'retired'}
        and worker['expires_at'] > now
        and not worker['drain_requested']
        and intent['state'] in {'starting', 'ready', 'busy'}
        and intent['hard_deadline'] > now,
        'worker_id': worker['id'],
        'worker_fence': worker['fence'],
        'instance_id': worker['instance_id'],
        'runtime_incarnation': incarnation,
        'observed_at': now,
    }
    return _append(repo, c, intent['id'], observation, _latest(c, intent['id']))


def withdraw(repo, c, worker, backend, observation):
    intent, ready = _original(c, worker)
    if (
        backend.slot_key != 'modal-' + intent['id']
        or getattr(backend, 'expected_incarnation', None)
        != ready['runtime_incarnation']
        or _latest(c, intent['id']) != observation
        or worker['fence'] != observation['worker_fence']
    ):
        return
    _append(repo, c, intent['id'], {**observation, 'ready': False}, observation)


def consumer_reason(c, worker, now):
    try:
        intent, ready = _original(c, worker)
        consumer = _latest(c, intent['id']) or {}
        observed = consumer.get('observed_at')
        require(consumer.get('ready') is True)
        require(consumer.get('worker_id') == worker['id'])
        require(consumer.get('worker_fence') == worker['fence'])
        require(consumer.get('instance_id') == worker['instance_id'])
        require(consumer.get('runtime_incarnation') == ready['runtime_incarnation'])
        require(
            type(observed) in (int, float)
            and math.isfinite(observed)
            and 0 <= now - observed <= 60
        )
    except (BudgetExceeded, ValueError, KeyError, TypeError):
        return 'modal_queue_consumer_unconfirmed'
    return None
