"""Fresh native inventory over the original global physical-resource overlay.

An original Modal process exit removes only its exact physical device from this
read-only projection. The worker, attempts, receipts and money remain intact.
"""

from collections import Counter
import copy
import logging
import re

from sqlalchemy import select

from .operator_capacity import operator_nodes
from .repository import (
    BudgetExceeded,
    instance_intents,
    registered_devices,
    registered_workers,
    request_hash,
    scaler_actions,
    scaler_receipts,
)


def _exited_modal_worker(connection, intent, now):
    """Return the exact original worker only with immutable positive exit proof."""
    from .control import WorkerSpec, worker_spec_payload
    from .dstack_test_budget import finite
    from .modal_budget import binding, require
    from .modal_runtime import selected_profile
    from .runtime_catalog import engine_manifest, profile_output_delivery

    try:
        require(intent['provider'] == 'modal' and intent['state'] == 'destroyed')
        original, bound = binding(connection, intent['id'])
        require(original['provider_instance_id'] == intent['provider_instance_id'])
        action = (
            connection.execute(
                select(scaler_actions).where(scaler_actions.c.intent_id == intent['id'])
            )
            .mappings()
            .one_or_none()
        )
        require(action is not None and action['launch_spec'] == bound['spec'])
        started = action['create_started_at']
        require(
            finite(now) and finite(started) and intent['created_at'] <= started <= now
        )
        records = {}
        for operation in ('modal_demand_bound', 'modal_runtime_bound'):
            rows = list(
                connection.execute(
                    select(scaler_receipts).where(
                        scaler_receipts.c.intent_id == intent['id'],
                        scaler_receipts.c.operation == operation,
                    )
                ).mappings()
            )
            require(len(rows) == 1 and isinstance(rows[0]['facts'], dict))
            records[operation] = rows[0]
        selection = records['modal_demand_bound']['facts']
        ready = records['modal_runtime_bound']
        facts = ready['facts']
        manifest = engine_manifest(selected_profile(selection), selection['mode'])
        require(
            selection.get('capacity_backend') == 'modal-v1'
            and selection['manifest_digest'] == manifest.digest
            and facts['manifest_digest'] == manifest.digest
            and facts['instance_id'] == intent['provider_instance_id']
            and isinstance(facts.get('runtime_incarnation'), str)
            and re.fullmatch(r'[0-9a-f]{32}', facts['runtime_incarnation']) is not None
            and finite(ready['observed_at'])
            and started <= ready['observed_at'] <= now
        )
        spec = WorkerSpec(
            'modal-' + intent['id'].replace('-', ''),
            intent['pool'],
            'modal',
            intent['provider_instance_id'],
            ('gpu0',),
            (manifest.document['generation_recipe_id'],),
            manifest.document['model_id'],
            selection['configuration_id'],
            'wangp-worker',
            manifest.digest,
            profile_output_delivery(selected_profile(selection)),
            'hatchet-v1',
        )
        worker = (
            connection.execute(
                select(registered_workers).where(
                    registered_workers.c.id == spec.worker_id
                )
            )
            .mappings()
            .one_or_none()
        )
        expected = worker_spec_payload(spec)
        require(
            worker is not None
            and worker['provider'] == spec.provider
            and worker['instance_id'] == spec.instance_id
            and worker['pool'] == spec.pool
            and worker['spec'] == expected
            and worker['spec_hash'] == request_hash(expected)
        )
        exits = connection.execute(
            select(scaler_receipts).where(
                scaler_receipts.c.intent_id == intent['id'],
                scaler_receipts.c.operation == 'observed',
            )
        ).mappings()
        require(
            any(
                isinstance(row['facts'], dict)
                and set(row['facts']) == {'id', 'exit_code'}
                and row['facts']['id'] == spec.instance_id
                and type(row['facts']['exit_code']) is int
                and finite(row['observed_at'])
                and ready['observed_at'] <= row['observed_at'] <= now
                for row in exits
            )
        )
        return spec.worker_id
    except (BudgetExceeded, KeyError, TypeError, ValueError):
        # Missing, edited or uncertain proof retains physical occupancy.
        return None


