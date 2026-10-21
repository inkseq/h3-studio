"""Same-origin human browser session controls; no caller-selected identity."""

from fastapi import Header, Request
from fastapi.responses import JSONResponse

from .account_sessions import AccountSessions, SessionError


def register_routes(app):
    service = AccountSessions(
        app.state.repository, app.state.auth, app.state.settings, app.state.workos_auth
    )
    app.state.account_sessions = service
    app.state.auth.session_checker = service.check

    @app.exception_handler(SessionError)
    async def session_error(_request, error):
        return JSONResponse(
            {"code": error.code, "detail": error.code},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/auth/sessions/revoke-all")
    def revoke_all(
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        from .api import COOKIE

        value = service.revoke_all(request.state.principal, body, key)
        response = JSONResponse(
            value,
            status_code=200 if value["state"] == "completed" else 202,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.delete_cookie(COOKIE, path="/")
        return response

    return service
