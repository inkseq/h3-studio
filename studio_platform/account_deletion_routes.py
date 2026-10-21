"""Staging-gated same-origin self deletion; opaque operation IDs confer no access."""

from fastapi import Header, Request
from fastapi.responses import JSONResponse

from .account_deletion import AccountDeletion, DeletionError


def register_routes(app):
    service = AccountDeletion(
        app.state.repository,
        app.state.auth,
        app.state.settings,
        app.state.workos_auth,
        app.state.quick_chat,
    )
    app.state.account_deletion = service

    @app.exception_handler(DeletionError)
    async def deletion_error(_request, error):
        return JSONResponse(
            {"code": error.code, "detail": error.code, **error.data},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/account/deletion/preview")
    def preview(request: Request, body: dict):
        return JSONResponse(
            service.preview(request.state.principal, body),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/account/deletion")
    def confirm(
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        from .api import COOKIE

        result = JSONResponse(
            service.confirm(request.state.principal, body, key),
            status_code=202,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        result.delete_cookie(COOKIE, path="/")
        return result

    return service
