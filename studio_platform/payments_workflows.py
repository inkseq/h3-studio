"""Verified payment events use the existing DBOS application and queue.

The event row is the business journal; DBOS owns durable wake/wait/replay.
Provider reads happen before, never inside, the core money transaction.
"""

import asyncio
import hashlib
import json

from .payments import PaymentCore
from .domain_turns import DomainTurns

PAYMENT_WORKFLOW = "inkseq.payment.event.v1"


def payment_workflow_id(tenant, event_id):
    return (
        "inkseq-payment-"
        + hashlib.sha256(
            json.dumps([tenant, event_id], separators=(",", ":")).encode()
        ).hexdigest()
    )


def register_payment_workflows(
    DBOS, repo, settings, *, gateway_factory=None, domain_turns=None
):
    domain_turns = domain_turns or DomainTurns()
    core = PaymentCore(repo, settings)
    operation_limit = asyncio.Semaphore(4)

    def turn(tenant, event_id):
        try:
            original = core.event_for_processing(tenant, event_id)
            if original is None:
                return {"done": True, "state": "complete"}
            if gateway_factory is None:
                from .stripe_client import StripeGateway

                gateway = StripeGateway(settings)
            else:
                gateway = gateway_factory(settings)
            proof = gateway.resolve_event(original)
            return core.process_event(tenant, event_id, proof=proof)
        except Exception:
            # The SDK may contain request/secret-bearing exception objects.
            # Persist only one finite diagnostic and retain the original event.
            try:
                return core.defer_event(tenant, event_id)
            except Exception:
                return {"done": False, "state": "unavailable", "wait_seconds": 30}

    @DBOS.step(name="inkseq.payment.event-turn.v1", retries_allowed=False)
    async def event_turn(tenant, event_id):
        async with operation_limit:
            return await domain_turns.call(turn, tenant, event_id)

    @DBOS.workflow(name=PAYMENT_WORKFLOW, max_recovery_attempts=None)
    async def process(tenant, event_id):
        while True:
            result = await event_turn(tenant, event_id)
            if result["done"]:
                return {"event_id": event_id, "state": result["state"]}
            await DBOS.recv_async("ledger", timeout_seconds=result["wait_seconds"])

    return process
