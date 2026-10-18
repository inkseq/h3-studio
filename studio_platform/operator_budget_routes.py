"""Owner-scoped cumulative budgets; human admin edits on the original ledger."""

from fastapi import Query, Request
from fastapi.responses import JSONResponse

from .operator_budgets import OperatorBudgets


def register_routes(app):
    service = OperatorBudgets(app.state.repository, app.state.settings)
    app.state.operator_budgets = service

    def principal(request):
        return getattr(request.state, "principal", None)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/v1/operator/budgets")
    def owner_budgets(
        request: Request,
        limit: int = Query(100, ge=1, le=100),
        after_id: str | None = Query(None, max_length=200),
    ):
        return response(
            service.owned(principal(request), limit=limit, after_id=after_id)
        )

    @app.get("/v1/operator/admin/budgets")
    def admin_budgets(
        request: Request,
        limit: int = Query(100, ge=1, le=100),
        after_id: str | None = Query(None, max_length=200),
    ):
        return response(
            service.admin(principal(request), limit=limit, after_id=after_id)
        )

    @app.get("/v1/operator/admin/budgets/audit")
    def audit(
        request: Request,
        limit: int = Query(100, ge=1, le=100),
        before: str | None = Query(None, max_length=100),
    ):
        return response(service.audit(principal(request), limit=limit, before=before))

    @app.put("/v1/operator/admin/budgets/{account_id}")
    def update_budget(account_id: str, request: Request, body: dict):
        return response(service.update(principal(request), account_id, body))
