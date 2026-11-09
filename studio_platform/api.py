"""Versioned video business API. No import of the legacy single-worker server."""

from __future__ import annotations

import logging

import asyncio
from contextlib import asynccontextmanager
import re
import time

from fastapi import (
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .auth import SessionUnavailable
from .identity import require_admin
from .capabilities import capabilities
from .settings import Settings
from .storage import (
    VIDEO_SIGNING_BUCKET_SECONDS,
)
from .http_limits import (
    ADMISSION_SCOPE_KEY,
    BodyLimitMiddleware,
    UploadAdmission,
    RequestAdmission,
    RequestAdmissionMiddleware,
    admission_rejected,
    hosted_error_redirect,
    is_asset_upload,
    body_limit,
)
from .render_plans import RECIPE as RENDER_RECIPE
from .agent_discovery import PUBLIC_PATHS as AGENT_PUBLIC_PATHS, DISCOVERY_LINK
from .agent_connect_routes import (
    EXCHANGE_PATH,
    PREVIEW_PATH,
    GrantPreviewPrivacyMiddleware,
    register_routes as register_agent_connect_routes,
)
from .frontend import (
    FRONTEND_CONTRACT,
    STATIC_CACHE_SCOPE_KEY,
    is_page_navigation,
    is_public_frontend,
)
from .operator_capacity import (
    OperatorRegistry,
)  # Register additive tables before create_schema.
from . import capacity_market  # Read-only market cache; never authorizes capacity.  # noqa: F401 -- register additive tables before schema creation
from . import (
    dstack_workspace,  # noqa: F401 -- register presentation tables before schema creation
)  # Account-owned presentation metadata before schema creation.

# Session checks wait up to 2 s for a free slot before answering 429.
AUTH_SLOT_WAIT_SECONDS = 0.05
AUTH_SLOT_WAIT_STEPS = 40

COOKIE = "sixnine_session"
# The opaque handle can outlive its authority: every request checks the SQL
# seven-day inactivity expiry. Only login writes the cookie, so a late response
# from an older account cannot overwrite a newer login or restore a logout.
_logger = logging.getLogger(__name__)
BROWSER_HANDLE_SECONDS = 365 * 86400
TERMINAL = {"succeeded", "failed", "cancelled"}
ARTIFACT_VIDEO_CACHE_SCOPE_KEY = "inkseq.immutable-artifact-video-cache"


def create_app(
    settings: Settings,
    *,
    repository=None,
    storage=None,
    quick_chat_hooks=None,
    assistant=None,
    assistant_enabled=False,
    operator_registry=None,
    title_generator=None,
    dstack_operator=None,
    story_chat_client=None,
    story_chat_image_client=None,
    story_chat_billing=None,
):
    from .application_services import create_core

    services = create_core(
        settings,
        repository=repository,
        storage=storage,
        capabilities_provider=lambda: capabilities(settings),
    )
    repo, storage, auth, asset_service = (
        services.repository,
        services.storage,
        services.auth,
        services.assets,
    )
    upload_admission = UploadAdmission()
    request_admission = RequestAdmission()

    @asynccontextmanager
    async def lifespan(app):
        await app.state.quick_chat_recovery.start()
        try:
            yield
        finally:
            await app.state.quick_chat_recovery.close()
            capacity = getattr(
                getattr(app.state, "dstack_operator", None), "capacity", None
            )
            if capacity is not None:
                capacity.client.close()
            if repository is None:
                repo.close()

    from .error_monitoring import monitored_app

    app = monitored_app(
        settings, title="Inkseq video platform", version="1.0.0-dev", lifespan=lifespan
    )
    app.add_middleware(
        BodyLimitMiddleware,
        project_bytes=settings.max_project_bytes,
        upload_bytes=settings.max_upload_bytes,
    )
    app.state.repository, app.state.auth, app.state.storage = repo, auth, storage
    app.state.assets, app.state.settings = asset_service, settings
    app.state.upload_admission = upload_admission
    app.state.request_admission = request_admission
    app.state.services = services
    generation_access, generation_read, generation_planning = (
        services.access,
        services.read,
        services.planning,
    )
    app.state.generation_access = generation_access
    app.state.generation_read = generation_read
    app.state.generation_planning = generation_planning
    # Compatibility names used by existing scenario and batch routes now point
    # to HTTP-independent services, rather than closures over this app factory.
    scope = generation_access.scope
    authorized_project, owned_plan = (
        generation_access.project,
        generation_access.plan,
    )
    public_job = generation_read.public_job

    from .api_errors import register_handlers

    register_handlers(app, repository=repo, settings=settings)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        request_public_origin = settings.public_origin_for_host(host)
        if settings.public_origin:
            if request_public_origin is None and not (
                request.url.path == "/healthz"
                and host.split(":")[0] in {"localhost", "127.0.0.1"}
            ):
                return JSONResponse({"detail": "无效访问域名"}, status_code=400)
        elif host.split(":")[0] not in {"localhost", "127.0.0.1", "testserver"}:
            return JSONResponse(
                {"detail": "本地服务仅支持loopback入口"}, status_code=400
            )
        maximum = body_limit(
            request.url.path, settings.max_project_bytes, settings.max_upload_bytes
        )
        length = request.headers.get("content-length")
        if length and (
            len(length) > 20
            or not length.isascii()
            or not length.isdigit()
            or int(length) > maximum
        ):
            return JSONResponse({"detail": "请求体超过限制"}, status_code=413)
        principal = None
        authorization = request.headers.get("authorization", "")
        lease = request.scope[ADMISSION_SCOPE_KEY]
        # A page opens several API reads at once; a short wait for a session-check
        # slot beats an immediate 429 that the browser shows as a failed load.
        for _ in range(AUTH_SLOT_WAIT_STEPS):
            if lease.acquire("authentication"):
                break
            await asyncio.sleep(AUTH_SLOT_WAIT_SECONDS)
        else:
            return admission_rejected(request.scope)
        try:
            if authorization:
                if authorization.startswith("Bearer "):
                    principal = await run_in_threadpool(auth.bearer, authorization[7:])
                if not principal:
                    return JSONResponse({"detail": "服务身份无效"}, status_code=401)
            else:
                # A provider outage or legacy missing SID must never prevent
                # the holder revoking only their own local sessions. Ordinary
                # callers keep the existing session(token) adapter contract.
                if request.url.path in {
                    "/api/auth/logout",
                    "/api/auth/sessions/revoke-all",
                }:
                    principal = await run_in_threadpool(
                        auth.session, request.cookies.get(COOKIE), verify_provider=False
                    )
                else:
                    principal = await run_in_threadpool(
                        auth.session, request.cookies.get(COOKIE)
                    )
        except SessionUnavailable:
            redirect = hosted_error_redirect(
                request.scope, "provider_unavailable", retry_after=3
            )
            if redirect is not None:
                return redirect
            return JSONResponse(
                {
                    "detail": "Session verification is temporarily unavailable.",
                    "code": "session_provider_unavailable",
                },
                status_code=503,
                headers={"Cache-Control": "no-store", "Retry-After": "3"},
            )
        except SQLAlchemyError:
            # Middleware runs outside FastAPI's endpoint exception handlers.
            # Connection errors must not escape with a DSN or bound token hash.
            redirect = hosted_error_redirect(
                request.scope, "provider_unavailable", retry_after=3
            )
            if redirect is not None:
                return redirect
            return JSONResponse(
                {"detail": "身份服务暂时不可用，请稍后重试"},
                status_code=503,
                headers={"Cache-Control": "no-store", "Retry-After": "3"},
            )
        finally:
            lease.release("authentication")
        support_view_id = request.headers.get("x-support-view")
        if support_view_id is None and request.method in {"GET", "HEAD"}:
            # Native media cannot send headers; only content downloads take the query form.
            if re.fullmatch(
                r"/v1/(?:assets|artifacts)/[^/]+/content", request.url.path
            ):
                support_view_id = request.query_params.get("support_view")
        if support_view_id is not None:
            from .support_view import refusal, resolve

            viewed = await run_in_threadpool(
                resolve, repo, settings, principal, support_view_id
            )
            refused = (
                "support_view_invalid"
                if viewed is None
                else refusal(request.method, request.url.path)
            )
            if refused:
                return JSONResponse(
                    {"detail": refused, "code": refused},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
            principal = viewed
        expected_account = request.headers.get("x-expected-account")
        native_content = (
            request.method in {"GET", "HEAD"}
            and re.fullmatch(
                r"/v1/(?:assets|artifacts)/[^/]+/content", request.url.path
            )
            is not None
        )
        if native_content and "expected_account" in request.query_params:
            # Native media/navigation cannot send the browser's custom header.
            # This hint fences cookie identity only; it never selects an owner
            # or grants a machine client access to a different project/account.
            accounts = request.query_params.getlist("expected_account")
            header_accounts = request.headers.getlist("x-expected-account")
            if (
                len(accounts) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", accounts[0])
                or len(header_accounts) > 1
                or expected_account is not None
                and expected_account != accounts[0]
            ):
                return JSONResponse(
                    {
                        "detail": "账户校验参数无效或互相冲突",
                        "code": "invalid_account_context",
                    },
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )
            expected_account = accounts[0]
        if (
            principal
            and not principal.machine
            and request.url.path.startswith("/v1/")
            and expected_account is not None
            and expected_account != principal.owner
        ):
            # Cookies are shared across tabs and ports. An old tab must not
            # accidentally apply its draft to a new owner's same project ID.
            return JSONResponse(
                {
                    "detail": "登录账户已改变，已阻止本次操作；请先核对当前账户",
                    "code": "account_context_changed",
                },
                status_code=409,
                headers={
                    "Cache-Control": "no-store",
                    "X-Authenticated-Account": principal.owner,
                },
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not (
            principal and principal.machine
        ):
            origin = request.headers.get("origin")
            expected = request_public_origin or str(request.base_url).rstrip("/")
            allowed_origins = {expected, *settings.local_ui_origins}
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "跨站写入请求被拒绝"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "跨站写入请求被拒绝"}, status_code=403)
        public_paths = {"/healthz", "/api/auth/config", "/api/auth/login"}
        if request.method == "GET":
            public_paths.update({"/api/auth/workos/start", "/api/auth/workos/callback"})
            public_paths.add("/v1/credits/prices")
            public_paths.add("/v1/billing/packs")
            public_paths.add("/v1/legal/policy")
        if request.method == "POST":
            public_paths.add("/v1/billing/stripe/webhook")
            # A dial-out GPU worker has no session; its one-time boot token
            # is checked by the route itself (#859).
            public_paths.add("/v1/worker/boot")
        public_frontend = is_public_frontend(
            settings, request.method, request.url.path
        ) or is_page_navigation(
            settings, request.method, request.url.path, request.headers.get("accept")
        )
        public_discovery = (
            request.method in {"GET", "HEAD"} and request.url.path in AGENT_PUBLIC_PATHS
        )
        public_exchange = request.method == "POST" and request.url.path in {
            EXCHANGE_PATH,
            PREVIEW_PATH,
        }
        if (
            request.url.path not in public_paths
            and not public_frontend
            and not public_discovery
            and not public_exchange
            and not principal
        ):
            cookie = request.cookies.get(COOKIE)
            if cookie and not authorization:
                # A cookie was sent but refused: log the closed reason only (#1145).
                try:
                    reason = await run_in_threadpool(auth.refusal_reason, cookie)
                except Exception as error:
                    reason = "diagnosis_failed_" + type(error).__name__
                _logger.warning(
                    "session_cookie_refused reason=%s path=%s",
                    reason,
                    request.url.path[:80],
                )
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        request.state.principal = principal
        if (
            principal is not None
            and principal.identity_source == "canonical"
            and request.url.path.startswith("/v1/operator/")
        ):
            from .operator_capacity import OperatorError

            try:
                require_admin(principal, repo, settings)
            except OperatorError as error:
                return JSONResponse(
                    {"code": error.code, "message": error.code},
                    status_code=error.status,
                    headers={"Cache-Control": "no-store"},
                )
        if principal and not lease.acquire("owner", principal.owner):
            return admission_rejected(request.scope)
        downloading = (
            principal is not None
            and request.method in {"GET", "HEAD"}
            and request.url.path.endswith("/content")
            and request.url.path.startswith(("/v1/artifacts/", "/v1/assets/"))
        )
        if downloading:
            # Only the strict small-copy selector enters this gate. The route
            # still authorizes its original owner/project and verifies the
            # <=64KiB role; invalid/missing selectors never serve originals.
            thumbnail = request.query_params.getlist("rendition") == ["thumb"]
            global_gate, owner_gate = (
                ("thumbnails", "owner_thumbnails")
                if thumbnail
                else ("downloads", "owner_downloads")
            )
            if not lease.acquire(global_gate) or not lease.acquire(
                owner_gate, principal.owner
            ):
                return admission_rejected()
        uploading = (
            request.method == "POST"
            and is_asset_upload(request.url.path)
            and principal is not None
        )
        if uploading and not upload_admission.acquire(principal.owner):
            return JSONResponse(
                {"detail": "同时上传的文件已达到上限，请等当前上传完成再重试"},
                status_code=429,
                headers={"Retry-After": "3"},
            )
        if uploading:
            lease.release_when_finished(
                lambda: upload_admission.release(principal.owner)
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = (
            request.scope.get(STATIC_CACHE_SCOPE_KEY, "no-store")
            if response.status_code < 400
            else "no-store"
        )
        video_max_age = request.scope.get(ARTIFACT_VIDEO_CACHE_SCOPE_KEY)
        if (
            principal is not None
            and request.method == "GET"
            and response.status_code == 307
            and getattr(request.scope.get("route"), "path", None)
            in {"/v1/artifacts/{artifact_id}/content", "/v1/assets/{asset_id}/content"}
            and type(video_max_age) is int
            and 0 <= video_max_age <= VIDEO_SIGNING_BUCKET_SECONDS
        ):
            response.headers["Cache-Control"] = f"private, max-age={video_max_age}"
            vary = [
                value.strip()
                for value in response.headers.get("Vary", "").split(",")
                if value.strip()
            ]
            for header in ("Authorization", "Cookie", "X-Expected-Account"):
                if header.lower() not in {value.lower() for value in vary}:
                    vary.append(header)
            response.headers["Vary"] = ", ".join(vary)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path in {"/", "/index.html", "/for-agents", "/for-agents/"}:
            response.headers["Link"] = DISCOVERY_LINK
        if principal and request.url.path.startswith("/v1/"):
            response.headers["X-Authenticated-Account"] = principal.owner
        return response

    @app.get("/healthz", include_in_schema=False)
    def health():
        with repo.engine.connect() as connection:
            connection.execute(select(1)).scalar_one()
        return {
            "status": "ok",
            "generation_enabled": settings.generation_enabled,
            # Public liveness must not disclose the engine/provider identity.
            # Exact execution identity remains in protected configuration checks.
            "execution_backend": "disabled"
            if settings.execution_backend == "disabled"
            else "enabled",
            "auth_ready": auth.ready() or app.state.workos_auth.enabled(),
            "render_enabled": settings.render_enabled,
            "frontend_contract": FRONTEND_CONTRACT,
            "cloud_creation_enabled": False,
        }

    from .auth_routes import create_auth_router

    app.include_router(
        create_auth_router(
            app, cookie=COOKIE, browser_handle_seconds=BROWSER_HANDLE_SECONDS
        )
    )

    @app.get("/v1/capabilities")
    def get_capabilities():
        result = capabilities(settings)
        result["upload_max_bytes"] = settings.max_upload_bytes
        result["chapter_render"] = {
            "recipe_id": RENDER_RECIPE,
            "implemented": True,
            "enabled": settings.render_enabled,
            "resolutions": ["480P", "720P"],
            "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"],
            "fps": 24,
            "max_shots": 50,
            "max_duration": 600,
            "max_audio_tracks": 32,
            "video_original_audio": False,
            "subtitles": True,
            "transitions": False,
            "source_trim": True,
            "generated_audio": True,
            "render_contract_version": 5,
            "playback_rates": [0.5, 0.75, 1, 1.25, 1.5, 2],
            "video_segments": True,
            "max_video_segments": 50,
            "detached_generated_audio": True,
            "audio_fades_max_s": 5,
            "clip_sound_crossfade_values_s": [0, 0.25, 0.5, 1],
        }
        return result

    @app.get("/v1/generation-availability")
    def get_generation_availability(request: Request):
        principal = request.state.principal
        if principal.machine and not {"jobs:read", "jobs:write"}.intersection(
            principal.scopes
        ):
            raise HTTPException(403, "generation_availability_forbidden")
        from .generation_availability import generation_availability

        return generation_availability(
            settings,
            repo,
            registry=app.state.operator_capacity.registry,
            owner=principal.owner,
        )

    from .project_routes import create_project_router

    app.include_router(create_project_router(app))

    from .asset_routes import register_routes as register_asset_routes

    object_content = register_asset_routes(
        app,
        services=services,
        clock=lambda: time.time(),
        cache_scope_key=ARTIFACT_VIDEO_CACHE_SCOPE_KEY,
    )

    from .generation_routes import register_routes as register_generation_routes

    register_generation_routes(
        app,
        services=services,
        object_content=object_content,
        capabilities_provider=lambda: capabilities(settings),
    )

    # Helpers for the batch/worker integration without exposing internal state to clients.
    app.state.scope = scope
    app.state.authorized_project = authorized_project
    app.state.owned_plan = owned_plan
    app.state.public_job = public_job
    from .application_services import create_quick_chat
    from .quick_chat_routes import register_routes as register_quick_chat_routes

    chat = create_quick_chat(
        services,
        hooks=quick_chat_hooks,
        assistant=assistant,
        assistant_enabled=assistant_enabled,
        title_generator=title_generator,
    )
    register_quick_chat_routes(app, service=chat)
    from .quick_chat_recovery import QuickChatRecovery

    app.state.quick_chat_recovery = QuickChatRecovery(app.state.quick_chat)
    register_agent_connect_routes(app)
    from . import enhance_routes, worker_boot

    worker_boot.register_routes(app)
    enhance_routes.register_routes(app)
    from .batches import register_routes

    register_routes(app)
    from .guided import register_routes as register_guided_routes

    register_guided_routes(app)
    from .script_windows_service import register_routes as register_script_windows

    register_script_windows(app, client=story_chat_client)
    from .story_chat_routes import register_story_chat_routes

    register_story_chat_routes(
        app,
        client=story_chat_client,
        image_client=story_chat_image_client,
        billing=story_chat_billing,
    )
    from .speech_routes import register_routes as register_speech_routes

    register_speech_routes(app)
    from .voice_design_routes import register_routes as register_voice_design

    register_voice_design(app)
    from .storyboard_versions import register_routes as register_storyboard_routes

    register_storyboard_routes(app)
    from .film_routes import register_film_routes

    register_film_routes(
        app,
        enqueue=repo.generation_transport.enqueue_film
        if repo.generation_transport is not None
        else None,
    )
    from .agent_discovery import register_routes as register_agent_discovery_routes

    register_agent_discovery_routes(app)
    from .operator_routes import register_routes as register_operator_routes
    from .runtime_catalog import public_catalog

    registry = operator_registry
    if registry is None:
        registry = OperatorRegistry.from_environment(
            catalog=public_catalog, repository=app.state.repository
        )
    register_operator_routes(app, registry=registry)
    from .dstack_routes import register_routes as register_dstack_routes

    register_dstack_routes(app, service=dstack_operator)
    from .operator_budget_routes import register_routes as register_budget_routes

    register_budget_routes(app)
    from .operator_billing_routes import register_routes as register_billing_routes

    register_billing_routes(app)
    from .spending_routes import register_routes as register_spending_routes

    register_spending_routes(app)
    from .operator_jobs import register_routes as register_operator_jobs

    register_operator_jobs(app)
    from .support_view import register_routes as register_support_view

    register_support_view(app)
    from .generation_stats import register_routes as register_generation_stats

    register_generation_stats(app)
    from .review_routes import register_routes as register_review_routes

    register_review_routes(app)
    from .operator_host_routes import register_routes as register_operator_host_routes

    register_operator_host_routes(app)
    from .identity_admin_routes import register_routes as register_identity_admin_routes

    register_identity_admin_routes(app)
    from .staging_qa import register_routes as register_staging_qa_routes

    register_staging_qa_routes(app)
    from .workos_routes import register_routes as register_workos_routes

    from .application_services import create_accounts

    accounts = create_accounts(services, app.state.quick_chat)
    register_workos_routes(
        app,
        cookie=COOKIE,
        browser_handle_seconds=BROWSER_HANDLE_SECONDS,
        service=accounts.workos,
    )
    from .account_session_routes import register_routes as register_session_routes

    register_session_routes(app, cookie=COOKIE, service=accounts.sessions)
    from .account_deletion_routes import register_routes as register_deletion_routes

    register_deletion_routes(app, cookie=COOKIE, service=accounts.deletion)
    from .credits_routes import register_routes as register_credit_routes

    register_credit_routes(app)
    from .content_routes import register_routes as register_content_routes
    from .content_policy import reviewed_terms

    register_content_routes(
        app, settings, repo, lambda request: request.state.principal
    )
    from .payments_routes import register_routes as register_payment_routes

    register_payment_routes(
        app,
        settings,
        repo,
        lambda request: request.state.principal,
        terms_check=reviewed_terms,
    )
    from .frontend import register_routes as register_frontend_routes

    register_frontend_routes(app)
    # Last-added middleware is outermost, including the authentication guard.
    app.add_middleware(RequestAdmissionMiddleware, admission=request_admission)
    from .alert_http import AlertCountersMiddleware

    app.add_middleware(AlertCountersMiddleware)
    from .universal_trash import register_routes as register_trash_routes

    register_trash_routes(app)
    from .library import register_routes as register_library_routes

    register_library_routes(app)
    from .http_boundaries import install as install_http_boundaries

    install_http_boundaries(app)
    app.add_middleware(GrantPreviewPrivacyMiddleware)
    from .frontend import register_not_found_route

    register_not_found_route(app)
    return app
