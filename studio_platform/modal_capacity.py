"""Finite Modal admissions over the adopted policy and original physical ledger.

Native removal witnesses affect capacity only. Original funding, invoices and
accepted jobs remain unchanged; unproved resources cannot authorize new spend.
"""

from dataclasses import asdict

from sqlalchemy import select

from .capacity_settings import reservation_provenance, validate_snapshot
from .control import check_registration_capacity, registration_capacity_projection
from .modal_budget import binding, enforce, require
from .repository import Conflict, Scope, instance_intents, outbox, scaler_receipts

BACKEND = 'modal-v1'
OPERATION = 'modal_demand_bound'
CONFIRMED = {
    'dstack_capacity_active',
    'dstack_never_applied',
    'dstack_terminal_no_allocation',
    'dstack_terminal_provider_absent',
    'dstack_terminal_provider_accounted',
}


def selection(c, identity):
    rows = list(
        c.execute(
            select(scaler_receipts.c.facts).where(
                scaler_receipts.c.intent_id == identity,
                scaler_receipts.c.operation == OPERATION,
            )
        ).scalars()
    )
    require(len(rows) == 1)
    return rows[0]


def usage(service, c):
    """Reuse #682's fresh native/global overlay, retaining unproved liabilities."""
    value = service.prelaunch_capacity_usage(c)
    registration_capacity_projection(value)
    observations = value.get('observations')
    active = value.get('all_resource_intent_ids', value.get('active_instance_ids'))
    require(isinstance(observations, list) and isinstance(active, (tuple, list)))
    require(
        all(
            isinstance(o, dict)
            and o.get('reason_code') in CONFIRMED
            and type(o.get('counted')) is bool
            for o in observations
        )
    )
    require(value.get('unconfirmed_allocation_ids') == ())
    priced = {o['intent_id'] for o in observations if o['counted']}
    hourly = value.get('hourly_cost_microusd')
    require(type(hourly) is int and hourly >= 0)
    represented = set()
    for intent in c.execute(
        select(instance_intents).where(
            instance_intents.c.id.in_(active),
        )
    ).mappings():
        key = (
            intent['provider'],
            intent['provider_instance_id'] or 'intent:' + intent['id'],
        )
        if intent['provider'] == 'modal':
            _, original = binding(c, intent['id'])
            hourly += original['spec']['max_hourly_microusd']
            priced.add(intent['id'])
        represented.add(key)
    # Legacy/unrelated resources have no proved hourly bound. Retain their
    # physical count and refuse a new paid admission, rather than price as zero.
    require(set(active).issubset(priced))
    native_resources = value.get('native_resources')
    require(isinstance(native_resources, (tuple, list)))
    require(set(value['resources']).issubset(represented | set(native_resources)))
    return {**value, 'active_instance_ids': active, 'hourly_cost_microusd': hourly}


def admission(service, c, profile, spec, *, identity=None, expected_snapshot=None):
    from .supplier_spending import admit

    stored = service._admin_limits(c)
    snapshot = admit(
        c,
        service.settings.tenant_id,
        service.repo.clock(),
        reference_type='instance',
        test_task=True,
        policy=stored,
    )
    if identity is not None and snapshot != expected_snapshot:
        raise Conflict('capacity_policy_snapshot_changed')
    limits = service.prelaunch_capacity_limits(c, profile=profile, for_demand=True)
    value = usage(service, c)
    key = ('modal', 'intent:' + identity) if identity is not None else None
    if identity is not None:
        require(value['resources'].get(key) == 1)
    check_registration_capacity(
        registration_capacity_projection(value, key=key), limits
    )
    current = stored['value']
    price = spec.max_hourly_microusd
    mode = profile['mode'] if profile['mode'] in {'fl', 'ref'} else 'other'
    require(price <= current['max_hourly_price_microusd'])
    require(price <= current['mode_hourly_ceiling_microusd'][mode])
    require(
        value['hourly_cost_microusd'] + (price if identity is None else 0)
        <= current['fleet_hourly_limit_microusd']
    )
    return snapshot, value


