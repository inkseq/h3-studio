"""Browser-owned limits and exact-quote confirmations; no provider effects."""

from sqlalchemy import insert, select, update
from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import Principal, personal_keys
from .operator_capacity import OperatorError
from .identity import is_admin
from .repository import (
    Conflict,
    NotFound,
    plans,
    spending_settings,
    spending_confirmations,
)
from .spending_limits import (
    ADMIN_OWNER,
    ADMIN_DEFAULTS,
    SpendingConfirmationRequired,
    lock_owner,
    read_settings,
    validate_settings,
    public_usage,
    admin_defaults,
    credit_settings,
)


def register_routes(app):
    repo, settings = app.state.repository, app.state.settings

    def human(request, *, admin=False):
        principal = getattr(request.state, "principal", None)
        if admin:
            from .identity import require_admin

            require_admin(
                principal,
                repo,
                settings,
                machine_code="spending_settings_browser_required",
            )
        if not isinstance(principal, Principal) or principal.machine:
            raise OperatorError("spending_settings_browser_required", 403)
        return principal

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    def owned_keys(connection, owner):
        return {
            "key:" + row.id
            for row in connection.execute(
                select(personal_keys.c.id).where(
                    personal_keys.c.tenant == settings.tenant_id,
                    personal_keys.c.owner == owner,
                )
            )
        }

    def update_value(principal, owner, body, *, admin=False):
        if (
            not isinstance(body, dict)
            or set(body) != {"expected_version", "value"}
            or type(body["expected_version"]) is not int
        ):
            raise ValueError("spending_settings_invalid")
        value = validate_settings(body["value"], admin=admin)
        with repo.transaction() as connection:
            lock_owner(connection, settings.tenant_id, owner)
            old = read_settings(
                connection,
                settings.tenant_id,
                owner,
                admin_defaults=admin_defaults(
                    getattr(app.state, "dstack_operator", None)
                )
                if admin
                else None,
            )
            if not admin:
                # Old clients cannot erase newly configured credit limits, and
                # credit clients cannot reinterpret the retained provider caps.
                value = validate_settings({**old["value"], **value})
            if body["expected_version"] != old["version"]:
                raise Conflict("spending_settings_changed")
            if not admin:
                selected_keys = set(value["connection_limits"]) | set(
                    value.get("connection_credit_limits", {})
                )
                if selected_keys - owned_keys(connection, principal.owner):
                    raise NotFound("connection_not_found")
            if old["value"] == value:
                return {**old, "changed": False}
            values = {
                "value": value,
                "version": old["version"] + 1,
                "actor_id": principal.actor_id,
                "updated_at": repo.clock(),
            }
            condition = (
                spending_settings.c.tenant_id == settings.tenant_id,
                spending_settings.c.owner_id == owner,
            )
            if old["version"]:
                connection.execute(
                    update(spending_settings).where(*condition).values(**values)
                )
            else:
                connection.execute(
                    insert(spending_settings).values(
                        tenant_id=settings.tenant_id, owner_id=owner, **values
                    )
                )
            if not admin:
                from .agent_management import synchronize_cap_versions

                synchronize_cap_versions(
                    connection,
                    settings.tenant_id,
                    owner,
                    old['value']['connection_limits'],
                    value['connection_limits'],
                    repo.clock(),
                    old_credit_limits=old['value'].get('connection_credit_limits', {}),
                    new_credit_limits=value.get('connection_credit_limits', {}),
                )
            repo._emit(
                connection,
                "spending.settings_updated",
                owner,
                {
                    "tenant_id": settings.tenant_id,
                    "owner_id": owner,
                    "actor_id": principal.actor_id,
                    "old": old,
                    "new": {"version": values["version"], "value": value},
                },
            )
            return {"version": values["version"], "value": value, "changed": True}

    @app.exception_handler(SpendingConfirmationRequired)
    async def confirmation_required(_request, error):
        return JSONResponse(
            {
                "code": "spending_confirmation_required",
                "plan_id": error.plan_id,
                "plan_ids": error.plan_ids,
                (
                    "credits" if error.unit == "credits" else "estimate_microusd"
                ): error.amount,
                "unit": error.unit,
                "message_key": "errors.spendingConfirmationRequired",
                "messages": {
                    "en": "This quote needs your approval before submission.",
                    "zh": "本次报价超过你设置的确认金额，请先确认再提交。",
                },
            },
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/account/spending-settings")
    def read_user(request: Request):
        principal = human(request)
        with repo.engine.connect() as connection:
            value = read_settings(connection, settings.tenant_id, principal.owner)
            from .credits import credit_policy, daily_projection as credit_projection

            credit_mode = credit_policy(connection, settings.tenant_id)["enabled"]
            if credit_mode:
                value = credit_settings(connection, settings.tenant_id, principal.owner)
            keys = [
                dict(row)
                for row in connection.execute(
                    select(
                        personal_keys.c.id,
                        personal_keys.c.name,
                        personal_keys.c.revoked_at,
                    ).where(
                        personal_keys.c.tenant == settings.tenant_id,
                        personal_keys.c.owner == principal.owner,
                    )
                ).mappings()
            ]
            return response(
                {
                    **value,
                    "account": principal.owner,
                    "unit": "credits" if credit_mode else "microusd",
                    "usage": {
                        "exposure_credits": credit_projection(
                            connection,
                            settings.tenant_id,
                            principal.owner,
                            repo.clock(),
                        )
                    }
                    if credit_mode
                    else public_usage(
                        connection, settings.tenant_id, principal.owner, repo.clock()
                    ),
                    "connections": [
                        {
                            "actor_id": "key:" + key["id"],
                            "name": key["name"],
                            "revoked": key["revoked_at"] is not None,
                            (
                                "daily_limit_credits"
                                if credit_mode
                                else "daily_limit_microusd"
                            ): value["value"][
                                "connection_credit_limits"
                                if credit_mode
                                else "connection_limits"
                            ].get("key:" + key["id"], 0),
                        }
                        for key in keys
                    ],
                    "admin": is_admin(principal, repo, settings),
                }
            )

    @app.put("/v1/account/spending-settings")
    def update_user(request: Request, body: dict):
        principal = human(request)
        result = update_value(principal, principal.owner, body)
        from .credits import credit_policy

        with repo.engine.connect() as connection:
            if credit_policy(connection, settings.tenant_id)["enabled"]:
                result = {
                    **result,
                    "value": credit_settings(
                        connection, settings.tenant_id, principal.owner
                    )["value"],
                    "unit": "credits",
                }
        return response(result)

    @app.get("/v1/operator/admin/spending-settings")
    def read_admin(request: Request):
        human(request, admin=True)
        with repo.engine.connect() as connection:
            return response(
                {
                    **read_settings(
                        connection,
                        settings.tenant_id,
                        ADMIN_OWNER,
                        admin_defaults=admin_defaults(
                            getattr(app.state, "dstack_operator", None)
                        ),
                    ),
                    "test_budget_scope": "acceptance_task",
                }
            )

    @app.put("/v1/operator/admin/spending-settings")
    def update_admin(request: Request, body: dict):
        return response(
            update_value(human(request, admin=True), ADMIN_OWNER, body, admin=True)
        )

    @app.post("/v1/account/spending-confirmations")
    def confirm(request: Request, body: dict):
        principal = human(request)
        if not isinstance(body, dict) or set(body) not in ({"plan_id"}, {"plan_ids"}):
            raise ValueError("spending_confirmation_invalid")
        ids = body.get("plan_ids", [body.get("plan_id")])
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 1000
            or any(not isinstance(ident, str) for ident in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("spending_confirmation_invalid")
        with repo.transaction() as connection:
            lock_owner(connection, settings.tenant_id, principal.owner)
            selected = [
                connection.execute(
                    select(plans).where(
                        plans.c.id == ident,
                        plans.c.tenant_id == settings.tenant_id,
                        plans.c.owner_id == principal.owner,
                    )
                )
                .mappings()
                .first()
                for ident in ids
            ]
            if not all(selected):
                raise NotFound("plan_not_found")
            if any(plan["expires_at"] <= repo.clock() for plan in selected):
                raise Conflict("plan_expired")
            config = read_settings(connection, settings.tenant_id, principal.owner)
            for plan in selected:
                store_confirmation(connection, principal, plan, config)
            quote = [plan["execution_plan"].get("credit_quote") for plan in selected]
            credit_mode = all(
                isinstance(item, dict) and item.get("enforced") is True
                for item in quote
            )
            if any(quote) and not credit_mode:
                raise Conflict("credit_quote_required")
            return response(
                {
                    "plan_ids": ids,
                    ("credits" if credit_mode else "estimate_microusd"): sum(
                        item["credits"] for item in quote
                    )
                    if credit_mode
                    else sum(p["estimated_cost_microusd"] for p in selected),
                    "unit": "credits" if credit_mode else "microusd",
                    "single_use": True,
                }
            )

    def store_confirmation(connection, principal, plan, config):
        condition = (
            spending_confirmations.c.tenant_id == settings.tenant_id,
            spending_confirmations.c.owner_id == principal.owner,
            spending_confirmations.c.plan_id == plan["id"],
            spending_confirmations.c.actor_id == principal.actor_id,
        )
        old = (
            connection.execute(select(spending_confirmations).where(*condition))
            .mappings()
            .first()
        )
        if old and old["consumed_reference"]:
            raise Conflict("spending_confirmation_consumed")
        value = {
            "plan_hash": plan["plan_hash"],
            "settings_version": config["version"],
            "expires_at": min(plan["expires_at"], repo.clock() + 600),
            "consumed_reference": None,
        }
        if old:
            connection.execute(
                update(spending_confirmations).where(*condition).values(**value)
            )
        else:
            connection.execute(
                insert(spending_confirmations).values(
                    tenant_id=settings.tenant_id,
                    owner_id=principal.owner,
                    actor_id=principal.actor_id,
                    plan_id=plan["id"],
                    **value,
                )
            )
