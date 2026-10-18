"""Cookie-only dstack adapter over the existing operator ledger and CSRF guard."""

from fastapi import Header, Request
from fastapi.responses import JSONResponse

PREFIX = "/v1/operator/dstack"


def register_routes(app, *, service=None):
    if service is None:
        from .dstack_operator import from_environment

        service = from_environment(app.state.repository, app.state.settings)
    app.state.dstack_operator = service

    def principal(request):
        return getattr(request.state, "principal", None)

    def response(value, status=200):
        return JSONResponse(
            value, status_code=status, headers={"Cache-Control": "no-store"}
        )

    @app.get(PREFIX + "/state")
    def state(request: Request):
        return response(service.state(principal(request)))

    @app.get(PREFIX + "/catalog")
    def catalog(request: Request):
        return response(service.catalog(principal(request)))

    @app.get(PREFIX + "/admin/prices")
    def admin_prices(request: Request):
        return response(service.admin_prices(principal(request)))

    @app.put(PREFIX + "/admin/prices/{profile_id}")
    def update_price(profile_id: str, request: Request, body: dict):
        return response(service.update_price(principal(request), profile_id, body))

    @app.post(PREFIX + "/previews")
    def preview(request: Request, body: dict):
        return response(service.preview(principal(request), body))

    @app.post(PREFIX + "/starts", status_code=202)
    def start(
        request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")
    ):
        return response(service.start(principal(request), body, key), 202)

    @app.post(PREFIX + "/nodes/{node_id}/stop", status_code=202)
    def stop(
        node_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(
            service.node_command(principal(request), node_id, body, key, "stop"), 202
        )

    @app.post(PREFIX + "/nodes/{node_id}/hold")
    def hold(
        node_id: str,
        request: Request,
        body: dict,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(service.set_hold(principal(request), node_id, body, key))

    from .dstack_workspace_routes import register_routes as register_workspace

    register_workspace(app, service=service)
