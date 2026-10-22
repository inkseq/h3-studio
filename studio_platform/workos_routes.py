"""AuthKit browser entry points and current-account API, without token storage."""

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .auth import LoginLimited
from .http_limits import hosted_error_redirect
from .workos_auth import STATE_COOKIE, STATE_SECONDS, WorkOSAuth, WorkOSError

HOSTED_PATHS = {"/api/auth/workos/start", "/api/auth/workos/callback"}
BROWSER_ERRORS = {
    "workos_state_invalid": "state_invalid",
    "return_to_invalid": "state_invalid",
    "workos_link_session_changed": "state_invalid",
    "workos_sign_in_cancelled": "cancelled",
    "account_deleted": "account_deleted",
    "account_unavailable": "account_unavailable",
    "account_suspended": "account_unavailable",
}


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

    def failure(request, *, code, detail, status, retry_after=None):
        hosted = request.method == "GET" and request.url.path in HOSTED_PATHS
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        public_code = (
            "rate_limited"
            if status == 429
            else BROWSER_ERRORS.get(code, "provider_unavailable")
        )
        result = hosted_error_redirect(
            request.scope, public_code, retry_after=retry_after
        )
        if result is None:
            result = JSONResponse(
                {"detail": detail, "code": code}, status_code=status, headers=headers
            )
        # An extra click must not erase the cookie of a pending Google flow.
        return clear_state(result) if hosted and status != 429 else result

    @app.exception_handler(WorkOSError)
    async def workos_error(request, error):
        return failure(
            request, code=error.code, detail=error.detail, status=error.status
        )

    @app.get("/api/auth/workos/start")
    def start(request: Request):
        query = request.query_params
        if (
            any(
                len(query.getlist(key)) > 1
                for key in ("return_to", "link", "screen_hint")
            )
            or query.get("link", "0") not in {"0", "1"}
            or (
                "screen_hint" in query
                and query["screen_hint"] not in {"sign-up", "sign-in"}
            )
        ):
            raise WorkOSError("workos_state_invalid")
        link = query.get("link") == "1"
        if link and request.headers.get("sec-fetch-site") == "cross-site":
            raise WorkOSError("workos_link_session_changed", 403)
        try:
            service.auth.reserve_workos_start(
                request.client.host if request.client else "unknown"
            )
        except LoginLimited as error:
            return failure(
                request,
                code="rate_limited",
                status=429,
                detail="Too many sign-in attempts. Please try again later.",
                retry_after=error.retry_after,
            )
        value = service.start(
            origin=origin(request),
            return_to=query.get("return_to", "/"),
            link=link,
            principal=request.state.principal,
            screen_hint=query.get("screen_hint"),
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
