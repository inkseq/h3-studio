"""DBOS durable waits around finite original-domain film turns.

Registration is explicit in the single controller executor. No SDK import,
database initialization or process is started when the API imports this module.
"""

from .film_runs import VERSION, EXECUTION_BLOCKS
from .auth import AuthenticationError
from .repository import identifier, Conflict, BudgetExceeded
from .domain_turns import DomainTurns


def setup_controller_film(DBOS, domain_turns, repo, app):
    repo.generation_transport.client.register_queue(VERSION, polling_interval_sec=10)
    service = app.state.film_runs

    def principal_for(tenant, owner, project_id, run_id):
        grant = service.execution_context(tenant, owner, project_id, run_id)
        return app.state.auth.workflow_principal(owner, grant)

    register_film_workflows(DBOS, service, principal_for, domain_turns=domain_turns)


def register_film_workflows(DBOS, service, principal_for, *, domain_turns=None):
    domain_turns = domain_turns or DomainTurns()
    @DBOS.step(name='inkseq_film_turn_v1', retries_allowed=False)
    async def domain_turn(tenant, owner, project_id, run_id):
        for value in (tenant, owner, project_id, run_id):
            identifier(value)
        if tenant != service.tenant:
            raise ValueError('film_tenant_mismatch')

        def invoke():
            try:
                principal = principal_for(tenant, owner, project_id, run_id)
                if principal.owner != owner:
                    raise ValueError('film_owner_mismatch')
                return service.turn(principal, project_id, run_id)
            except AuthenticationError:
                return service.record_execution_block(
                    tenant, owner, project_id, run_id, 'film_actor_grant_unavailable'
                )
            except (Conflict, BudgetExceeded) as error:
                code = str(error)
                if code not in EXECUTION_BLOCKS:
                    raise
                return service.record_business_block(
                    principal, project_id, run_id, code
                )

        return await domain_turns.call(invoke)

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
