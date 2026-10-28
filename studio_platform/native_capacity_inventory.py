"""Fresh native inventory over the original global physical-resource overlay.

An original Modal process exit removes only its exact physical device from this
read-only projection. The worker, attempts, receipts and money remain intact.
"""

from collections import Counter
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
    from .modal_runtime import PROFILE
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
        manifest = engine_manifest(PROFILE, selection['mode'])
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
            profile_output_delivery(PROFILE),
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
        for identity in usage['active_instance_ids']
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


def prelaunch_usage(service, connection):
    """Keep provider reads and registration's physical exclusion rules identical."""
    from .dstack_operator import BACKEND_MARKER

    nodes = [
        dict(node)
        for node in connection.execute(select(operator_nodes)).mappings()
        if node['payload'].get('capacity_backend') == BACKEND_MARKER
    ]
    intents = {
        row['id']: dict(row)
        for row in connection.execute(select(instance_intents)).mappings()
    }
    usage, observed_runs = service._observed_registration_usage(nodes)
    return resource_overlay(service, connection, nodes, intents, usage, observed_runs)
