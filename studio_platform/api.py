"""Versioned video business API. No import of the legacy single-worker server."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote
import uuid

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .assets import AssetService, AssetNotFound
from .media_renditions import public_renditions, RENDITIONS
from .upload_route import AssetUploadRoute
from .auth import Auth, AuthenticationError, LoginLimited
from .capabilities import VERSION, capabilities
from .media import MediaError, MediaBusy
from .project_validation import validate_project
from .repository import Repository, NotFound, Conflict, BudgetExceeded, artifacts, jobs
from .settings import Settings
from .storage import (LocalObjectStore, StorageError, S3ObjectStore,
                      VIDEO_SIGNING_BUCKET_SECONDS, VIDEO_SIGNING_VALIDITY_SECONDS)
from .storage_config import S3StorageConfig, load_configured_storage_credentials
from .http_limits import (ADMISSION_SCOPE_KEY, BodyLimitMiddleware, UploadAdmission,
                         RequestAdmission, RequestAdmissionMiddleware, admission_rejected, is_asset_upload, body_limit)
from .execution_policy import ExecutionPolicies
from .render_plans import RECIPE as RENDER_RECIPE, compile_render
from .agent_discovery import PUBLIC_PATHS as AGENT_PUBLIC_PATHS, DISCOVERY_LINK
from .agent_connect_routes import EXCHANGE_PATH, register_routes as register_agent_connect_routes
from .frontend import FRONTEND_CONTRACT, STATIC_CACHE_SCOPE_KEY, is_public_frontend
from .generation_admission import GenerationAdmission, reject_managed
from .generation_services import GenerationAccess, GenerationPlanning, GenerationRead
from .operator_capacity import OperatorRegistry  # Register additive tables before create_schema.
from . import capacity_market  # Read-only market cache; never authorizes capacity.
from . import dstack_workspace  # Account-owned presentation metadata before schema creation.

COOKIE = "sixnine_session"
# The opaque handle can outlive its authority: every request checks the SQL
# seven-day inactivity expiry. Only login writes the cookie, so a late response
# from an older account cannot overwrite a newer login or restore a logout.
BROWSER_HANDLE_SECONDS = 365 * 86400
TERMINAL = {"succeeded", "failed", "cancelled"}
ARTIFACT_VIDEO_CACHE_SCOPE_KEY = "inkseq.immutable-artifact-video-cache"


def create_app(settings: Settings, *, repository=None, storage=None, quick_chat_hooks=None,
               assistant=None, assistant_enabled=False, operator_registry=None, title_generator=None,
               dstack_operator=None):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    repo = repository or Repository(settings.database_url)
    repo.create_schema()
    if storage is None:
        if settings.storage_provider == "local":
            storage = LocalObjectStore(settings.data_dir / "objects")
        elif settings.storage_provider == "r2":
            import os
            cfg = S3StorageConfig("r2", settings.storage_endpoint, settings.storage_region,
                settings.storage_bucket, "cloudflare-r2", settings.storage_profile, enabled=True)
            credentials = load_configured_storage_credentials(cfg, credentials_file=settings.storage_credential_file,
                registry_root=os.environ.get("AI_REGISTRY_ROOT"))
            storage = S3ObjectStore(cfg, credentials)
        else:
            raise ValueError("此远端存储需要显式注入已审阅的store与凭据入口；不使用默认AWS账户")
    auth = Auth(repo.engine, tenant=settings.tenant_id, mode=settings.auth_mode, session_seconds=settings.session_seconds)
    asset_service = AssetService(repo.engine, storage, settings.data_dir,
        tenant=settings.tenant_id, max_bytes=settings.max_upload_bytes)
    execution_policies = ExecutionPolicies(settings, repo)
    upload_admission = UploadAdmission()
    request_admission = RequestAdmission()

    @asynccontextmanager
    async def lifespan(app):
        await app.state.quick_chat_recovery.start()
        try:
            yield
        finally:
            await app.state.quick_chat_recovery.close()
            capacity = getattr(getattr(app.state, "dstack_operator", None), "capacity", None)
            if capacity is not None:
                capacity.client.close()
            if repository is None:
                repo.close()

    app = FastAPI(title="Inkseq video platform", version="1.0.0-dev", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, project_bytes=settings.max_project_bytes,
                       upload_bytes=settings.max_upload_bytes)
    app.state.repository, app.state.auth, app.state.storage = repo, auth, storage
    app.state.assets, app.state.settings = asset_service, settings
    app.state.upload_admission = upload_admission
    app.state.request_admission = request_admission
    generation_access = GenerationAccess(repo, settings.tenant_id)
    generation_read = GenerationRead(repo, generation_access)
    generation_planning = GenerationPlanning(repo=repo, assets=asset_service, settings=settings,
        policies=execution_policies, access=generation_access, read=generation_read)
    app.state.generation_access = generation_access
    app.state.generation_read = generation_read
    app.state.generation_planning = generation_planning
    # Compatibility names used by existing scenario and batch routes now point
    # to HTTP-independent services, rather than closures over this app factory.
    scope, project_scope = generation_access.scope, generation_access.project_scope
    authorized_project, owned_plan, owned_job = generation_access.project, generation_access.plan, generation_access.job
    project_response, public_execution, public_job = generation_read.project, generation_read.execution, generation_read.public_job

    @app.exception_handler(NotFound)
    @app.exception_handler(AssetNotFound)
    async def missing(_request, _error):
        return JSONResponse({"detail": "资源不存在或无权访问"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        code = str(error)
        guidance = {
            "historical_recipe_unknown": {"en": "This historical version has no immutable authoring recipe. Keep the current draft; do not reconstruct it from today's profiles.", "zh": "此历史版本没有不可变配方快照，请保留当前草稿，不要从当前档案猜测旧配方。"},
            "storyboard_defaults_changed": {"en": "Generation defaults changed after preflight. Review the draft and explicitly preflight again; accepted work is unchanged.", "zh": "生成默认设置在预检后已改变，请核对草稿并明确重新预检；已接收任务不变。"},
            "connection_paused": {"en": "This assistant is paused. Ask the owner to resume it; accepted work is preserved.", "zh": "此 AI 助手已暂停，请让所有者恢复连接；已接收任务保持不变。"},
            "connection_unavailable": {"en": "This connection is expired or revoked. Ask the owner to review access; do not retry paid work with a new key automatically.", "zh": "连接已过期或撤销，请让所有者检查授权；不要自动换新密钥重试付费操作。"},
            "connection_permission_denied": {"en": "This connection can organise drafts but cannot perform paid work. Ask the owner for a generation grant.", "zh": "此连接可整理草稿，但不能执行付费操作；如需生成，请让所有者授予生成权限。"},
        }
        if code in guidance:
            return JSONResponse({"code": code, "message": guidance[code]["en"], "messages": guidance[code]}, status_code=409)
        return JSONResponse({"detail": code}, status_code=409)

    @app.exception_handler(BudgetExceeded)
    async def quota(_request, error):
        # Closed monetary vocabulary: capacity failures must not masquerade as
        # a depleted account, and exception strings must never expose secrets.
        code = str(error)
        from .agent_management import record_refusal
        record_refusal(repo.engine,settings.tenant_id,getattr(_request.state,'principal',None),code,repo.clock())
        if code in {"user_daily_limit_reached", "connection_daily_limit_reached", "spending_usage_unconfirmed"}:
            messages = {"user_daily_limit_reached": {"en": "Your daily spending limit has been reached.", "zh": "已达到你的每日消费上限。"},
                "connection_daily_limit_reached": {"en": "This assistant's daily spending limit has been reached.", "zh": "已达到此 AI 助手的每日消费上限。"},
                "spending_usage_unconfirmed": {"en": "Spending is not yet confirmed; reconcile the existing charge first.", "zh": "当前费用尚未确认，请先核对已有费用。"}}[code]
            return JSONResponse({"code": code, "message_key": "errors." + code,
                "messages": messages, "message": messages["en"]}, status_code=429)
        if code in {"budget_exceeded", "budget_below_committed", "budget_not_configured"}:
            messages = {"budget_exceeded": "额度不足；可用额度不足以预留本次操作，未新增付费执行。",
                "budget_below_committed": "额度不能低于已花费用与当前预留之和。",
                "budget_not_configured": "本次操作的预算尚未配置，未新增付费执行。"}
            return JSONResponse({"code": code, "detail": messages[code], "message": messages[code]}, status_code=429)
        return JSONResponse({"detail": "容量不足，未新增付费执行", "code": "capacity_unavailable"}, status_code=429)

    @app.exception_handler(ValueError)
    async def invalid(_request, error):
        if _request.url.path == "/v1/generations/preflight":
            from .generation_diagnostics import preflight_diagnostic
            diagnostic = preflight_diagnostic(error)
            return JSONResponse({"detail": diagnostic["error_message"], **diagnostic}, status_code=422)
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(MediaBusy)
    async def media_busy(_request, _error):
        return JSONResponse({"detail": "素材处理繁忙；原件已保留，请在素材列表恢复同一素材"},
                            status_code=503, headers={"Retry-After": "5", "Cache-Control": "no-store"})

    @app.exception_handler(StorageError)
    async def storage_error(_request, _error):
        return JSONResponse({"detail": "素材存储不可用，未创建生成任务"}, status_code=503)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        # SQLAlchemy errors may include a DSN or user data. Do not serialize them.
        return JSONResponse({"detail": "持久存储暂时不可用，请保留当前编辑后重试"}, status_code=503)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        request_public_origin = settings.public_origin_for_host(host)
        if settings.public_origin:
            if request_public_origin is None and not (request.url.path == "/healthz" and host.split(":")[0] in {"localhost", "127.0.0.1"}):
                return JSONResponse({"detail": "无效访问域名"}, status_code=400)
        elif host.split(":")[0] not in {"localhost", "127.0.0.1", "testserver"}:
            return JSONResponse({"detail": "本地服务仅支持loopback入口"}, status_code=400)
        maximum = body_limit(request.url.path, settings.max_project_bytes, settings.max_upload_bytes)
        length = request.headers.get("content-length")
        if length and (len(length) > 20 or not length.isascii() or not length.isdigit() or int(length) > maximum):
            return JSONResponse({"detail": "请求体超过限制"}, status_code=413)
        principal = None
        authorization = request.headers.get("authorization", "")
        lease = request.scope[ADMISSION_SCOPE_KEY]
        if not lease.acquire("authentication"):
            return admission_rejected()
        try:
            if authorization:
                if authorization.startswith("Bearer "):
                    principal = await run_in_threadpool(auth.bearer, authorization[7:])
                if not principal:
                    return JSONResponse({"detail": "服务身份无效"}, status_code=401)
            else:
                principal = await run_in_threadpool(auth.session, request.cookies.get(COOKIE))
        except SQLAlchemyError:
            # Middleware runs outside FastAPI's endpoint exception handlers.
            # Connection errors must not escape with a DSN or bound token hash.
            return JSONResponse({"detail": "身份服务暂时不可用，请稍后重试"}, status_code=503,
                                headers={"Cache-Control": "no-store", "Retry-After": "3"})
        finally:
            lease.release("authentication")
        expected_account = request.headers.get("x-expected-account")
        native_content = (request.method in {"GET", "HEAD"}
            and re.fullmatch(r"/v1/(?:assets|artifacts)/[^/]+/content", request.url.path) is not None)
        if native_content and "expected_account" in request.query_params:
            # Native media/navigation cannot send the browser's custom header.
            # This hint fences cookie identity only; it never selects an owner
            # or grants a machine client access to a different project/account.
            accounts = request.query_params.getlist("expected_account")
            header_accounts = request.headers.getlist("x-expected-account")
            if (len(accounts) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", accounts[0])
                    or len(header_accounts) > 1
                    or expected_account is not None and expected_account != accounts[0]):
                return JSONResponse({"detail": "账户校验参数无效或互相冲突",
                                     "code": "invalid_account_context"}, status_code=400,
                                    headers={"Cache-Control": "no-store"})
            expected_account = accounts[0]
        if (principal and not principal.machine and request.url.path.startswith("/v1/")
                and expected_account is not None and expected_account != principal.owner):
            # Cookies are shared across tabs and ports. An old tab must not
            # accidentally apply its draft to a new owner's same project ID.
            return JSONResponse({"detail": "登录账户已改变，已阻止本次操作；请先核对当前账户",
                                 "code": "account_context_changed"}, status_code=409,
                headers={"Cache-Control": "no-store", "X-Authenticated-Account": principal.owner})
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not (principal and principal.machine):
            origin = request.headers.get("origin")
            expected = request_public_origin or str(request.base_url).rstrip("/")
            allowed_origins = {expected, *settings.local_ui_origins}
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "跨站写入请求被拒绝"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "跨站写入请求被拒绝"}, status_code=403)
        public_paths = {"/healthz", "/api/auth/config", "/api/auth/login"}
        public_frontend = is_public_frontend(settings, request.method, request.url.path)
        public_discovery = request.method in {"GET", "HEAD"} and request.url.path in AGENT_PUBLIC_PATHS
        public_exchange = request.method == "POST" and request.url.path == EXCHANGE_PATH
        if request.url.path not in public_paths and not public_frontend and not public_discovery and not public_exchange and not principal:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        request.state.principal = principal
        if principal and not lease.acquire("owner", principal.owner):
            return admission_rejected()
        downloading = (principal is not None and request.method in {"GET", "HEAD"}
            and request.url.path.endswith("/content")
            and request.url.path.startswith(("/v1/artifacts/", "/v1/assets/")))
        if downloading:
            # Only the strict small-copy selector enters this gate. The route
            # still authorizes its original owner/project and verifies the
            # <=64KiB role; invalid/missing selectors never serve originals.
            thumbnail = request.query_params.getlist("rendition") == ["thumb"]
            global_gate, owner_gate = (("thumbnails", "owner_thumbnails") if thumbnail
                                      else ("downloads", "owner_downloads"))
            if not lease.acquire(global_gate) or not lease.acquire(owner_gate, principal.owner):
                return admission_rejected()
        uploading = request.method == "POST" and is_asset_upload(request.url.path) and principal is not None
        if uploading and not upload_admission.acquire(principal.owner):
            return JSONResponse({"detail": "同时上传的文件已达到上限，请等当前上传完成再重试"}, status_code=429,
                                headers={"Retry-After": "3"})
        if uploading:
            lease.release_when_finished(lambda: upload_admission.release(principal.owner))
        response = await call_next(request)
        response.headers["Cache-Control"] = (request.scope.get(STATIC_CACHE_SCOPE_KEY, "no-store")
            if response.status_code < 400 else "no-store")
        video_max_age = request.scope.get(ARTIFACT_VIDEO_CACHE_SCOPE_KEY)
        if (principal is not None and request.method == "GET" and response.status_code == 307
                and getattr(request.scope.get("route"), "path", None) == "/v1/artifacts/{artifact_id}/content"
                and type(video_max_age) is int and 0 <= video_max_age <= VIDEO_SIGNING_BUCKET_SECONDS):
            response.headers["Cache-Control"] = f"private, max-age={video_max_age}"
            vary = [value.strip() for value in response.headers.get("Vary", "").split(",") if value.strip()]
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
        return {"status": "ok", "generation_enabled": settings.generation_enabled,
                "execution_backend": settings.execution_backend, "auth_ready": auth.ready(),
                "render_enabled": settings.render_enabled,
                "frontend_contract": FRONTEND_CONTRACT,
                "cloud_creation_enabled": False}

    @app.get("/api/auth/config")
    def auth_config():
        return {"authentication": "password" if settings.auth_mode == "password" else "username-only-test",
                "auth_ready": auth.ready(), "users": ["superdan", "supervan"] if settings.auth_mode == "local-test" else []}

    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("username"), str) or not isinstance(body.get("password", ""), str):
            raise HTTPException(422, "登录信息格式无效")
        try:
            token = await run_in_threadpool(auth.login, body["username"], body.get("password", ""), request.client.host if request.client else "unknown")
        except LoginLimited as error:
            raise HTTPException(429, str(error), headers={"Retry-After": str(error.retry_after)}) from None
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from None
        response = JSONResponse({"username": body["username"], "authentication": settings.auth_mode})
        response.set_cookie(COOKIE, token, max_age=BROWSER_HANDLE_SECONDS, httponly=True,
            secure=bool(settings.public_origin), samesite="lax", path="/")
        return response

    @app.get("/api/auth/me")
    def whoami(request: Request):
        principal = request.state.principal
        return {"username": principal.owner, "actor_id": principal.actor_id, "machine": principal.machine,
                "authentication": settings.auth_mode, "scopes": list(principal.scopes),
                "features": {"storyboard_workspace": not principal.machine and principal.owner in settings.storyboard_owners},
                "operator_capacity": {name: not principal.machine and principal.owner in settings.operator_capacity_owners
                    for name in ("view", "start", "drain", "stop", "update_policy")} | {
                    "admin": not principal.machine and principal.owner in settings.operator_admin_owners},
                "all_projects": principal.all_projects, "project_ids": list(principal.project_ids)}

    @app.post("/api/auth/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/v1/capabilities")
    def get_capabilities():
        result = capabilities(settings)
        result["upload_max_bytes"] = settings.max_upload_bytes
        result["chapter_render"] = {"recipe_id": RENDER_RECIPE, "implemented": True,
            "enabled": settings.render_enabled, "resolutions": ["480P", "720P"],
            "aspects": ["16:9", "9:16", "1:1", "4:3", "3:4"], "fps": 24,
            "max_shots": 50, "max_duration": 600, "max_audio_tracks": 32,
            "video_original_audio": False, "subtitles": True, "transitions": False,
            "source_trim": True, "generated_audio": True, "render_contract_version": 3}
        return result

    @app.get("/v1/generation-availability")
    def get_generation_availability(request: Request):
        principal = request.state.principal
        if principal.machine and not {"jobs:read", "jobs:write"}.intersection(principal.scopes):
            raise HTTPException(403, "generation_availability_forbidden")
        from .generation_availability import generation_availability
        return generation_availability(settings, repo, registry=app.state.operator_capacity.registry)

    @app.get("/v1/projects")
    def list_projects(request: Request, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000)):
        principal = request.state.principal
        allowed = ((None if principal.all_projects else principal.project_ids) if "projects:read" in principal.scopes else ()) if principal.machine else None
        values = repo.list_documents(project_scope(principal), "project", limit=limit, offset=offset,
                                     allowed_ids=allowed, summary=True, exclude_managed=True)
        return {"projects": [{"id": v["document_id"], "title": v["payload"]["title"],
                              "version": v["version"], "updated_at": v["updated_at"]}
                for v in values if principal.allows(v["document_id"], "projects:read")]}

    @app.post("/v1/projects", status_code=201)
    def create_project(request: Request, body: dict, idempotency_key: str | None = Header(None)):
        principal = request.state.principal
        if principal.machine and not (principal.all_projects and "projects:create" in principal.scopes):
            raise HTTPException(403, "创建故事需要projects:create及全部本人项目授权")
        from .guided import empty_project
        if "project" in body:
            if set(body) != {"project"}:
                raise HTTPException(422, "导入故事仅接受project字段")
            project = body["project"]
        else:
            values = dict(body)
            if idempotency_key and "id" not in values:
                values["id"] = "project-"+uuid.uuid5(uuid.NAMESPACE_URL,
                    f"{settings.tenant_id}:{principal.owner}:{principal.actor_id}:{idempotency_key}").hex
            project = empty_project(values)
        validate_project(project, settings.max_project_bytes)
        reject_managed(project)
        return app.state.guided.mutate(principal, project["id"], project, idempotency_key, create=True)

    @app.get("/v1/projects/{project_id}")
    def get_project(project_id: str, request: Request):
        from .universal_trash import active_target
        with repo.engine.connect() as connection:
            if active_target(connection,settings.tenant_id,request.state.principal.owner,
                    {"kind":"project","project_id":project_id,"id":project_id}):
                raise HTTPException(404,"project_in_trash")
        record=authorized_project(request.state.principal, project_id)
        if record["payload"].get("content_deleted"):
            raise HTTPException(404, "project_not_found")
        from .agent_management import doc_where,ATTRIBUTION_PROJECT
        from .repository import documents
        with repo.engine.connect() as conn:
            rows=conn.execute(select(documents.c.payload).where(documents.c.tenant_id==settings.tenant_id,
                documents.c.owner_id==request.state.principal.owner,documents.c.project_id==ATTRIBUTION_PROJECT,
                documents.c.kind=='created_by',documents.c.payload['project_id'].as_string()==project_id)).scalars()
            attribution={r['resource_id']:{k:r[k] for k in ('kind','connection_id','name')} for r in rows}
        return {**project_response(record),'created_by':attribution}

    @app.put("/v1/projects/{project_id}")
    def save_project(project_id: str, request: Request, body: dict):
        principal = request.state.principal
        if principal.machine and not principal.allows(project_id, "projects:write"):
            raise HTTPException(403, "保存故事需要projects:write授权")
        reject_managed(authorized_project(principal, project_id, "projects:write")["payload"])
        project = validate_project(body.get("project"), settings.max_project_bytes)
        reject_managed(project)
        if project["id"] != project_id or type(body.get("expected_version")) is not int:
            raise HTTPException(422, "需要匹配的项目ID与版本")
        return app.state.guided.save(principal, project_id, project, body["expected_version"])

    def upload_asset(request: Request, file: UploadFile = File(...), client_project_id: str = Form(...), client_asset_id: str | None = Form(None)):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "assets:write")
        return asset_service.upload(principal.owner, client_project_id, file.file, file.filename or "file", client_asset_id=client_asset_id)

    app.router.add_api_route("/v1/assets", upload_asset, methods=["POST"],
        status_code=201, route_class_override=AssetUploadRoute)

    @app.get("/v1/assets")
    def list_assets(request: Request, client_project_id: str):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "assets:read")
        return {"assets": asset_service.list(principal.owner, client_project_id)}

    @app.get("/v1/storage-usage")
    def storage_usage(request: Request):
        if request.state.principal.machine:
            raise HTTPException(403, "服务身份不能查询账户整体存储用量")
        return asset_service.usage(request.state.principal.owner)

    @app.get("/v1/assets/{asset_id}")
    def get_asset(asset_id: str, request: Request):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:read")
        return asset_service.public(value)

    @app.post("/v1/assets/{asset_id}/resume")
    def resume_asset(asset_id: str, request: Request):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:write")
        # Browser callers cannot override an active/stale worker lease. They
        # resume the same durable receipt, never create a fresh storage key.
        return asset_service.resume(principal.owner, asset_id)

    def object_content(key, mime, filename, request, *, download=False, immutable_video=False, immutable_rendition=False):
        headers = {"Accept-Ranges": "bytes", "Content-Disposition": ("attachment" if download else "inline")+"; filename*=UTF-8''"+quote(filename, safe="")}
        if not isinstance(storage, LocalObjectStore):
            if request.method == "HEAD":
                # A GET presign cannot authenticate a redirected HEAD request.
                info = storage.stat(key)
                headers["Content-Length"] = str(info.size_bytes)
                return StreamingResponse(iter(()), media_type=mime, headers=headers)
            options = {"download_filename": filename} if download else {}
            if (immutable_video or immutable_rendition) and isinstance(storage, S3ObjectStore) and storage.config.provider == "r2":
                signer = storage.presign_immutable_rendition if immutable_rendition else storage.presign_immutable_video
                url = signer(key, content_type=mime, **options)
                now = time.time()
                # Use the issued URL's bucket, including a rollover during signing.
                bucket_end = url.expires_at - VIDEO_SIGNING_VALIDITY_SECONDS + VIDEO_SIGNING_BUCKET_SECONDS
                if immutable_video:
                    request.scope[ARTIFACT_VIDEO_CACHE_SCOPE_KEY] = max(0,
                        min(int(url.expires_at - now) - 600, int(bucket_end - now)))
            else:
                url = storage.presign_download(key, expires_seconds=300, **options)
            return RedirectResponse(url.reveal(), status_code=307,
                headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
        info = storage.stat(key)
        start, end, status = 0, info.size_bytes-1, 200
        requested_range = request.headers.get("range")
        if requested_range:
            match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", requested_range)
            if not match or not any(match.groups()):
                raise HTTPException(416, "无效字节范围", headers={"Content-Range": f"bytes */{info.size_bytes}"})
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right) if right else end, end)
            else:
                start = max(0, info.size_bytes-int(right))
            if start > end or start >= info.size_bytes:
                raise HTTPException(416, "字节范围越界", headers={"Content-Range": f"bytes */{info.size_bytes}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{info.size_bytes}"
        headers["Content-Length"] = str(end-start+1)
        def stream():
            with storage.open(key) as source:
                # Starlette's threaded iterator may remain suspended at yield
                # after a disconnected/expired response. The outer ASGI lease
                # deterministically closes the handle instead of relying on
                # generator garbage collection or a success-only background task.
                request.scope[ADMISSION_SCOPE_KEY].release_when_finished(source.close)
                source.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = source.read(min(1024*1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        return StreamingResponse(iter(()) if request.method == "HEAD" else stream(), status_code=status, media_type=mime, headers=headers)

    @app.api_route("/v1/assets/{asset_id}/content", methods=["GET", "HEAD"])
    def asset_content(asset_id: str, request: Request, download: bool = False,
                      rendition: str | None = Query(None),
                      expected_account: str | None = Query(None, description="Optional browser account fence for native media/navigation; does not grant access")):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:read")
        if value["status"] != "ready":
            raise HTTPException(409, "素材尚未就绪")
        if "rendition" in request.query_params:
            if request.query_params.getlist("rendition") != [rendition] or rendition not in RENDITIONS:
                raise HTTPException(400, "invalid_rendition")
            advertised = public_renditions(value).get("renditions", {}).get(rendition)
            if advertised is None:
                raise HTTPException(404, "rendition_not_ready")
            item = value[rendition]
            return object_content(item["key"], advertised["content_type"], f'{asset_id}-{rendition}.webp',
                request, download=download, immutable_rendition=True)
        return object_content(value["original"]["key"], value["mime"], value["file_name"], request, download=download)

    @app.post("/v1/assets/{asset_id}/derivatives", status_code=201)
    def derivative(asset_id: str, request: Request, body: dict):
        principal = request.state.principal
        parent = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, parent["project_id"], "assets:write")
        if set(body) - {"start", "end"}:
            raise HTTPException(422, "仅支持明确的时间选段")
        return asset_service.derive(principal.owner, asset_id, body.get("start"), body.get("end"))

    @app.post("/v1/generation-plans", status_code=201)
    def make_plan(request: Request, body: dict):
        return generation_admission.plan(request.state.principal, body)

    @app.get("/v1/projects/{project_id}/shots/{shot_id}/generation-draft")
    def generation_draft(project_id: str, shot_id: str, request: Request):
        from .generation_draft import read_draft, shot_entity
        record = authorized_project(request.state.principal, project_id)
        project = record["payload"]
        from .storyboard_versions import preferences, effective_project
        defaults = preferences(repo, settings.tenant_id, request.state.principal.owner)
        rendered, provenance = effective_project(project, shot_id, defaults["defaults"])
        draft, issues = read_draft(rendered, shot_id, capabilities=capabilities(settings))
        view = "/freestyle" if project.get("journey", {}).get("workspace") == "freestyle" else "/"
        return {"project_id": project_id, "shot_id": shot_id, "project_version": record["version"],
            "shot_version": shot_entity(project, shot_id)["version"], "draft": draft, "issues": issues,
            "defaults": provenance,
            "web_url": f"{view}?project={quote(project_id, safe='')}&entity={quote(shot_id, safe='')}"}

    @app.post("/v1/projects/{project_id}/shots/{shot_id}/generation-plans", status_code=201)
    def saved_generation_plan(project_id: str, shot_id: str, request: Request, body: dict):
        from .generation_draft import fields
        principal = request.state.principal
        fields(body, {"expected_version", "capabilities_version"}, "草稿预检")
        if type(body.get("expected_version")) is not int or body["expected_version"] < 1:
            raise ValueError("需要当前项目的expected_version")
        if body.get("capabilities_version", VERSION) != VERSION:
            raise ValueError("生成能力已变化，请刷新后重新预检")
        authorized_project(principal, project_id, "projects:read")
        record = authorized_project(principal, project_id, "jobs:write")
        reject_managed(record["payload"])
        if record["version"] != body["expected_version"]:
            raise Conflict("document_version_conflict")
        return generation_admission.preflight(principal, record["payload"], shot_id,
                                              expected_version=body["expected_version"])

    def resolve_render_source(principal, project_id, entity, kind):
        data = entity.get("data", {})
        artifact_id, asset_id = data.get("cloudArtifactId"), data.get("cloudAssetId")
        if artifact_id:
            if not isinstance(artifact_id, str):
                raise NotFound("source_not_found")
            with repo.engine.connect() as connection:
                row = connection.execute(select(artifacts).join(jobs, artifacts.c.job_id == jobs.c.id).where(
                    artifacts.c.id == artifact_id, jobs.c.tenant_id == settings.tenant_id,
                    jobs.c.owner_id == principal.owner, jobs.c.project_id == project_id,
                    jobs.c.status == "succeeded")).mappings().first()
            if row is None:
                raise NotFound("source_not_found")
            metadata = row["metadata"]
            job = owned_job(principal, row["job_id"])
            if metadata.get("kind") != kind or not metadata.get("validated"):
                raise ValueError("成片来源尚未校验")
            return {"kind": kind, "duration": metadata.get("duration_s"),
                "source_job_id": row["job_id"], "artifact_id": row["id"],
                "object": {"key": metadata["object_key"], "size_bytes": metadata["size_bytes"],
                    "sha256": metadata["sha256"], "content_type": metadata.get("content_type", metadata.get("mime"))},
                "simulation": job["execution_plan"].get("backend") == "mock" or job["request"].get("simulation") is True}
        if not isinstance(asset_id, str):
            raise ValueError("需要先把所选本机素材上传到当前云项目")
        try:
            asset = asset_service.get(principal.owner, asset_id, project_id)
        except AssetNotFound:
            raise NotFound("source_not_found") from None
        if asset.get("status") != "ready" or asset.get("kind") != kind:
            raise ValueError("素材尚未就绪或类型不符")
        return {"kind": kind, "duration": asset["metadata"].get("source_duration"),
                "source_job_id": None, "artifact_id": None,
                "object": asset["original"], "simulation": False}

    @app.post("/v1/render-plans", status_code=201)
    def make_render_plan(request: Request, body: dict):
        principal = request.state.principal
        project_id = body.get("client_ref", {}).get("project_id") if isinstance(body.get("client_ref"), dict) else None
        project = authorized_project(principal, project_id, "jobs:write")["payload"]
        reject_managed(project)
        compiled, timeline, blockers, warnings = compile_render(body, project,
            lambda entity, kind: resolve_render_source(principal, project_id, entity, kind))
        compiled["render_blockers"] = blockers
        from .repository import request_hash
        fingerprint = request_hash(compiled)
        admission = execution_policies.evaluate(compiled, scope(principal, project_id), fingerprint)
        plan = repo.create_plan(scope(principal, project_id), compiled, admission.execution,
            expires_at=admission.expires_at, estimated_cost_microusd=0)
        return {"plan_id": plan["id"], "status": "ready" if admission.execution["enabled"] else "blocked",
            "request_hash": plan["request_hash"], "client_ref": compiled["client_ref"],
            "output_spec": compiled["output_spec"], "timeline": timeline,
            "expires_at": plan["expires_at"], "blockers": admission.execution["blockers"],
            "warnings": warnings, "estimate": admission.estimate, "execution": public_execution(admission.execution),
            "simulation": compiled["simulation"]}

    generation_admission = GenerationAdmission(repo=repo, assets=asset_service, settings=settings,
        policies=execution_policies, access=generation_access, planning=generation_planning,
        capabilities_provider=lambda: capabilities(settings))
    app.state.generation_admission = generation_admission

    def create_from_plan(principal, plan_id, idempotency_key, initial_status=None):
        return generation_admission.create(principal, plan_id, idempotency_key,
                                           initial_status=initial_status)

    def enqueue_planned(principal, job, *, confirmation_required=False, spending_settings_version=None):
        return generation_admission.enqueue(principal, job, confirmation_required=confirmation_required, spending_settings_version=spending_settings_version)

    @app.post("/v1/jobs", status_code=202)
    def create_job(request: Request, body: dict, idempotency_key: str = Header(..., alias="Idempotency-Key")):
        if set(body) != {"plan_id"} or not isinstance(body["plan_id"], str):
            raise HTTPException(422, "只接受已确认的plan_id")
        return public_job(create_from_plan(request.state.principal, body["plan_id"], idempotency_key))

    @app.get("/v1/jobs")
    def list_jobs(request: Request, client_project_id: str | None = None, limit: int = Query(100, ge=1, le=100),
                  offset: int = Query(0, ge=0, le=1000000)):
        return generation_read.list_jobs(request.state.principal,
            project_id=client_project_id, limit=limit, offset=offset)

    from .job_events import register_routes as register_job_events
    register_job_events(app)

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        return generation_read.job(request.state.principal, job_id)

    @app.get("/v1/activity-summary")
    def activity_summary(request: Request, client_project_id: str):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "jobs:read")
        from .diagnostics import job_activity
        return job_activity(repo, tenant_id=settings.tenant_id, owner_id=principal.owner,
                            project_id=client_project_id)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        principal = request.state.principal
        job = owned_job(principal, job_id, "jobs:write")
        return public_job(repo.request_cancel(scope(principal, job["project_id"]), job_id))

    @app.get("/v1/jobs/{job_id}/artifacts")
    def job_artifacts(job_id: str, request: Request):
        job = owned_job(request.state.principal, job_id)
        return {"artifacts": public_job(job)["artifacts"]}

    @app.post("/v1/projects/{project_id}/artifacts/{artifact_id}/frames", status_code=201)
    def artifact_frame(project_id: str, artifact_id: str, request: Request, body: dict,
                       idempotency_key: str = Header(..., alias="Idempotency-Key")):
        principal = request.state.principal
        authorized_project(principal, project_id, "assets:write")
        row = generation_access.artifact(principal, artifact_id)
        job = owned_job(principal, row["job_id"])
        if job["project_id"] != project_id:
            raise NotFound("artifact_not_found")
        if job["status"] != "succeeded":
            raise Conflict("artifact_not_ready")
        return asset_service.artifact_frame(principal.owner, project_id, principal.actor_id,
            artifact_id, job["id"], row["metadata"], body, idempotency_key)

    @app.api_route("/v1/artifacts/{artifact_id}/content", methods=["GET", "HEAD"])
    def artifact_content(artifact_id: str, request: Request, download: bool = False,
                         rendition: str | None = Query(None),
                         expected_account: str | None = Query(None, description="Optional browser account fence for native media/navigation; does not grant access")):
        principal = request.state.principal
        row = generation_access.artifact(principal, artifact_id)
        value = row["metadata"]
        if "rendition" in request.query_params:
            from .media_renditions import public_artifact_renditions
            if request.query_params.getlist("rendition") != [rendition] or rendition not in RENDITIONS:
                raise HTTPException(400, "invalid_rendition")
            advertised = public_artifact_renditions(row).get("renditions", {}).get(rendition)
            if advertised is None:
                raise HTTPException(404, "rendition_not_ready")
            item = value["display_renditions"]["objects"][rendition]
            return object_content(item["key"], "image/webp", f'{artifact_id}-{rendition}.webp',
                request, download=download, immutable_rendition=True)
        extension = {"video": "mp4", "audio": "flac", "image": "png"}.get(value.get("kind"), "bin")
        filename = value.get("filename", f'inkseq-{row["job_id"][:8]}-{value.get("kind", "result")}.{extension}')
        if isinstance(filename, str) and filename.lower().startswith("sixnine-"):
            filename = "inkseq-" + filename[len("sixnine-"):]
        immutable_video = (value.get("kind") == "video"
            and value.get("mime", value.get("content_type")) == "video/mp4"
            and isinstance(value.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
            and type(value.get("size_bytes")) is int and value["size_bytes"] > 0)
        return object_content(value["object_key"], value.get("mime", value.get("content_type", "application/octet-stream")),
                              filename, request, download=download, immutable_video=immutable_video)

    # Helpers for the batch/worker integration without exposing internal state to clients.
    app.state.scope = scope
    app.state.authorized_project = authorized_project
    app.state.owned_plan = owned_plan
    app.state.create_from_plan = create_from_plan
    app.state.enqueue_planned = enqueue_planned
    app.state.public_job = public_job
    from .quick_chat import QuickChatHooks
    from .quick_chat_routes import register_routes as register_quick_chat_routes

    def quick_cancel(principal, job_id):
        job = owned_job(principal, job_id, "jobs:write")
        return public_job(repo.request_cancel(scope(principal, job["project_id"]), job_id))

    hooks = quick_chat_hooks or QuickChatHooks(preflight=generation_admission.preflight,
        create_planned=generation_admission.create_planned, enqueue=lambda p, j, confirmation_required=False, spending_settings_version=None: generation_admission.enqueue(p, j, business=True, confirmation_required=confirmation_required, spending_settings_version=spending_settings_version),
        public_job=generation_read.job, cancel=quick_cancel, refresh_planned=generation_admission.refresh_planned)
    register_quick_chat_routes(app, hooks=hooks, assistant=assistant,
        assistant_enabled=assistant_enabled, title_generator=title_generator)
    from .quick_chat_recovery import QuickChatRecovery
    app.state.quick_chat_recovery = QuickChatRecovery(app.state.quick_chat)
    register_agent_connect_routes(app)
    from .batches import register_routes
    register_routes(app)
    from .guided import register_routes as register_guided_routes
    register_guided_routes(app)
    from .storyboard_versions import register_routes as register_storyboard_routes
    register_storyboard_routes(app)
    from .agent_discovery import register_routes as register_agent_discovery_routes
    register_agent_discovery_routes(app)
    from .operator_routes import register_routes as register_operator_routes
    from .runtime_catalog import public_catalog
    registry = operator_registry
    if registry is None:
        registry = OperatorRegistry.from_environment(catalog=public_catalog, repository=app.state.repository)
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
    from .operator_host_routes import register_routes as register_operator_host_routes
    register_operator_host_routes(app)
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
    return app