def precreate(service, hosts, c, intent):
    """Repeat current policy/physical limits after reserve, before sole create."""
    original = selection(c, intent['id'])
    require(original.get('capacity_backend') == BACKEND)
    profile = service.profiles.get(original['demand']['profile_id'])
    require(profile is not None)
    admission(
        service,
        c,
        profile,
        hosts.spec,
        identity=intent['id'],
        expected_snapshot=original['capacity_policy_snapshot'],
    )


def registration_usage(service, hosts, c, spec):
    """Fulfill one original finite admission, never add an extra allocation."""
    rows = list(
        c.execute(
            select(instance_intents).where(
                instance_intents.c.provider == 'modal',
                instance_intents.c.provider_instance_id == spec.instance_id,
                instance_intents.c.pool == spec.pool,
            )
        ).mappings()
    )
    require(len(rows) == 1)
    intent, bound = binding(c, rows[0]['id'])
    identity = intent['id']
    original = selection(c, identity)
    require(intent['state'] in {'starting', 'ready', 'busy'})
    require(spec.worker_id == 'modal-' + identity.replace('-', ''))
    require(spec.physical_gpu_ids == ('gpu0',))
    require(spec.backend == 'wangp-worker' and spec.dispatch_backend == 'hatchet-v1')
    require(original['configuration_id'] == spec.configuration_id)
    require(original['manifest_digest'] == spec.engine_manifest_digest)
    from .modal_runtime import PROFILE
    from .runtime_catalog import engine_manifest, profile_output_delivery

    manifest = engine_manifest(PROFILE, original['mode'])
    require(spec.model_id == manifest.document['model_id'])
    require(spec.recipe_ids == (manifest.document['generation_recipe_id'],))
    require(spec.output_delivery == profile_output_delivery(PROFILE))
    ready = list(
        c.execute(
            select(scaler_receipts.c.facts).where(
                scaler_receipts.c.intent_id == identity,
                scaler_receipts.c.operation == 'modal_runtime_bound',
            )
        ).scalars()
    )
    require(len(ready) == 1 and ready[0]['instance_id'] == spec.instance_id)
    require(ready[0]['manifest_digest'] == spec.engine_manifest_digest)
    require(bound['spec'] == asdict(hosts.spec))
    enforce(c, identity, bound['spec'], service.repo.clock())
    require(intent['hard_deadline'] > service.repo.clock())
    fact = hosts.client.lookup('inkseq-' + identity, spec.instance_id)
    require(fact == {'id': spec.instance_id, 'exit_code': None})
    result = usage(service, c)
    require(result['resources'].get(('modal', spec.instance_id)) == 1)
    snapshot = original.get('capacity_policy_snapshot')
    if snapshot is not None:
        validate_snapshot(snapshot)
        events = list(
            c.execute(
                select(outbox.c.payload).where(
                    outbox.c.event_type == 'instance.reserved',
                    outbox.c.aggregate_id == identity,
                )
            ).scalars()
        )
        require(len(events) == 1)
        require(events[0].get('capacity_policy_snapshot') == snapshot)
        proof = events[0].get('capacity_provenance', {})
        require(
            proof.get('capacity_backend') == original.get('capacity_backend') == BACKEND
        )
        reservation_provenance(
            snapshot,
            proof,
            Scope(service.settings.tenant_id, proof['owner_id'], proof['project_id']),
            spec.pool,
            'modal',
            continuing=False,
            gpus=intent['physical_gpus'],
            slots=intent['slots'],
            usage=result,
        )
        result['capacity_admission'] = {
            'capacity_backend': BACKEND,
            'lifecycle_version': 2,
            'intent_id': identity,
            'worker_id': spec.worker_id,
            'provider': 'modal',
            'instance_id': spec.instance_id,
            'pool': spec.pool,
            'capacity_policy_snapshot': snapshot,
        }
    return result
