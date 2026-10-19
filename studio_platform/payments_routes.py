"""Billing HTTP boundary and admin pack settings in the existing document store."""

from datetime import datetime, timezone
import re
import uuid

from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, insert, select, update

from .auth import Principal
from .identity import account_identity, lock_account_gate
from .identity_schema import admin_audit, users
from .repository import documents, request_hash
from .stripe_client import (
    MAX_BODY,
    StripeGateway,
    StripeGatewayError,
    optional_receipt_url,
    provider_url,
)

PACK_SCOPE = {
    "owner_id": "__billing",
    "project_id": "__billing",
    "kind": "billing_packs",
    "document_id": "active",
}
MAX_CREDITS = (1 << 63) - 1


class BillingError(Exception):
    def __init__(self, code, detail, status=409):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(code)


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise BillingError(
            "idempotency_key_required", "Provide an Idempotency-Key.", 422
        )
    return value


def _reason(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise BillingError("billing_reason_required", "Provide a short reason.", 422)
    return value.strip()


def validate_packs(packs):
    required = {
        "id",
        "price_id",
        "credits",
        "amount_cents",
        "currency",
        "bonus_percent",
    }
    if not isinstance(packs, list) or len(packs) > 100:
        raise BillingError(
            "billing_packs_invalid", "Provide a valid list of credit packs.", 422
        )
    ids, prices = set(), set()
    for pack in packs:
        if (
            type(pack) is not dict
            or not required <= set(pack) <= required | {"popular"}
            or not isinstance(pack["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pack["id"])
            or not isinstance(pack["price_id"], str)
            or not re.fullmatch(r"price_[A-Za-z0-9]{1,194}", pack["price_id"])
            or type(pack["credits"]) is not int
            or not 1 <= pack["credits"] <= MAX_CREDITS
            or type(pack["amount_cents"]) is not int
            or not 500 <= pack["amount_cents"] <= 99999999
            or pack["currency"] != "usd"
            or type(pack["bonus_percent"]) is not int
            or not 0 <= pack["bonus_percent"] <= 100
            or "popular" in pack
            and type(pack["popular"]) is not bool
            or pack["id"] in ids
            or pack["price_id"] in prices
        ):
            raise BillingError(
                "billing_packs_invalid",
                "Each pack needs a unique ID, test price, USD amount and integer credits.",
                422,
            )
        ids.add(pack["id"])
        prices.add(pack["price_id"])
    return [dict(pack) for pack in packs]


class PaymentAPI:
    def __init__(self, repo, settings, *, terms_check=None, core=None, gateway=None):
        self.repo, self.settings, self.terms_check = repo, settings, terms_check
        self.gateway = gateway or StripeGateway(settings)
        if core is None:
            from .payments import PaymentCore

            core = PaymentCore(repo, settings, terms_check=terms_check)
        self.core = core

    def _where(self):
        return and_(
            documents.c.tenant_id == self.settings.tenant_id,
            *[documents.c[key] == value for key, value in PACK_SCOPE.items()],
        )

    def _packs(self, connection):
        value = connection.execute(
            select(documents.c.payload).where(self._where())
        ).scalar_one_or_none()
        if value is None:
            return {"revision": None, "packs": []}
        if (
            type(value) is not dict
            or set(value) != {"revision", "packs"}
            or not isinstance(value["revision"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value["revision"])
        ):
            raise BillingError(
                "billing_packs_unavailable", "Credit packs are unavailable.", 503
            )
        try:
            validate_packs(value["packs"])
        except BillingError:
            raise BillingError(
                "billing_packs_unavailable", "Credit packs are unavailable.", 503
            ) from None
        return value

    def _user(self, connection, principal, *, admin=False):
        if not isinstance(principal, Principal) or principal.machine:
            raise BillingError(
                "human_account_required", "Sign in with your personal account.", 403
            )
        # No legacy fallback or caller-supplied user/role can authorize billing.
        identity = account_identity(
            connection, self.settings.tenant_id, principal.owner
        )
        if (
            identity is None
            or identity["status"] != "active"
            or admin
            and identity["role"] != "admin"
        ):
            raise BillingError(
                "admin_required" if admin else "account_unavailable",
                "An active administrator is required."
                if admin
                else "This account is unavailable.",
                403,
            )
        return (
            connection.execute(
                select(users).where(
                    users.c.id == identity["id"],
                    users.c.tenant == self.settings.tenant_id,
                )
            )
            .mappings()
            .one()
        )

    def packs(self):
        with self.repo.engine.connect() as connection:
            value = self._packs(connection)
        return {
            "packs": [
                {key: item for key, item in pack.items() if key != "price_id"}
                for pack in value["packs"]
            ]
        }

    def admin_packs(self, principal):
        with self.repo.engine.connect() as connection:
            self._user(connection, principal, admin=True)
            return self._packs(connection)

    def set_packs(self, principal, body, *, key):
        _key(key)
        if type(body) is not dict or set(body) != {
            "expected_revision",
            "packs",
            "reason",
        }:
            raise BillingError(
                "billing_packs_invalid",
                "Provide expected_revision, packs and reason.",
                422,
            )
        packs, reason = validate_packs(body["packs"]), _reason(body["reason"])
        fingerprint = request_hash(body)
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            actor = self._user(connection, principal, admin=True)
            journal_where = and_(
                documents.c.tenant_id == self.settings.tenant_id,
                documents.c.owner_id == principal.owner,
                documents.c.project_id == "__billing_admin",
                documents.c.kind == "billing_admin_action",
                documents.c.document_id == request_hash({"key": key}),
            )
            previous = connection.execute(
                select(documents.c.payload).where(journal_where)
            ).scalar_one_or_none()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise BillingError(
                        "idempotency_conflict",
                        "This request key was already used for another change.",
                    )
                return previous["result"]
            old = self._packs(connection)
            if body["expected_revision"] != old["revision"]:
                raise BillingError(
                    "billing_packs_changed",
                    "Credit packs changed. Refresh before saving.",
                )
            value = {"revision": str(uuid.uuid4()), "packs": packs}
            row = connection.execute(
                select(documents.c.version).where(self._where())
            ).first()
            if row:
                connection.execute(
                    update(documents)
                    .where(self._where())
                    .values(
                        payload=value,
                        version=row.version + 1,
                        updated_at=self.repo.clock(),
                    )
                )
            else:
                connection.execute(
                    insert(documents).values(
                        tenant_id=self.settings.tenant_id,
                        **PACK_SCOPE,
                        version=1,
                        payload=value,
                        updated_at=self.repo.clock(),
                    )
                )
            connection.execute(
                insert(admin_audit).values(
                    id=str(uuid.uuid4()),
                    tenant=self.settings.tenant_id,
                    actor_user_id=actor["id"],
                    target_user_id=None,
                    action="billing packs updated",
                    reason=reason,
                    before=old,
                    after=value,
                    created_at=self.repo.clock(),
                )
            )
            connection.execute(
                insert(documents).values(
                    tenant_id=self.settings.tenant_id,
                    owner_id=principal.owner,
                    project_id="__billing_admin",
                    kind="billing_admin_action",
                    document_id=request_hash({"key": key}),
                    version=1,
                    payload={"fingerprint": fingerprint, "result": value},
                    updated_at=self.repo.clock(),
                )
            )
            return value

    def checkout(self, principal, body, *, key):
        _key(key)
        if (
            type(body) is not dict
            or set(body) != {"pack_id"}
            or not isinstance(body["pack_id"], str)
        ):
            raise BillingError("billing_pack_required", "Choose a credit pack.", 422)
        if not callable(self.terms_check):
            with self.repo.engine.connect() as connection:
                self._user(connection, principal)
            raise BillingError(
                "billing_terms_unavailable",
                "Credit purchases will open after the purchase terms are approved.",
                503,
            )
        from .payment_schema import PaymentSchemaError, payments, ready

        with self.repo.engine.connect() as connection:
            user = self._user(connection, principal)
            try:
                if not ready(connection):
                    raise PaymentSchemaError()
            except PaymentSchemaError:
                raise BillingError(
                    "billing_unavailable", "Credit purchases are not ready.", 503
                ) from None
            original = connection.execute(
                select(payments.c.pack_snapshot).where(
                    payments.c.tenant == self.settings.tenant_id,
                    payments.c.user_id == user["id"],
                    payments.c.idempotency_key == key,
                )
            ).scalar_one_or_none()
            if original is not None:
                if original["id"] != body["pack_id"]:
                    raise BillingError(
                        "idempotency_conflict",
                        "This request key was already used for another purchase.",
                    )
                pack = original
            else:
                saved = self._packs(connection)
                found = next(
                    (item for item in saved["packs"] if item["id"] == body["pack_id"]),
                    None,
                )
                pack = (
                    {**found, "revision": saved["revision"]}
                    if found is not None
                    else None
                )
        if pack is None:
            raise BillingError(
                "billing_pack_unavailable",
                "This credit pack is unavailable. Please refresh.",
                409,
            )
        config = self.gateway.configuration(for_checkout=True)
        prepared = self.core.begin_checkout(
            principal,
            pack,
            key,
            account_scope=config.account_id,
        )
        payment = prepared["payment"]
        if not prepared["dispatch"]:
            if not payment.get("checkout_url"):
                raise BillingError(
                    "checkout_reconciliation_required",
                    "Payment setup is being checked. Retry the same request.",
                    409,
                )
            return {"url": provider_url(payment["checkout_url"])}
        try:
            session = self.gateway.checkout(
                payment, idempotency_key=prepared["stripe_idempotency_key"]
            )
        except StripeGatewayError as error:
            self.core.mark_checkout_unknown(
                payment["id"], payment["request_fingerprint"], error.code
            )
            raise
        result = self.core.bind_checkout(
            payment["id"], payment["request_fingerprint"], session
        )
        return {"url": provider_url(result["checkout_url"])}

    def receive_webhook(self, raw, signature):
        event = self.gateway.verify_event(raw, signature)
        if event is not None:
            self.core.receive_event(event)
        return {"received": True}

    def history(self, principal, *, before=None, limit=50):
        from .payment_schema import PaymentSchemaError, payments, ready

        if type(limit) is not int or not 1 <= limit <= 100:
            raise BillingError(
                "billing_page_invalid", "Choose a page size from 1 to 100.", 422
            )
        with self.repo.engine.connect() as connection:
            user = self._user(connection, principal)
            try:
                if not ready(connection):
                    raise PaymentSchemaError()
            except PaymentSchemaError:
                raise BillingError(
                    "billing_unavailable", "Payment history is not ready.", 503
                ) from None
            statement = select(payments).where(
                payments.c.tenant == self.settings.tenant_id,
                payments.c.user_id == user["id"],
            )
            if before is not None:
                cursor = connection.execute(
                    select(payments.c.created_at, payments.c.id).where(
                        payments.c.id == before,
                        payments.c.tenant == self.settings.tenant_id,
                        payments.c.user_id == user["id"],
                    )
                ).first()
                if cursor is None:
                    raise BillingError(
                        "billing_cursor_invalid",
                        "This payment page is unavailable.",
                        422,
                    )
                from sqlalchemy import or_

                statement = statement.where(
                    or_(
                        payments.c.created_at < cursor.created_at,
                        and_(
                            payments.c.created_at == cursor.created_at,
                            payments.c.id < cursor.id,
                        ),
                    )
                )
            rows = list(
                connection.execute(
                    statement.order_by(
                        payments.c.created_at.desc(), payments.c.id.desc()
                    ).limit(limit + 1)
                ).mappings()
            )
        result = []
        for row in rows[:limit]:
            item = {
                key: row[key]
                for key in (
                    "id",
                    "pack_id",
                    "credits",
                    "amount_cents",
                    "currency",
                    "status",
                )
            }
            item.update(
                created_at=datetime.fromtimestamp(
                    row["created_at"], timezone.utc
                ).isoformat(),
                paid_at=datetime.fromtimestamp(row["paid_at"], timezone.utc).isoformat()
                if row["paid_at"] is not None
                else None,
                receipt_url=optional_receipt_url(row["receipt_url"]),
            )
            result.append(item)
        return {
            "payments": result,
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }


def register_routes(
    app,
    settings,
    repo,
    principal_resolver,
    *,
    terms_check=None,
    core=None,
    gateway=None,
):
    from .payments import PaymentError

    service = PaymentAPI(
        repo, settings, terms_check=terms_check, core=core, gateway=gateway
    )
    app.state.payments = service

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.exception_handler(BillingError)
    @app.exception_handler(StripeGatewayError)
    @app.exception_handler(PaymentError)
    async def billing_error(_request, error):
        return JSONResponse(
            {"code": error.code, "detail": error.detail},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/billing/packs")
    def packs():
        return response(service.packs())

    @app.get("/v1/admin/billing/packs")
    def admin_packs(request: Request):
        return response(service.admin_packs(principal_resolver(request)))

    @app.post("/v1/admin/billing/packs")
    def save_packs(
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return response(service.set_packs(principal_resolver(request), body, key=key))

    @app.post("/v1/billing/checkout")
    def checkout(
        request: Request,
        body: dict,
        key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return response(service.checkout(principal_resolver(request), body, key=key))

    @app.get("/v1/billing/payments")
    def history(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        return response(
            service.history(principal_resolver(request), before=cursor, limit=limit)
        )

    @app.post("/v1/billing/stripe/webhook")
    async def webhook(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY:
                raise BillingError(
                    "billing_event_too_large",
                    "The payment notification is too large.",
                    413,
                )
            raw.extend(chunk)
        return response(
            service.receive_webhook(bytes(raw), request.headers.get("stripe-signature"))
        )

    return service
