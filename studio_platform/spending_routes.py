"""Browser-owned limits and exact-quote confirmations; no provider effects."""
from sqlalchemy import insert, select, update
from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import Principal, personal_keys
from .operator_capacity import OperatorError
from .repository import Conflict, NotFound, plans, spending_settings, spending_confirmations
from .spending_limits import (ADMIN_OWNER, ADMIN_DEFAULTS, SpendingConfirmationRequired,
    lock_owner, read_settings, validate_settings, public_usage, admin_defaults)


def register_routes(app):
    repo, settings = app.state.repository, app.state.settings

    def human(request, *, admin=False):
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal) or principal.machine:
            raise OperatorError("spending_settings_browser_required", 403)
        if admin and principal.owner not in settings.operator_admin_owners:
            raise OperatorError("operator_admin_required", 403)
        return principal

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    def owned_keys(connection, owner):
        return {"key:" + row.id for row in connection.execute(select(personal_keys.c.id).where(
            personal_keys.c.tenant == settings.tenant_id, personal_keys.c.owner == owner))}

    def update_value(principal, owner, body, *, admin=False):
        if not isinstance(body, dict) or set(body) != {"expected_version", "value"} or type(body["expected_version"]) is not int:
            raise ValueError("spending_settings_invalid")
        value = validate_settings(body["value"], admin=admin)
        with repo.transaction() as connection:
            lock_owner(connection, settings.tenant_id, owner)
            old = read_settings(connection, settings.tenant_id, owner,
                admin_defaults=admin_defaults(getattr(app.state, "dstack_operator", None)) if admin else None)
            if body["expected_version"] != old["version"]:
                raise Conflict("spending_settings_changed")
            if not admin and set(value["connection_limits"]) - owned_keys(connection, principal.owner):
                raise NotFound("connection_not_found")
            if old["value"] == value:
                return {**old, "changed": False}
            values = {"value": value, "version": old["version"] + 1,
                "actor_id": principal.actor_id, "updated_at": repo.clock()}
            condition = (spending_settings.c.tenant_id == settings.tenant_id,
                         spending_settings.c.owner_id == owner)
            if old["version"]:
                connection.execute(update(spending_settings).where(*condition).values(**values))
            else:
                connection.execute(insert(spending_settings).values(tenant_id=settings.tenant_id, owner_id=owner, **values))
            if not admin:
                from .agent_management import synchronize_cap_versions
                synchronize_cap_versions(connection,settings.tenant_id,owner,
                    old['value']['connection_limits'],value['connection_limits'],repo.clock())
            repo._emit(connection, "spending.settings_updated", owner,
                {"tenant_id": settings.tenant_id, "owner_id": owner, "actor_id": principal.actor_id,
                 "old": old, "new": {"version": values["version"], "value": value}})
            return {"version": values["version"], "value": value, "changed": True}

    @app.exception_handler(SpendingConfirmationRequired)
    async def confirmation_required(_request, error):
        return JSONResponse({"code": "spending_confirmation_required", "plan_id": error.plan_id,
            "plan_ids": error.plan_ids,
            "estimate_microusd": error.amount, "message_key": "errors.spendingConfirmationRequired",
            "messages": {"en": "This quote needs your approval before submission.",
                         "zh": "本次报价超过你设置的确认金额，请先确认再提交。"}},
            status_code=409, headers={"Cache-Control": "no-store"})

    @app.get("/v1/account/spending-settings")
    def read_user(request: Request):
        principal = human(request)
        with repo.engine.connect() as connection:
            value = read_settings(connection, settings.tenant_id, principal.owner)
            keys = [dict(row) for row in connection.execute(select(personal_keys.c.id, personal_keys.c.name,
                personal_keys.c.revoked_at).where(personal_keys.c.tenant == settings.tenant_id,
                personal_keys.c.owner == principal.owner)).mappings()]
            return response({**value, "account": principal.owner,
                "usage": public_usage(connection, settings.tenant_id, principal.owner, repo.clock()),
                "connections": [{"actor_id": "key:" + key["id"], "name": key["name"],
                    "revoked": key["revoked_at"] is not None,
                    "daily_limit_microusd": value["value"]["connection_limits"].get("key:" + key["id"], 0)} for key in keys],
                "admin": principal.owner in settings.operator_admin_owners})

    @app.put("/v1/account/spending-settings")
    def update_user(request: Request, body: dict):
        principal = human(request)
        return response(update_value(principal, principal.owner, body))

    @app.get("/v1/operator/admin/spending-settings")
    def read_admin(request: Request):
        human(request, admin=True)
        with repo.engine.connect() as connection:
            return response(read_settings(connection, settings.tenant_id, ADMIN_OWNER,
                admin_defaults=admin_defaults(getattr(app.state, "dstack_operator", None))))

    @app.put("/v1/operator/admin/spending-settings")
    def update_admin(request: Request, body: dict):
        return response(update_value(human(request, admin=True), ADMIN_OWNER, body, admin=True))

    @app.post("/v1/account/spending-confirmations")
    def confirm(request: Request, body: dict):
        principal = human(request)
        if not isinstance(body, dict) or set(body) not in ({"plan_id"}, {"plan_ids"}):
            raise ValueError("spending_confirmation_invalid")
        ids = body.get("plan_ids", [body.get("plan_id")])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(not isinstance(ident, str) for ident in ids) or len(set(ids)) != len(ids):
            raise ValueError("spending_confirmation_invalid")
        with repo.transaction() as connection:
            lock_owner(connection, settings.tenant_id, principal.owner)
            selected = [connection.execute(select(plans).where(plans.c.id == ident,
                plans.c.tenant_id == settings.tenant_id, plans.c.owner_id == principal.owner)).mappings().first()
                for ident in ids]
            if not all(selected):
                raise NotFound("plan_not_found")
            if any(plan["expires_at"] <= repo.clock() for plan in selected):
                raise Conflict("plan_expired")
            config = read_settings(connection, settings.tenant_id, principal.owner)
            for plan in selected:
                store_confirmation(connection, principal, plan, config)
            return response({"plan_ids": ids, "estimate_microusd": sum(p["estimated_cost_microusd"] for p in selected),
                "single_use": True})

    def store_confirmation(connection, principal, plan, config):
        condition = (spending_confirmations.c.tenant_id == settings.tenant_id,
            spending_confirmations.c.owner_id == principal.owner,
            spending_confirmations.c.plan_id == plan["id"],
            spending_confirmations.c.actor_id == principal.actor_id)
        old = connection.execute(select(spending_confirmations).where(*condition)).mappings().first()
        if old and old["consumed_reference"]:
            raise Conflict("spending_confirmation_consumed")
        value = {"plan_hash": plan["plan_hash"], "settings_version": config["version"],
            "expires_at": min(plan["expires_at"], repo.clock() + 600), "consumed_reference": None}
        if old:
            connection.execute(update(spending_confirmations).where(*condition).values(**value))
        else:
            connection.execute(insert(spending_confirmations).values(tenant_id=settings.tenant_id,
                owner_id=principal.owner, actor_id=principal.actor_id, plan_id=plan["id"], **value))
