"""Human-admin preview/confirmation on the original billing authority."""
from fastapi import Query, Request
from fastapi.responses import JSONResponse

from .operator_billing import BillingRefused, OperatorBilling


def register_routes(app):
    service = OperatorBilling(app.state.repository, app.state.settings)
    app.state.operator_billing = service

    @app.exception_handler(BillingRefused)
    async def refused(_request, error):
        return JSONResponse({"code": error.code, "no_mutation_committed": True},
            status_code=error.status, headers={"Cache-Control": "no-store"})

    def principal(request):
        return getattr(request.state, "principal", None)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/v1/operator/admin/billing/references")
    def references(request: Request, owner_id: str = Query(..., max_length=200),
                   limit: int = Query(50, ge=1, le=100), after: str | None = Query(None, max_length=512),
                   created_after: float | None = None, created_before: float | None = None):
        return response(service.references(principal(request), owner_id, limit=limit, after=after,
            created_after=created_after, created_before=created_before))

    @app.post("/v1/operator/admin/billing/previews")
    def preview(request: Request, body: dict):
        return response(service.preview(principal(request), body))

    @app.get("/v1/operator/admin/billing/actions/by-key")
    def by_key(request: Request, key: str = Query(..., min_length=1, max_length=200)):
        return response(service.by_key(principal(request), key))

    @app.get("/v1/operator/admin/billing/actions/{action_id}")
    def action(action_id: str, request: Request):
        return response(service.action(principal(request), action_id))

    @app.post("/v1/operator/admin/billing/actions")
    def apply(request: Request, body: dict):
        return response(service.apply(principal(request), body, request.headers.get("Idempotency-Key")))
