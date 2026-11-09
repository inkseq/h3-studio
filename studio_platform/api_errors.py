"""Existing HTTP error vocabulary and status mapping."""

from __future__ import annotations
import json
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from .assets import AssetNotFound
from .repository import NotFound, Conflict, BudgetExceeded
from .media import MediaBusy
from .storage import StorageError


def register_handlers(app, *, repository, settings):
    repo = repository

    @app.exception_handler(NotFound)
    @app.exception_handler(AssetNotFound)
    async def missing(_request, _error):
        return JSONResponse({"detail": "资源不存在或无权访问"}, status_code=404)

    from .maintenance_notice import MaintenanceInProgress

    @app.exception_handler(MaintenanceInProgress)
    async def maintenance(_request, error):
        # Nothing was submitted or charged; the UI shows the expected end.
        import math
        import time

        wait = (error.expected_end_at or 0) - time.time()
        messages = {
            "en": "A release is in progress. Nothing was submitted or charged; please try again after it ends.",
            "zh": "系统正在更新，本次未提交也未扣费，请在更新结束后重试。",
        }
        return JSONResponse(
            {
                "code": error.code,
                "expected_end_at": error.expected_end_at,
                "message": messages["en"],
                "messages": messages,
            },
            status_code=503,
            headers={
                "Retry-After": str(min(3600, max(30, math.ceil(wait)))),
                "Cache-Control": "no-store",
            },
        )

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        from .quality_http import error_response

        code = str(error)
        guidance = {
            "historical_recipe_unknown": {
                "en": "This historical version has no immutable authoring recipe. Keep the current draft; do not reconstruct it from today's profiles.",
                "zh": "此历史版本没有不可变配方快照，请保留当前草稿，不要从当前档案猜测旧配方。",
            },
            "storyboard_defaults_changed": {
                "en": "Generation defaults changed after preflight. Review the draft and explicitly preflight again; accepted work is unchanged.",
                "zh": "生成默认设置在预检后已改变，请核对草稿并明确重新预检；已接收任务不变。",
            },
            "connection_paused": {
                "en": "This assistant is paused. Ask the owner to resume it; accepted work is preserved.",
                "zh": "此 AI 助手已暂停，请让所有者恢复连接；已接收任务保持不变。",
            },
            "connection_unavailable": {
                "en": "This connection is expired or revoked. Ask the owner to review access; do not retry paid work with a new key automatically.",
                "zh": "连接已过期或撤销，请让所有者检查授权；不要自动换新密钥重试付费操作。",
            },
            "connection_permission_denied": {
                "en": "This connection can organise drafts but cannot perform paid work. Ask the owner for a generation grant.",
                "zh": "此连接可整理草稿，但不能执行付费操作；如需生成，请让所有者授予生成权限。",
            },
            "credit_limits_review_required": {
                "en": "The owner must review and save credit limits before this previously limited connection or account can generate. Existing safeguards have not been removed.",
                "zh": "此账户或连接原有消费限制，请让所有者先核对并保存积分限额，再继续生成；原有保障不会自动移除。",
            },
        }
        if code in guidance:
            return JSONResponse(
                {
                    "code": code,
                    "message": guidance[code]["en"],
                    "messages": guidance[code],
                },
                status_code=409,
            )
        return error_response(_request, app, {"detail": code}, status_code=409)

    @app.exception_handler(BudgetExceeded)
    async def quota(_request, error):
        # Closed monetary vocabulary: capacity failures must not masquerade as
        # a depleted account, and exception strings must never expose secrets.
        code = str(error)
        from .agent_management import record_refusal

        record_refusal(
            repo.engine,
            settings.tenant_id,
            getattr(_request.state, 'principal', None),
            code,
            repo.clock(),
        )
        if code in {"tenant_daily_limit_reached", "tenant_daily_usage_unconfirmed"}:
            return JSONResponse(
                {"code": code}, status_code=429, headers={"Cache-Control": "no-store"}
            )
        if code in {
            "user_daily_limit_reached",
            "connection_daily_limit_reached",
            "spending_usage_unconfirmed",
        }:
            messages = {
                "user_daily_limit_reached": {
                    "en": "Your daily spending limit has been reached.",
                    "zh": "已达到你的每日消费上限。",
                },
                "connection_daily_limit_reached": {
                    "en": "This assistant's daily spending limit has been reached.",
                    "zh": "已达到此 AI 助手的每日消费上限。",
                },
                "spending_usage_unconfirmed": {
                    "en": "Spending is not yet confirmed; reconcile the existing charge first.",
                    "zh": "当前费用尚未确认，请先核对已有费用。",
                },
            }[code]
            return JSONResponse(
                {
                    "code": code,
                    "message_key": "errors." + code,
                    "messages": messages,
                    "message": messages["en"],
                },
                status_code=429,
            )
        if code in {
            "budget_exceeded",
            "budget_below_committed",
            "budget_not_configured",
        }:
            messages = {
                "budget_exceeded": "额度不足；可用额度不足以预留本次操作，未新增付费执行。",
                "budget_below_committed": "额度不能低于已花费用与当前预留之和。",
                "budget_not_configured": "本次操作的预算尚未配置，未新增付费执行。",
            }
            return JSONResponse(
                {"code": code, "detail": messages[code], "message": messages[code]},
                status_code=429,
            )
        return JSONResponse(
            {"detail": "容量不足，未新增付费执行", "code": "capacity_unavailable"},
            status_code=429,
        )

    @app.exception_handler(ValueError)
    async def invalid(_request, error):
        from .quality_http import error_response

        if _request.url.path == "/v1/generations/preflight":
            from .generation_diagnostics import preflight_diagnostic

            diagnostic = preflight_diagnostic(error)
            return error_response(
                _request,
                app,
                {"detail": diagnostic["error_message"], **diagnostic},
                status_code=422,
            )
        return error_response(_request, app, {"detail": str(error)}, status_code=422)

    from .guided_schema import ActionShapeError

    @app.exception_handler(ActionShapeError)
    async def public_action_shape_error(_request, error):
        # This type contains closed operation/field names only, never a supplied
        # model/profile or arbitrary exception. Keep actionable edit guidance.
        return JSONResponse({"detail": str(error)}, status_code=422)

    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    from .quality_http import error_response

    @app.exception_handler(HTTPException)
    async def public_http_error(request, error):
        return error_response(
            request,
            app,
            {"detail": error.detail},
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def public_validation_error(request, error):
        original = await request_validation_exception_handler(request, error)
        return error_response(
            request, app, json.loads(original.body), status_code=original.status_code
        )

    from .quality_tiers import QualitySelectionError

    @app.exception_handler(QualitySelectionError)
    async def quality_selection_error(_request, error):
        return JSONResponse(
            {"code": error.code, "detail": error.detail},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(MediaBusy)
    async def media_busy(_request, _error):
        return JSONResponse(
            {"detail": "素材处理繁忙；原件已保留，请在素材列表恢复同一素材"},
            status_code=503,
            headers={"Retry-After": "5", "Cache-Control": "no-store"},
        )

    @app.exception_handler(StorageError)
    async def storage_error(_request, _error):
        return JSONResponse(
            {"detail": "素材存储不可用，未创建生成任务"}, status_code=503
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        # SQLAlchemy errors may include a DSN or user data. Do not serialize them.
        return JSONResponse(
            {"detail": "持久存储暂时不可用，请保留当前编辑后重试"}, status_code=503
        )
