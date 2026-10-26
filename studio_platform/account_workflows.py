"""Original private account operations run in the existing DBOS app and queue."""

import asyncio

from .account_sessions import AccountSessions, WORKFLOW
from .auth import Auth
from .workos_auth import WorkOSAuth
from .domain_turns import DomainTurns


def register_account_workflows(
    DBOS, repo, settings, *, sessions=None, deletion=None, domain_turns=None
):
    domain_turns = domain_turns or DomainTurns()
    # Auth metadata already exists; this is a reader/writer of the existing
    # service, never a new table family, schema adoption or provider setup.
    if sessions is None:
        auth = Auth(
            repo.engine,
            tenant=settings.tenant_id,
            mode=settings.auth_mode,
            session_seconds=settings.session_seconds,
        )
        sessions = AccountSessions(
            repo, auth, settings, WorkOSAuth(repo, auth, settings)
        )
    limit = asyncio.Semaphore(2)

    @DBOS.step(name="inkseq.account.lifecycle-turn.v1", retries_allowed=False)
    async def turn(tenant, operation_id):
        async with limit:
            return await domain_turns.call(sessions.turn, tenant, operation_id)

    @DBOS.workflow(name=WORKFLOW, max_recovery_attempts=None)
    async def process(tenant, operation_id):
        while True:
            result = await turn(tenant, operation_id)
            if result["done"]:
                return {"operation_id": operation_id, "state": result["state"]}
            await DBOS.recv_async(
                "account_changed", timeout_seconds=result["wait_seconds"]
            )

    if deletion is not None:
        register_deletion_workflow(DBOS, deletion, limit, domain_turns=domain_turns)
    return process


def register_deletion_workflow(DBOS, service, limit, *, domain_turns=None):
    domain_turns = domain_turns or DomainTurns()
    from .account_deletion import WORKFLOW as DELETION_WORKFLOW

    @DBOS.step(name="inkseq.account.deletion-turn.v1", retries_allowed=False)
    async def turn(tenant, operation_id):
        async with limit:
            try:
                result = await domain_turns.call(service.turn, tenant, operation_id)
            except Exception:
                result = None
            return result or {"done": False, "state": "unavailable", "wait_seconds": 60}

    @DBOS.workflow(name=DELETION_WORKFLOW, max_recovery_attempts=None)
    async def process(tenant, operation_id):
        while True:
            result = await turn(tenant, operation_id)
            if result["done"]:
                return {"operation_id": operation_id, "state": result["state"]}
            await DBOS.recv_async(
                "account_changed", timeout_seconds=result["wait_seconds"]
            )

    return process
