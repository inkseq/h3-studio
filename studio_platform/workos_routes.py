"""AuthKit browser entry points and current-account API, without token storage."""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .auth import LoginLimited
from .workos_auth import STATE_COOKIE, STATE_SECONDS, WorkOSAuth, WorkOSError


def register_routes(app, *, service=None):
    service = service or WorkOSAuth(
        app.state.repository, app.state.auth, app.state.settings
    )
    app.state.workos_auth = service

    def origin(request):
        return app.state.settings.public_origin or str(request.base_url).rstrip("/")

    def clear_state(response):
        response.delete_cookie(
            STATE_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.exception_handler(WorkOSError)
    async def workos_error(request, error):
        result = JSONResponse(
            {"detail": error.detail, "code": error.code},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )
        return (
            clear_state(result)
            if request.url.path.startswith("/api/auth/workos/")
            else result
        )

    @app.get("/api/auth/workos/start")
    def start(request: Request):
        query = request.query_params
        if any(
            len(query.getlist(key)) > 1 for key in ("return_to", "link")
        ) or query.get("link", "0") not in {"0", "1"}:
            raise WorkOSError("workos_state_invalid")
        link = query.get("link") == "1"
        if link and request.headers.get("sec-fetch-site") == "cross-site":
            raise WorkOSError("workos_link_session_changed", 403)
        try:
            service.auth.reserve_login(
                "workos-start:" + (request.client.host if request.client else "unknown")
            )
        except LoginLimited as error:
            raise HTTPException(
                429,
                "Too many sign-in attempts. Please try again later.",
                headers={
                    "Retry-After": str(error.retry_after),
                    "Cache-Control": "no-store",
                },
            ) from None
        value = service.start(
            origin=origin(request),
            return_to=query.get("return_to", "/"),
            link=link,
            principal=request.state.principal,
        )
        result = RedirectResponse(
            value["url"],
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        result.set_cookie(
            STATE_COOKIE,
            value["cookie"],
            max_age=STATE_SECONDS,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return result

    @app.get("/api/auth/workos/callback")
    async def callback(request: Request):
        query = request.query_params
        if (
            len(query.getlist("state")) != 1
            or len(query.getlist("code")) > 1
            or len(query.getlist("error")) > 1
        ):
            raise WorkOSError("workos_state_invalid")
        value = await run_in_threadpool(
            service.callback,
            code=query.get("code"),
            state=query.get("state"),
            cookie=request.cookies.get(STATE_COOKIE),
            origin=origin(request),
            principal=request.state.principal,
            cancelled="error" in query,
        )
        from .api import BROWSER_HANDLE_SECONDS, COOKIE

        result = clear_state(RedirectResponse(value["return_to"], status_code=302))
        result.set_cookie(
            COOKIE,
            value["token"],
            max_age=BROWSER_HANDLE_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return result

    @app.get("/v1/account")
    def account(request: Request):
        return response(service.account(request.state.principal))

    @app.patch("/v1/account")
    def update_account(request: Request, body: dict):
        return response(service.update_account(request.state.principal, body))

    @app.post("/v1/account/authkit-widget-token")
    def widget_token(request: Request):
        return response(service.widget_token(request.state.principal))
