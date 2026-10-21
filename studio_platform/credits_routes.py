"""Owned credit reads and audited human-admin adjustments/publication."""

import copy
import re
import uuid

from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, insert, select, update

from .auth import Principal
from .credit_schema import entries as entry_table, prices
from .credits_pricing import (
    PricingError,
    admin_margin_estimate,
    configuration,
    current,
    public_prices,
    rates,
    require_ready,
)
from .identity import lock_account_gate
from .identity_schema import admin_audit, users
from .repository import documents, request_hash

POLICY_SCOPE = {
    "owner_id": "__credits",
    "project_id": "__credits",
    "kind": "credit_policy",
    "document_id": "active",
}


class CreditAPI:
    def __init__(self, repository, settings):
        self.repo, self.settings = repository, settings

    def _actor(self, connection, principal):
        require_ready(connection)
        if not isinstance(principal, Principal) or principal.machine:
            raise PricingError("admin_required", "admin_required", 403)
        row = (
            connection.execute(
                select(users).where(
                    users.c.tenant == self.settings.tenant_id,
                    users.c.owner == principal.owner,
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["status"] != "active" or row["role"] != "admin":
            raise PricingError("admin_required", "admin_required", 403)
        return row

    def balance(self, principal):
        from .credits import account

        with self.repo.engine.connect() as connection:
            require_ready(connection)
            value = account(
                connection,
                self.settings.tenant_id,
                principal.owner,
                now=self.repo.clock(),
            )
        return {
            key: value[key]
            for key in ("balance", "held", "available", "today_used", "daily_limit")
        }

    def history(self, principal, *, cursor=None, limit=50):
        from .credits import entries

        with self.repo.engine.connect() as connection:
            require_ready(connection)
            return entries(
                connection,
                self.settings.tenant_id,
                principal.owner,
                cursor=cursor,
                limit=limit,
            )

    def _audit(self, connection, actor, *, action, reason, before, after, target=None):
        connection.execute(
            insert(admin_audit).values(
                id=str(uuid.uuid4()),
                tenant=self.settings.tenant_id,
                actor_user_id=actor["id"],
                target_user_id=target["id"] if target else None,
                action=action,
                reason=reason,
                before=before,
                after={
                    **after,
                    "actor": actor["display_name"] or actor["owner"],
                    "target": (target["display_name"] or target["owner"])
                    if target
                    else "Credit pricing",
                },
                created_at=self.repo.clock(),
            )
        )

    def _key(self, key, *, required=True):
        if key is None and not required:
            return
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key):
            raise PricingError(
                "idempotency_key_required", "Provide an Idempotency-Key."
            )

    def _journal(self, connection, principal, key, operation, body):
        fingerprint = request_hash({"operation": operation, "body": body})
        where = and_(
            documents.c.tenant_id == self.settings.tenant_id,
            documents.c.owner_id == principal.owner,
            documents.c.project_id == "__credits_admin",
            documents.c.kind == "credit_admin_action",
            documents.c.document_id == request_hash({"key": key}),
        )
        saved = (
            connection.execute(
                select(documents.c.payload).where(where)
            ).scalar_one_or_none()
            if key
            else None
        )
        if saved is not None and saved["fingerprint"] != fingerprint:
            raise PricingError(
                "idempotency_conflict",
                "Idempotency-Key was used with another request.",
                409,
            )
        return saved["result"] if saved else None, fingerprint

    def _remember(self, connection, principal, key, fingerprint, result):
        if key:
            connection.execute(
                insert(documents).values(
                    tenant_id=self.settings.tenant_id,
                    owner_id=principal.owner,
                    project_id="__credits_admin",
                    kind="credit_admin_action",
                    document_id=request_hash({"key": key}),
                    version=1,
                    payload={"fingerprint": fingerprint, "result": result},
                    updated_at=self.repo.clock(),
                )
            )

    @staticmethod
    def _reason(value):
        if (
            not isinstance(value, str)
            or not 1 <= len(value.strip()) <= 500
            or any(ord(c) < 32 for c in value)
        ):
            raise PricingError(
                "credit_reason_required", "Provide a reason of 1 to 500 characters."
            )
        return value.strip()

    def adjust(self, principal, user_id, body, *, key):
        from .credits import adjust

        self._key(key)
        if type(body) is not dict or set(body) != {"delta", "reason"}:
            raise PricingError("credit_adjust_invalid", "Provide delta and reason.")
        reason = self._reason(body["reason"])
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            actor = self._actor(connection, principal)
            saved, fingerprint = self._journal(
                connection, principal, key, "adjust:" + user_id, body
            )
            if saved is not None:
                return saved
            target = (
                connection.execute(
                    select(users).where(
                        users.c.tenant == self.settings.tenant_id, users.c.id == user_id
                    )
                )
                .mappings()
                .first()
            )
            if target is None:
                raise PricingError("user_not_found", "User not found.", 404)
            result = adjust(
                connection,
                self.settings.tenant_id,
                target["owner"],
                body["delta"],
                actor=principal.actor_id,
                reason=reason,
                idempotency_key=key,
                now=self.repo.clock(),
            )
            account, entry = result["account"], result["entry"]
            if result["created"]:
                self._audit(
                    connection,
                    actor,
                    action="credits adjusted",
                    reason=reason,
                    before={
                        "balance": entry["balance_after"] - entry["amount"],
                        "held": entry["held_after"],
                    },
                    after={
                        "balance": account["balance"],
                        "held": account["held"],
                        "delta": entry["amount"],
                    },
                    target=target,
                )
            value = {
                "user_id": user_id,
                "balance": account["balance"],
                "held": account["held"],
                "available": account["available"],
                "entry_id": entry["id"],
            }
            self._remember(connection, principal, key, fingerprint, value)
            return value

    def admin_prices(self, principal):
        from .credit_margin import report
        from .credits import _aggregate_integer
        from .payments_routes import current_packs

        with self.repo._budget_read() as connection:
            self._actor(connection, principal)
            value = current(connection, self.settings.tenant_id)
            captured = connection.execute(
                select(func.coalesce(-func.sum(entry_table.c.amount), 0)).where(
                    entry_table.c.tenant == self.settings.tenant_id,
                    entry_table.c.kind == "capture",
                    entry_table.c.created_at >= self.repo.clock() - 30 * 86400,
                )
            ).scalar_one()
            return {
                "revision": value["revision"],
                "rates": value["rates"],
                **value["configuration"],
                "margin_estimate": admin_margin_estimate(
                    value, current_packs(connection, self.settings.tenant_id)
                ),
                "report_30d": {
                    "captured_credits": _aggregate_integer(captured),
                    "provider_cost_usd": None,
                    "credit_cost_margin": report(connection, self.settings.tenant_id, self.repo.clock()),
                },
            }

    def _policy(self, connection, value):
        where = and_(
            documents.c.tenant_id == self.settings.tenant_id,
            *[documents.c[key] == val for key, val in POLICY_SCOPE.items()],
        )
        old = connection.execute(select(documents).where(where)).mappings().first()
        if old:
            connection.execute(
                update(documents)
                .where(where)
                .values(
                    payload=value,
                    version=old["version"] + 1,
                    updated_at=self.repo.clock(),
                )
            )
        else:
            connection.execute(
                insert(documents).values(
                    tenant_id=self.settings.tenant_id,
                    **POLICY_SCOPE,
                    version=1,
                    payload=value,
                    updated_at=self.repo.clock(),
                )
            )

    def publish(self, principal, body, *, key=None):
        from .credits import credit_policy

        self._key(key, required=False)
        if (
            type(body) is not dict
            or not {"expected_revision", "rates"} <= set(body)
            or set(body)
            - {
                "expected_revision",
                "rates",
                "cost_multiplier",
                "welcome_credits",
                "fee_rate",
                "reason",
            }
        ):
            raise PricingError(
                "credit_prices_invalid", "Provide expected_revision and all tier rates."
            )
        reason = self._reason(body.get("reason", "Price publication"))
        rate = rates(body["rates"])
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            actor = self._actor(connection, principal)
            saved, fingerprint = self._journal(
                connection, principal, key, "publish", body
            )
            if saved is not None:
                return saved
            policy = credit_policy(connection, self.settings.tenant_id)
            old = current(connection, self.settings.tenant_id)
            if body["expected_revision"] != old["revision"]:
                raise PricingError(
                    "credit_prices_changed",
                    "Credit prices changed; refresh before publishing.",
                    409,
                )
            config = configuration(
                {
                    **copy.deepcopy(old["configuration"]),
                    **{
                        key: body[key]
                        for key in ("cost_multiplier", "welcome_credits", "fee_rate")
                        if key in body
                    },
                }
            )
            if (
                old["revision"]
                and rate == old["rates"]
                and config == old["configuration"]
            ):
                value = {"revision": old["revision"], "rates": old["rates"]}
            else:
                revision = str(uuid.uuid4())
                connection.execute(
                    insert(prices).values(
                        tenant=self.settings.tenant_id,
                        revision=revision,
                        rates=rate,
                        configuration=config,
                        published_by=actor["id"],
                        published_at=self.repo.clock(),
                    )
                )
                self._policy(connection, {**policy, "active_revision": revision})
                self._audit(
                    connection,
                    actor,
                    action="credit prices published",
                    reason=reason,
                    before=old,
                    after={
                        "revision": revision,
                        "rates": rate,
                        "configuration": config,
                    },
                )
                value = {"revision": revision, "rates": rate}
            self._remember(connection, principal, key, fingerprint, value)
            return value

    def policy(self, principal, body=None, *, key=None):
        from .credits import credit_policy

        if body is None:
            with self.repo.engine.connect() as connection:
                self._actor(connection, principal)
                return credit_policy(connection, self.settings.tenant_id)
        self._key(key)
        if (
            type(body) is not dict
            or set(body) != {"expected_revision", "enabled", "reason"}
            or type(body["enabled"]) is not bool
        ):
            raise PricingError(
                "credit_policy_invalid",
                "Provide expected_revision, enabled and reason.",
            )
        reason = self._reason(body["reason"])
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            actor = self._actor(connection, principal)
            saved, fingerprint = self._journal(
                connection, principal, key, "policy", body
            )
            if saved is not None:
                return saved
            old = credit_policy(connection, self.settings.tenant_id)
            if body["expected_revision"] != old["active_revision"]:
                raise PricingError(
                    "credit_prices_changed",
                    "Credit prices changed; refresh before activation.",
                    409,
                )
            if body["enabled"]:
                price = current(connection, self.settings.tenant_id)
                if price["revision"] is None:
                    raise PricingError(
                        "credit_prices_unavailable",
                        "Publish prices before activation.",
                        409,
                    )
            value = {**old, "enabled": body["enabled"]}
            if value != old:
                self._policy(connection, value)
                self._audit(
                    connection,
                    actor,
                    action="credits enabled"
                    if value["enabled"]
                    else "credits disabled",
                    reason=reason,
                    before=old,
                    after=value,
                )
            self._remember(connection, principal, key, fingerprint, value)
            return value

    def bind_generation_budget(self, principal, user_id, body, *, key):
        from .generation_budget_binding import ensure_owner_binding

        self._key(key)
        if type(body) is not dict or set(body) != {"reason"}:
            raise PricingError("generation_budget_binding_invalid", "Provide a reason.")
        reason = self._reason(body["reason"])
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            actor = self._actor(connection, principal)
            saved, fingerprint = self._journal(
                connection, principal, key, "generation-budget:" + user_id, body
            )
            if saved is not None:
                return saved
            target = (
                connection.execute(
                    select(users).where(
                        users.c.tenant == self.settings.tenant_id, users.c.id == user_id
                    )
                )
                .mappings()
                .first()
            )
            if target is None:
                raise PricingError("user_not_found", "User not found.", 404)
            result = ensure_owner_binding(
                connection,
                self.settings,
                target,
                actor_user_id=actor["id"],
                reason=reason,
                now=self.repo.clock(),
            )
            self._remember(connection, principal, key, fingerprint, result)
            return result


def register_routes(app):
    service = CreditAPI(app.state.repository, app.state.settings)
    app.state.credits = service

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    from .credits import CreditError

    @app.exception_handler(CreditError)
    @app.exception_handler(PricingError)
    async def credit_error(_request, error):
        return JSONResponse(
            {"code": error.code, "detail": error.detail, **getattr(error, "data", {})},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/credits/prices")
    def read_prices():
        with service.repo.engine.connect() as connection:
            return response(public_prices(connection, service.settings.tenant_id))

    @app.get("/v1/credits")
    def balance(request: Request):
        return response(service.balance(request.state.principal))

    @app.get("/v1/credits/entries")
    def history(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        return response(
            service.history(request.state.principal, cursor=cursor, limit=limit)
        )

    @app.get("/v1/admin/credits/prices")
    def admin_prices(request: Request):
        return response(service.admin_prices(request.state.principal))

    @app.post("/v1/admin/credits/prices")
    def publish(
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return response(service.publish(request.state.principal, body, key=key))

    @app.get("/v1/admin/credits/policy")
    def policy(request: Request):
        return response(service.policy(request.state.principal))

    @app.post("/v1/admin/credits/policy")
    def set_policy(
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return response(service.policy(request.state.principal, body, key=key))

    @app.post("/v1/admin/users/{user_id}/credits")
    def adjust(
        user_id: str,
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return response(service.adjust(request.state.principal, user_id, body, key=key))

    @app.post("/v1/admin/users/{user_id}/generation-budget-binding")
    def bind_generation_budget(
        user_id: str,
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return response(
            service.bind_generation_budget(
                request.state.principal, user_id, body, key=key
            )
        )
