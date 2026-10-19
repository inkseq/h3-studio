"""DBOS durable waits around finite original-domain film turns.

Registration is explicit in the single controller executor. No SDK import,
database initialization or process is started when the API imports this module.
"""

import asyncio

from .film_runs import VERSION
from .repository import identifier


def register_film_workflows(DBOS, service, principal_for):
    @DBOS.step(name='inkseq_film_turn_v1', retries_allowed=False)
    async def domain_turn(tenant, owner, project_id, run_id):
        for value in (tenant, owner, project_id, run_id):
            identifier(value)
        if tenant != service.tenant:
            raise ValueError('film_tenant_mismatch')
        principal = principal_for(tenant, owner, project_id, run_id)
        if principal.owner != owner:
            raise ValueError('film_owner_mismatch')
        return await asyncio.to_thread(service.turn, principal, project_id, run_id)

    @DBOS.workflow(name=VERSION)
    async def film(tenant, owner, project_id, run_id):
        while True:
            result = await domain_turn(tenant, owner, project_id, run_id)
            if result['terminal']:
                return result
            # Native work has its own authoritative observation deadline. This
            # durable wait owns no GPU lease and never polls via time.sleep.
            await DBOS.recv_async(topic='film_changed', timeout_seconds=10)

    return film
