"""Human operator host observations and audited future-allocation policy.

Shared authentication/CSRF/account guards still belong to the application. No
provider call, host mutation or inference submission is performed by these routes.
"""
from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse


def register_routes(app):
    from .host_scorecard import OperatorHosts
    from .stage_metrics import configured_stage_reader

    service = OperatorHosts(app.state.repository, app.state.settings,
        stage_reader=configured_stage_reader(app.state.settings.stage_query_config_file))
    app.state.operator_hosts = service

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/v1/operator/hosts")
    def owned(request: Request, limit: int = Query(100, ge=1, le=100),
              offset: int = Query(0, ge=0, le=100000)):
        return response(service.list(request.state.principal, limit=limit, offset=offset))

    @app.get("/v1/operator/admin/hosts")
    def admin(request: Request, limit: int = Query(100, ge=1, le=100),
              offset: int = Query(0, ge=0, le=100000),
              machine_id: str | None = Query(None, pattern=r"^[1-9][0-9]{0,17}$")):
        return response(service.list(request.state.principal, limit=limit, offset=offset,
            admin=True, machine_id=machine_id))

    @app.get("/v1/operator/admin/hosts/audit")
    def audit(request: Request, limit: int = Query(100, ge=1, le=100),
              offset: int = Query(0, ge=0, le=100000)):
        return response(service.audit(request.state.principal, limit=limit, offset=offset))

    @app.post("/v1/operator/admin/hosts/policy")
    def policy(request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return response(service.policy(request.state.principal, body, key))

    return service
