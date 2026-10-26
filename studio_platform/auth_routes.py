"""Password/session HTTP endpoints; policy and authentication stay in their services."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .auth import AuthenticationError, LoginLimited
from .identity import is_admin, public_identity


def create_auth_router(app, *, cookie: str, browser_handle_seconds: int) -> APIRouter:
    """Build app-local routes without owning middleware, lifespan or auth state."""
    router = APIRouter()
    repo, auth, settings = app.state.repository, app.state.auth, app.state.settings

    @router.get("/api/auth/config")
    def auth_config():
        return {
            "authentication": "password"
            if settings.auth_mode == "password"
            else "username-only-test",
            "auth_ready": auth.ready() or app.state.workos_auth.enabled(),
            "hosted_sign_in": {"enabled": app.state.workos_auth.enabled()},
            "users": ["superdan", "supervan"]
            if settings.auth_mode == "local-test"
            else [],
        }

    @router.post("/api/auth/login")
    async def login(request: Request):
        body = await request.json()
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("username"), str)
            or not isinstance(body.get("password", ""), str)
        ):
            raise HTTPException(422, "登录信息格式无效")
        try:
            token = await run_in_threadpool(
                auth.login,
                body["username"],
                body.get("password", ""),
                request.client.host if request.client else "unknown",
            )
        except LoginLimited as error:
            raise HTTPException(
                429, str(error), headers={"Retry-After": str(error.retry_after)}
            ) from None
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from None
        response = JSONResponse(
            {"username": body["username"], "authentication": settings.auth_mode}
        )
        response.set_cookie(
            cookie,
            token,
            max_age=browser_handle_seconds,
            httponly=True,
            secure=bool(settings.public_origin),
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/api/auth/me")
    def whoami(request: Request):
        principal = request.state.principal
        operator = is_admin(principal, repo, settings, legacy_capacity=True)
        from .credits import CreditError
        from .credits_pricing import PricingError

        try:
            credit_summary = {"enabled": True, **app.state.credits.balance(principal)}
        except (CreditError, PricingError):
            credit_summary = {"available": False, "balance": None, "held": None}
        return {
            "username": principal.owner,
            "actor_id": principal.actor_id,
            "machine": principal.machine,
            "authentication": principal.auth_mode or settings.auth_mode,
            **public_identity(principal, repo, settings),
            "credits": credit_summary,
            "scopes": list(principal.scopes),
            "features": {
                "account_deletion": app.state.account_deletion.capability(principal),
                "storyboard_workspace": not principal.machine
                and principal.owner in settings.storyboard_owners,
            },
            "operator_capacity": {
                name: operator
                for name in ("view", "start", "drain", "stop", "update_policy")
            }
            | {"admin": is_admin(principal, repo, settings)},
            "all_projects": principal.all_projects,
            "project_ids": list(principal.project_ids),
            **(
                {
                    "support_view": {
                        "viewer": principal.support_viewer,
                        "read_only": True,
                    }
                }
                if principal.support_viewer
                else {}
            ),
        }

    @router.post("/api/auth/logout")
    def logout(request: Request):
        provider_session = auth.logout(request.cookies.get(cookie))
        value = {"ok": True}
        logout_url = (
            app.state.workos_auth.logout_url(provider_session)
            if provider_session
            else None
        )
        if logout_url is not None:
            value["logout_url"] = logout_url
        response = JSONResponse(value)
        response.delete_cookie(cookie, path="/")
        if logout_url is not None:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return router
