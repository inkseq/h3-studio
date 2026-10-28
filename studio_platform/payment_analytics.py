"""Optional anonymous purchase funnel; money never depends on analytics.

Attribution uses existing scoped documents, never the immutable purchase or its
fingerprint. Captures run after the original money transaction on the existing
payment turn. No new queue, SDK consumer thread, account identifier or content.
"""

from copy import deepcopy
from importlib.metadata import version
import logging
import re
import uuid

from sqlalchemy import and_, select

from .credit_schema import entries
from .identity_schema import users
from .payment_schema import events, payments
from .repository import documents

SDK_VERSION = '7.60.0'
HOST = 'https://us.i.posthog.com'
# Existing public write token from the reviewed browser project; not a new key.
PROJECT_KEY = 'phc_rQdpUhKN9KJA8k9doCZGui6czQhqGj8NingDHmXmtEvc'
EVENTS = frozenset({'payment_succeeded', 'payment_failed'})
PROJECT = '__billing_analytics'
KIND = 'payment_analytics'


def anonymous_context(value):
    """Invalid or absent optional analytics suppresses collection, not Checkout."""
    if type(value) is not dict or set(value) != {'consent', 'distinct_id'}:
        return None
    if value['consent'] is not True or type(value['distinct_id']) is not str:
        return None
    try:
        ident = uuid.UUID(value['distinct_id'])
    except (ValueError, AttributeError):
        return None
    # Accept SDK-generated anonymous random IDs, never UUIDs deriving from names.
    if str(ident) != value['distinct_id'] or ident.version not in {4, 7}:
        return None
    return {'consent': True, 'distinct_id': str(ident)}


def environment(settings):
    target = getattr(settings, 'deployment_target', None)
    origin = getattr(settings, 'public_origin', None)
    wanted = {
        'staging': 'https://staging.inkseq.com',
        'production': 'https://inkseq.com',
    }
    return target if target in wanted and origin == wanted[target] else None


def remember(repo, settings, principal, payment, context):
    """Save valid original anonymous attribution once; any failure is isolated."""
    context, target = anonymous_context(context), environment(settings)
    if context is None or target is None:
        return False
    try:
        with repo.transaction() as connection:
            original = (
                connection.execute(
                    select(payments.c.id, payments.c.pack_id)
                    .join(
                        users,
                        and_(
                            users.c.id == payments.c.user_id,
                            users.c.tenant == payments.c.tenant,
                        ),
                    )
                    .where(
                        payments.c.id == payment['id'],
                        payments.c.tenant == settings.tenant_id,
                        users.c.owner == principal.owner,
                        payments.c.livemode == int(target == 'production'),
                    )
                )
                .mappings()
                .first()
            )
            if original is None:
                return False
            if connection.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert as upsert
            else:
                from sqlalchemy.dialects.sqlite import insert as upsert
            connection.execute(
                upsert(documents)
                .values(
                    tenant_id=settings.tenant_id,
                    owner_id=principal.owner,
                    project_id=PROJECT,
                    kind=KIND,
                    document_id=original['id'],
                    version=1,
                    payload={
                        **context,
                        'environment': target,
                        'pack_id': original['pack_id'],
                    },
                    updated_at=repo.clock(),
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        'tenant_id',
                        'owner_id',
                        'project_id',
                        'kind',
                        'document_id',
                    ]
                )
            )
        return True
    except Exception:
        # No endpoint, SQL/SDK exception text or analytics ID enters logs.
        return False


def closed_event(value):
    """Rebuild after SDK enrichment; reject person/profile/context properties."""
    if type(value) is not dict or value.get('event') not in EVENTS:
        return None
    props = value.get('properties')
    context = anonymous_context(
        {'consent': True, 'distinct_id': value.get('distinct_id')}
    )
    if context is None or type(props) is not dict:
        return None
    if props.get('environment') not in {'staging', 'production'}:
        return None
    pack = props.get('pack_id')
    if type(pack) is not str or re.fullmatch('[A-Za-z0-9_-]{1,64}', pack) is None:
        return None
    try:
        event_id = str(uuid.UUID(str(value.get('uuid'))))
    except (ValueError, AttributeError):
        return None
    result = {
        'event': value['event'],
        'distinct_id': context['distinct_id'],
        'uuid': event_id,
        'properties': {
            'environment': props['environment'],
            'pack_id': pack,
            '$process_person_profile': False,
            '$geoip_disable': True,
        },
    }
    # The SDK supplies an ISO timestamp; no caller timestamp or arbitrary context.
    stamp = value.get('timestamp')
    if type(stamp) is str and re.fullmatch(r'[0-9T:.+Z-]{10,40}', stamp):
        result['timestamp'] = stamp
    return result


