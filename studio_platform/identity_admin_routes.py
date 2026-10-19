"""Human-admin account API; uses the app's authentication and CSRF boundary."""

from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse

from .identity_admin import AdminError, IdentityAdmin


def register_routes(app):
    service = IdentityAdmin(app.state.repository, app.state.settings)
    app.state.identity_admin = service

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.exception_handler(AdminError)
    async def admin_error(_request, error):
        return JSONResponse(
            {"detail": error.detail},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/admin/users")
    def list_users(
        request: Request,
        q: str = "",
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        return response(
            service.list_users(request.state.principal, q=q, cursor=cursor, limit=limit)
        )

    @app.patch("/v1/admin/users/{user_id}")
    async def update_user(
        user_id: str,
        request: Request,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        try:
            body = await request.json()
        except ValueError:
            raise AdminError("Invalid JSON") from None
        return response(service.patch(request.state.principal, user_id, body, key=key))

    @app.get("/v1/admin/audit")
    def audit(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        return response(
            service.audit(request.state.principal, cursor=cursor, limit=limit)
        )