def _exclude_exited_modal_devices(connection, intents, usage, now):
    """Remove no row and release no money; unrelated device bindings still count."""
    devices = list(
        connection.execute(
            select(registered_devices).where(
                registered_devices.c.provider == 'modal',
                registered_devices.c.state != 'released',
            )
        ).mappings()
    )
    totals = Counter((row['provider'], row['instance_id']) for row in devices)
    removable = Counter()
    active_keys = {
        (row['provider'], row['provider_instance_id'])
        for identity in usage.get(
            'all_resource_intent_ids', usage['active_instance_ids']
        )
        if (row := intents.get(identity)) is not None
    }
    for intent in intents.values():
        if intent['provider'] != 'modal' or intent['state'] != 'destroyed':
            continue
        key = ('modal', intent['provider_instance_id'])
        if key in active_keys:
            continue
        worker_id = _exited_modal_worker(connection, intent, now)
        if worker_id is not None:
            removable[key] += sum(
                row['instance_id'] == intent['provider_instance_id']
                and row['worker_id'] == worker_id
                and row['gpu_id'] == 'gpu0'
                for row in devices
            )
    resources = dict(usage['resources'])
    for key, count in removable.items():
        if resources.get(key) != totals[key] or count > totals[key]:
            continue
        remaining = totals[key] - count
        if remaining:
            resources[key] = remaining
        else:
            resources.pop(key)
    return {
        **usage,
        'resources': resources,
        'instances': len(resources),
        'physical_gpus': sum(resources.values()),
    }


def resource_overlay(service, connection, nodes, intents, usage, observed_runs):
    """Use this call's existing native reads for every physical capacity check."""
    overlay = {
        **usage,
        **service._registration_resource_overlay(
            connection, nodes, intents, usage, observed_runs
        ),
    }
    return _exclude_exited_modal_devices(
        connection, intents, overlay, service.repo.clock()
    )


_logger = logging.getLogger(__name__)
PREREAD_CHANGED = 'dstack_capacity_read_changed'
# Liveness proofs that workers write under the capacity lock while they claim
# and submit. They are not capacity facts, so they never void a pre-read.
WORKER_KEYS = frozenset({'consumer_observation'})


def _dstack_nodes(connection):
    from .dstack_operator import BACKEND_MARKER

    return [
        dict(node)
        for node in connection.execute(select(operator_nodes)).mappings()
        if node['payload'].get('capacity_backend') == BACKEND_MARKER
    ]


def committed_facts(nodes):
    """Digest of every committed node fact a live capacity read depends on."""
    return request_hash(
        [
            [
                node['intent_id'],
                node['desired_state'],
                {
                    key: value
                    for key, value in node['payload'].items()
                    if key != 'dstack'
                },
                {
                    key: value
                    for key, value in (node['payload'].get('dstack') or {}).items()
                    if key not in WORKER_KEYS
                },
            ]
            for node in sorted(nodes, key=lambda node: node['intent_id'])
        ]
    )


def capacity_preread(service):
    """Provider reads for the capacity overlay, taken before the capacity lock.

    Holding the global lock across dstack and provider GETs (30 s timeouts,
    retries) made every worker claim and submit check wait behind them
    (#1148). The node rows are read and released first; no connection is held
    while the provider answers. Under the lock, ``prelaunch_usage`` accepts
    this read only if the same committed facts still hold.
    """
    with service.repo.engine.connect() as connection:
        nodes = _dstack_nodes(connection)
    usage, observed_runs = service._observed_registration_usage(nodes)
    return {
        'facts': committed_facts(nodes),
        'usage': usage,
        'observed_runs': observed_runs,
        'read_at': service.repo.clock(),
    }


def prelaunch_usage(service, connection, preread=None, *, reread=False):
    """Keep provider reads and registration's physical exclusion rules identical.

    With ``preread``, recheck only cheap committed facts under the caller's
    capacity lock: the same dstack node rows (any start, stop, observation or
    journal change voids it) and a read no older than the observation freshness
    the live read itself enforces. See ``observed_usage`` for a changed fact.
    """
    nodes = _dstack_nodes(connection)
    intents = {
        row['id']: dict(row)
        for row in connection.execute(select(instance_intents)).mappings()
    }
    usage, observed_runs = observed_usage(service, nodes, preread, reread=reread)
    return resource_overlay(service, connection, nodes, intents, usage, observed_runs)


def observed_usage(service, nodes, preread=None, *, reread=False):
    """The live usage for these locked node rows: read now, or the pre-read.

    A pre-read counts only for the same committed facts and while no older
    than the observation freshness the live read itself enforces. When a fact
    changed, a demand decision fails closed with ``dstack_capacity_read_changed``
    (nothing starts; the next tick reads again). A start or registration that
    must answer now passes ``reread``: it reads live under the lock, exactly
    as before #1148, which is rare because node facts change rarely.
    """
    if preread is None:
        return service._observed_registration_usage(nodes)
    age = service.repo.clock() - preread['read_at']
    if preread['facts'] != committed_facts(nodes) or not (
        0 <= age <= service.policy['observation_fresh_seconds']
    ):
        if reread:
            _logger.info('dstack_capacity_preread_changed action=locked_reread')
            return service._observed_registration_usage(nodes)
        raise BudgetExceeded(PREREAD_CHANGED)
    return copy.deepcopy(preread['usage']), preread['observed_runs']