def sdk_client():
    """Official synchronous SDK; no feature poller or background capture thread.

    timeout bounds each request. SDK7.60's requests adapter can retry connection
    establishment independently of max_retries; this is not an exactly-once or
    hard wall-clock guarantee. Stable event UUIDs preserve capture deduplication.
    """
    if version('posthog') != SDK_VERSION:
        raise ValueError('payment_analytics_sdk_mismatch')
    from posthog import Posthog

    client = Posthog(
        PROJECT_KEY,
        host=HOST,
        sync_mode=True,
        timeout=2,
        max_retries=0,
        capture_mode='v0',
        max_queue_size=1,
        enable_local_evaluation=False,
        enable_exception_autocapture=False,
        capture_trace_context=False,
        disable_geoip=True,
        before_send=closed_event,
    )
    # The SDK's no-throw wrapper may log upstream exception text. This client
    # owns an inert logger, leaving all unrelated loggers/clients unchanged.
    client.log = logging.Logger('inkseq.payment.analytics', level=logging.CRITICAL + 1)
    return client


def _outcome(connection, settings, tenant, event_id):
    row = (
        connection.execute(
            select(payments, users.c.owner, events.c.type.label('event_type'))
            .join(
                events,
                and_(
                    events.c.payment_id == payments.c.id,
                    events.c.tenant == payments.c.tenant,
                    events.c.account_scope == payments.c.account_scope,
                    events.c.livemode == payments.c.livemode,
                ),
            )
            .join(
                users,
                and_(
                    users.c.id == payments.c.user_id,
                    users.c.tenant == payments.c.tenant,
                ),
            )
            .where(
                payments.c.tenant == tenant,
                events.c.event_id == event_id,
                events.c.status == 'processed',
                payments.c.livemode == int(environment(settings) == 'production'),
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row['paid_at'] is not None and row['event_type'] in {
        'checkout.session.completed',
        'checkout.session.async_payment_succeeded',
    }:
        original = connection.execute(
            select(entries.c.id)
            .where(
                entries.c.tenant == tenant,
                entries.c.user_id == row['user_id'],
                entries.c.kind == 'purchase',
                entries.c.amount == row['credits'],
                entries.c.held_delta == 0,
                entries.c.actor == 'stripe:webhook',
                entries.c.reference_type == 'payment',
                entries.c.reference_id == row['id'],
                entries.c.idempotency_key
                == 'stripe-purchase:' + row['stripe_checkout_session_id'],
                entries.c.origin
                == ('stripe-live' if row['livemode'] else 'stripe-test'),
            )
            .limit(1)
        ).first()
        event = 'payment_succeeded' if original is not None else None
    elif (
        row['status'] == 'failed'
        and row['paid_at'] is None
        and row['event_type'] == 'checkout.session.async_payment_failed'
    ):
        event = 'payment_failed'
    else:
        event = None
    if event is None:
        return None
    attribution = connection.execute(
        select(documents.c.payload).where(
            documents.c.tenant_id == tenant,
            documents.c.owner_id == row['owner'],
            documents.c.project_id == PROJECT,
            documents.c.kind == KIND,
            documents.c.document_id == row['id'],
        )
    ).scalar_one_or_none()
    if (
        type(attribution) is not dict
        or anonymous_context(
            {
                'consent': attribution.get('consent'),
                'distinct_id': attribution.get('distinct_id'),
            }
        )
        is None
        or attribution.get('environment') != environment(settings)
        or attribution.get('pack_id') != row['pack_id']
    ):
        return None
    return {
        'event': event,
        'distinct_id': attribution['distinct_id'],
        'uuid': str(
            uuid.uuid5(uuid.NAMESPACE_URL, 'inkseq:' + row['id'] + ':' + event)
        ),
        'properties': {
            'environment': attribution['environment'],
            'pack_id': row['pack_id'],
            '$process_person_profile': False,
            '$geoip_disable': True,
        },
    }


def observe(repo, settings, tenant, event_id, *, client_factory=None):
    """Best-effort capture after commit; never alter/replay the money outcome."""
    if tenant != settings.tenant_id or environment(settings) is None:
        return False
    client = None
    try:
        with repo.engine.connect() as connection:
            if connection.dialect.name == 'postgresql':
                connection = connection.execution_options(postgresql_readonly=True)
            event = _outcome(connection, settings, tenant, event_id)
        if event is None:
            return False
        client = (client_factory or sdk_client)()
        return (
            client.capture(
                event['event'],
                distinct_id=event['distinct_id'],
                properties=deepcopy(event['properties']),
                uuid=event['uuid'],
            )
            is not None
        )
    except Exception:
        return False
    finally:
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass
