"""Original account/mode-bound purchases and verified-event money transitions.

No Stripe SDK, network, secret loading, startup DDL or alternate queue lives
here. The hosted-Checkout adapter verifies/signature-normalizes provider data;
this service independently binds it to the immutable original purchase.
"""

from __future__ import annotations

import copy
import math
import re
import uuid
from urllib.parse import urlsplit

from sqlalchemy import insert, or_, select, update

from .auth import Principal
from .identity import lock_account_gate
from .identity_schema import admin_audit, users
from .payment_schema import (
    LIVE_PAYMENT_REVISION,
    PaymentSchemaError,
    events,
    payments,
    ready,
    recognized_revision,
)
from .repository import request_hash
from .payment_tax import original_price_amount, refund_base_amount

EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
        "charge.refunded",
        "charge.dispute.created",
        "charge.dispute.closed",
    }
)
OBJECT_FIELDS = frozenset(
    {
        "payment_id",
        "session_id",
        "payment_intent_id",
        "charge_id",
        "dispute_id",
        "user_id",
        "pack_id",
        "pack_revision",
        "credits",
        "price_id",
        "amount_cents",
        "currency",
        "payment_status",
        "amount_refunded",
        "base_amount_cents",
        "tax_amount_cents",
        "tax_behavior",
        "receipt_url",
        "expires_at",
        "checkout_url",
        "dispute_won",
    }
)
MAX_INTEGER = (1 << 63) - 1
CHECKOUT_REPLAY_SECONDS = 23 * 3600
DEFER_SECONDS = 24 * 3600
STAGING = "https://staging.inkseq.com"
PRODUCTION = "https://inkseq.com"
LIVE_ALIASES = frozenset({"https://sixnine.art", "https://www.sixnine.art"})
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


class PaymentError(Exception):
    def __init__(self, code, status=409, detail=None):
        self.code, self.status, self.detail = code, status, detail or code
        super().__init__(code)


def _require(condition, code="payment_input_invalid", status=422):
    if not condition:
        raise PaymentError(code, status)


def _integer(value, *, positive=False):
    _require(type(value) is int and (1 if positive else 0) <= value <= MAX_INTEGER)
    return value


def _clock(value):
    _require(type(value) in (int, float) and math.isfinite(value) and value > 0)
    return value


def _identifier(value, maximum=200):
    _require(type(value) is str and len(value) <= maximum and _ID.fullmatch(value))
    return value


def _uuid(value):
    try:
        _require(type(value) is str and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise PaymentError("payment_input_invalid", 422) from None
    return value


def _url(value, *, checkout=False):
    _require(type(value) is str and 0 < len(value) <= 2048)
    try:
        parts = urlsplit(value)
        _require(
            parts.scheme == "https"
            and parts.hostname
            == ("checkout.stripe.com" if checkout else "pay.stripe.com")
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
        )
    except ValueError:
        raise PaymentError("payment_input_invalid", 422) from None
    return value


def _pack(value):
    required = {
        "id",
        "revision",
        "price_id",
        "credits",
        "amount_cents",
        "currency",
        "bonus_percent",
    }
    _require(
        type(value) is dict
        and required <= set(value)
        and not set(value) - required - {"popular"}
    )
    for field in ("id", "revision"):
        _identifier(value[field], 64)
    _identifier(value["price_id"])
    _require(value["price_id"].startswith("price_"))
    _integer(value["credits"], positive=True)
    _integer(value["amount_cents"], positive=True)
    _require(value["currency"] == "usd")
    _require(
        type(value["bonus_percent"]) is int and 0 <= value["bonus_percent"] <= 1000
    )
    _require("popular" not in value or type(value["popular"]) is bool)
    return copy.deepcopy(value)


def _object(value, *, livemode=False):
    _require(type(value) is dict and set(value) <= OBJECT_FIELDS)
    for key in ("payment_id", "user_id"):
        if key in value:
            _uuid(value[key])
    prefixes = {
        "session_id": "cs_live_" if livemode else "cs_test_",
        "payment_intent_id": "pi_",
        "charge_id": "ch_",
        "dispute_id": "dp_",
        "price_id": "price_",
    }
    for key, prefix in prefixes.items():
        if key in value:
            _require(_identifier(value[key]).startswith(prefix))
    for key in ("pack_id", "pack_revision"):
        if key in value:
            _identifier(value[key], 64)
    for key in (
        "credits",
        "amount_cents",
        "amount_refunded",
        "base_amount_cents",
        "tax_amount_cents",
    ):
        if key in value:
            _integer(
                value[key], positive=key not in {"amount_refunded", "tax_amount_cents"}
            )
    _require(
        "tax_behavior" not in value
        or type(value["tax_behavior"]) is str
        and value["tax_behavior"] in {"inclusive", "exclusive"}
    )
    _require("currency" not in value or value["currency"] == "usd")
    _require(
        "payment_status" not in value
        or type(value["payment_status"]) is str
        and value["payment_status"] in {"paid", "unpaid", "no_payment_required"}
    )
    if "expires_at" in value:
        _clock(value["expires_at"])
    if "receipt_url" in value:
        _url(value["receipt_url"])
    if "checkout_url" in value:
        _url(value["checkout_url"], checkout=True)
    _require("dispute_won" not in value or type(value["dispute_won"]) is bool)
    return copy.deepcopy(value)


def _context(value, *, event=False):
    fields = {"account_scope", "livemode", "object"} | (
        {"id", "type", "created"} if event else set()
    )
    _require(type(value) is dict and set(value) == fields)
    _require(type(value["livemode"]) is bool)
    _require(_identifier(value["account_scope"]).startswith("acct_"))
    result = copy.deepcopy(value)
    result["object"] = _object(value["object"], livemode=value["livemode"])
    if event:
        _require(_identifier(value["id"]).startswith("evt_"))
        _require(
            type(value["type"]) is str and value["type"] in EVENT_TYPES,
            "payment_event_type_unsupported",
            422,
        )
        _clock(value["created"])
    return result


class PaymentCore:
    def __init__(self, repo, settings, *, terms_check=None):
        self.repo, self.settings, self.terms_check = repo, settings, terms_check

    def _enabled(self, connection, *, livemode=None):
        live = getattr(self.settings, "deployment_target", "production") == "production"
        if (livemode is not None and livemode is not live) or (
            live
            and (
                self.settings.public_origin != PRODUCTION
                or not set(self.settings.public_origin_aliases) <= LIVE_ALIASES
            )
            or not live
            and (
                self.settings.deployment_target != "staging"
                or self.settings.public_origin != STAGING
                or self.settings.public_origin_aliases
            )
        ):
            raise PaymentError("payment_test_environment_required", 503)
        try:
            if (
                not ready(connection)
                or live
                and recognized_revision(connection) != LIVE_PAYMENT_REVISION
            ):
                raise PaymentError("payment_schema_not_ready", 503)
        except PaymentSchemaError:
            raise PaymentError("payment_schema_not_ready", 503) from None

    def _user(self, connection, owner=None, *, user_id=None, active=False):
        statement = select(users).where(users.c.tenant == self.settings.tenant_id)
        statement = (
            statement.where(users.c.id == user_id)
            if user_id
            else statement.where(users.c.owner == owner)
        )
        row = connection.execute(statement.with_for_update()).mappings().first()
        if row is None or active and row["status"] != "active":
            raise PaymentError("payment_account_unavailable", 403)
        return dict(row)

    def _payment(self, connection, payment_id):
        row = (
            connection.execute(
                select(payments)
                .where(
                    payments.c.id == payment_id,
                    payments.c.tenant == self.settings.tenant_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PaymentError("payment_not_found", 404)
        return dict(row)

    @staticmethod
    def _state(row):
        return {
            "id": row["id"],
            "status": row["status"],
            "user_id": row["user_id"],
            "pack_id": row["pack_id"],
            "pack_revision": row["pack_revision"],
            "pack_snapshot": copy.deepcopy(row["pack_snapshot"]),
            "price_id": row["price_id"],
            "credits": row["credits"],
            "amount_cents": row["amount_cents"],
            "currency": row["currency"],
            "account_scope": row["account_scope"],
            "livemode": bool(row["livemode"]),
            "request_fingerprint": row["request_fingerprint"],
            "stripe_checkout_session_id": row["stripe_checkout_session_id"],
            "checkout_url": row["checkout_url"],
            "checkout_started_at": row["checkout_started_at"],
        }

    @staticmethod
    def _audit(connection, row, action, after, now):
        actor = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "inkseq-stripe:" + row["tenant"] + ":" + row["account_scope"],
            )
        )
        connection.execute(
            insert(admin_audit).values(
                id=str(uuid.uuid4()),
                tenant=row["tenant"],
                actor_user_id=actor,
                target_user_id=row["user_id"],
                action=action,
                reason="Verified Stripe live payment"
                if row["livemode"]
                else "Verified Stripe test payment",
                before={},
                after={
                    "actor": "Stripe",
                    "target": row["user_id"],
                    "payment_id": row["id"],
                    "livemode": bool(row["livemode"]),
                    **after,
                },
                created_at=now,
            )
        )

    def begin_checkout(
        self, principal, pack, idempotency_key, *, account_scope, livemode=False
    ):
        if not isinstance(principal, Principal) or principal.machine:
            raise PaymentError("payment_human_required", 403)
        _require(
            type(idempotency_key) is str and _KEY.fullmatch(idempotency_key),
            "payment_idempotency_key_invalid",
        )
        _require(_identifier(account_scope).startswith("acct_"))
        _require(type(livemode) is bool)
        _require(
            getattr(self.settings, "stripe_mode", "test")
            == ("live" if livemode else "test"),
            "payment_purchases_disabled",
            503,
        )
        pack = _pack(pack)
        with self.repo.transaction() as connection:
            lock_account_gate(connection)
            self._enabled(connection, livemode=livemode)
            user = self._user(connection, principal.owner, active=True)
            prior = (
                connection.execute(
                    select(payments)
                    .where(
                        payments.c.tenant == self.settings.tenant_id,
                        payments.c.user_id == user["id"],
                        payments.c.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            now = self.repo.clock()
            if prior is not None:
                row = dict(prior)
                _require(
                    row["pack_id"] == pack["id"]
                    and row["account_scope"] == account_scope
                    and row["livemode"] == int(livemode),
                    "payment_idempotency_conflict",
                    409,
                )
            else:
                if not callable(self.terms_check):
                    raise PaymentError("payment_terms_unavailable", 503)
                terms = self.terms_check(connection, principal, user)
                _require(
                    type(terms) is dict and set(terms) == {"version", "accepted_at"},
                    "payment_terms_unavailable",
                    503,
                )
                _identifier(terms["version"], 64)
                _clock(terms["accepted_at"])
                _require(
                    user["terms_version"] == terms["version"]
                    and user["terms_accepted_at"] == terms["accepted_at"] <= now,
                    "payment_terms_required",
                    409,
                )
                row = dict(
                    id=str(uuid.uuid4()),
                    tenant=self.settings.tenant_id,
                    user_id=user["id"],
                    pack_id=pack["id"],
                    pack_revision=pack["revision"],
                    pack_snapshot=pack,
                    price_id=pack["price_id"],
                    credits=pack["credits"],
                    amount_cents=pack["amount_cents"],
                    currency=pack["currency"],
                    account_scope=account_scope,
                    livemode=int(livemode),
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_hash(
                        {
                            "user_id": user["id"],
                            "pack": pack,
                            "account_scope": account_scope,
                            "livemode": livemode,
                            "terms": terms,
                        }
                    ),
                    terms_version=terms["version"],
                    terms_accepted_at=terms["accepted_at"],
                    status="created",
                    created_at=now,
                    updated_at=now,
                    checkout_started_at=now,
                    checkout_bound_at=None,
                    stripe_checkout_session_id=None,
                    stripe_payment_intent_id=None,
                    stripe_charge_id=None,
                    checkout_url=None,
                    expires_at=None,
                    paid_at=None,
                    receipt_url=None,
                    refunded_cents=0,
                    refund_credits=0,
                    clawed_back_credits=0,
                    unmet_credits=0,
                    dispute_open=0,
                    stripe_dispute_id=None,
                )
                connection.execute(insert(payments).values(**row))
            if connection.execute(
                select(payments.c.id)
                .where(
                    payments.c.tenant == row["tenant"],
                    payments.c.user_id == row["user_id"],
                    payments.c.dispute_open == 1,
                )
                .limit(1)
            ).first():
                raise PaymentError("purchases_suspended", 403)
            if row["paid_at"] is not None:
                raise PaymentError("checkout_already_completed", 409)
            if row["status"] in {"failed", "expired", "refunded", "disputed"}:
                raise PaymentError("checkout_not_open", 409)
            if row["expires_at"] is not None and row["expires_at"] <= now:
                raise PaymentError("checkout_expired", 409)
            dispatch = row["stripe_checkout_session_id"] is None
            if dispatch and now - row["checkout_started_at"] >= CHECKOUT_REPLAY_SECONDS:
                raise PaymentError("checkout_reconciliation_required", 409)
            return {
                "payment": self._state(row),
                "dispatch": dispatch,
                "stripe_idempotency_key": "inkseq-checkout-" + row["id"],
            }

    @staticmethod
    def _proof(row, obj, *, full=False):
        expected = {
            "payment_id": row["id"],
            "user_id": row["user_id"],
            "pack_id": row["pack_id"],
            "pack_revision": row["pack_revision"],
            "credits": row["credits"],
            "price_id": row["price_id"],
            "amount_cents": row["amount_cents"],
            "currency": row["currency"],
        }
        if full and (
            not set(expected) <= set(obj)
            or "session_id" not in obj
            or "payment_status" not in obj
        ):
            raise PaymentError("payment_original_proof_missing", 409)
        try:
            amount = original_price_amount(obj)
        except ValueError:
            raise PaymentError("payment_original_mismatch", 409) from None
        if (
            any(
                key in obj and obj[key] != value
                for key, value in expected.items()
                if key != "amount_cents"
            )
            or "amount_cents" in obj
            and amount != row["amount_cents"]
        ):
            raise PaymentError("payment_original_mismatch", 409)
        columns = {
            "session_id": "stripe_checkout_session_id",
            "payment_intent_id": "stripe_payment_intent_id",
            "charge_id": "stripe_charge_id",
            "dispute_id": "stripe_dispute_id",
        }
        for key, column in columns.items():
            if key in obj and row[column] not in (None, obj[key]):
                raise PaymentError("payment_original_mismatch", 409)
        return {column: obj[key] for key, column in columns.items() if key in obj}

    def bind_checkout(self, payment_id, request_fingerprint, session):
        _uuid(payment_id)
        value = _context(session)
        with self.repo.transaction() as connection:
            lock_account_gate(connection)
            self._enabled(connection, livemode=value["livemode"])
            initial = self._payment(connection, payment_id)
            _require(
                initial["request_fingerprint"] == request_fingerprint
                and initial["account_scope"] == value["account_scope"]
                and initial["livemode"] == int(value["livemode"]),
                "payment_original_mismatch",
                409,
            )
            obj = value["object"]
            patch = self._proof(initial, obj, full=True)
            _require(
                "checkout_url" in obj and "expires_at" in obj,
                "checkout_proof_missing",
                409,
            )
            if initial["checkout_url"] is not None:
                _require(
                    initial["checkout_url"] == obj["checkout_url"],
                    "payment_original_mismatch",
                    409,
                )
            patch.update(
                checkout_url=obj["checkout_url"],
                expires_at=obj["expires_at"],
                checkout_bound_at=initial["checkout_bound_at"] or self.repo.clock(),
                updated_at=self.repo.clock(),
            )
            connection.execute(
                update(payments).where(payments.c.id == payment_id).values(**patch)
            )
            return self._state(dict(initial, **patch))

    def mark_checkout_failed(self, payment_id, request_fingerprint):
        """Adapter calls only on a definite provider rejection, never a timeout."""
        with self.repo.transaction() as connection:
            lock_account_gate(connection)
            self._enabled(connection)
            row = self._payment(connection, _uuid(payment_id))
            self._enabled(connection, livemode=bool(row["livemode"]))
            _require(
                row["request_fingerprint"] == request_fingerprint,
                "payment_original_mismatch",
                409,
            )
            if row["stripe_checkout_session_id"] is None and row["paid_at"] is None:
                connection.execute(
                    update(payments)
                    .where(payments.c.id == row["id"])
                    .values(status="failed", updated_at=self.repo.clock())
                )
            return {"payment_id": row["id"], "state": "checkout_rejected"}

    def mark_checkout_unknown(self, payment_id, request_fingerprint, _reason=None):
        """Retain the one original request; adapter errors never enter this row.

        The immutable start time and missing session are the unknown-outcome
        journal. A retry uses the SAME original Stripe key within its window.
        """
        with self.repo.transaction() as connection:
            lock_account_gate(connection)
            self._enabled(connection)
            row = self._payment(connection, _uuid(payment_id))
            self._enabled(connection, livemode=bool(row["livemode"]))
            _require(
                row["request_fingerprint"] == request_fingerprint,
                "payment_original_mismatch",
                409,
            )
            connection.execute(
                update(payments)
                .where(payments.c.id == row["id"])
                .values(updated_at=self.repo.clock())
            )
            return {
                "payment_id": row["id"],
                "state": row["status"]
                if row["paid_at"] is not None
                else "checkout_confirmation_pending",
            }

    def receive_event(self, verified):
        """Only a verified SDK DTO reaches here. Receipt and durable wake commit together."""
        value = _context(verified, event=True)
        fingerprint = request_hash(value)
        with self.repo.transaction() as connection:
            lock_account_gate(connection)
            self._enabled(connection, livemode=value["livemode"])
            transport = self.repo.generation_transport
            if transport is None or not callable(
                getattr(transport, "payment_event", None)
            ):
                raise PaymentError("payment_transport_unavailable", 503)
            # Conflict-safe insert establishes one original event before DBOS.
            if connection.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as upsert
            else:
                from sqlalchemy.dialects.sqlite import insert as upsert
            connection.execute(
                upsert(events)
                .values(
                    event_id=value["id"],
                    tenant=self.settings.tenant_id,
                    account_scope=value["account_scope"],
                    livemode=int(value["livemode"]),
                    type=value["type"],
                    verified_payload=value,
                    fingerprint=fingerprint,
                    received_at=self.repo.clock(),
                    processed_at=None,
                    status="received",
                    payment_id=None,
                    reason_code=None,
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            row = (
                connection.execute(
                    select(events)
                    .where(events.c.event_id == value["id"])
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            _require(
                row["tenant"] == self.settings.tenant_id
                and row["fingerprint"] == fingerprint,
                "payment_event_conflict",
                409,
            )
            if row["status"] not in {"processed", "review"}:
                transport.payment_event(
                    connection, self.settings.tenant_id, value["id"]
                )
            return {"event_id": value["id"], "state": row["status"]}

    def _finish_event(self, connection, event, state, reason=None, payment_id=None):
        connection.execute(
            update(events)
            .where(events.c.event_id == event["event_id"])
            .values(
                status=state,
                reason_code=reason,
                payment_id=payment_id,
                processed_at=self.repo.clock()
                if state in {"processed", "review"}
                else None,
            )
        )
        return {
            "event_id": event["event_id"],
            "state": state,
            "done": state in {"processed", "review"},
            **({"wait_seconds": 10} if state == "deferred" else {}),
        }

    def _find_payment(self, connection, obj, scope, livemode):
        columns = {
            "payment_id": payments.c.id,
            "session_id": payments.c.stripe_checkout_session_id,
            "payment_intent_id": payments.c.stripe_payment_intent_id,
            "charge_id": payments.c.stripe_charge_id,
            "dispute_id": payments.c.stripe_dispute_id,
        }
        selectors = [
            column == obj[key] for key, column in columns.items() if key in obj
        ]
        if not selectors:
            return None
        rows = list(
            connection.execute(
                select(payments)
                .where(
                    payments.c.tenant == self.settings.tenant_id,
                    payments.c.account_scope == scope,
                    payments.c.livemode == int(livemode),
                    or_(*selectors),
                )
                .limit(2)
            ).mappings()
        )
        if len(rows) > 1:
            raise PaymentError("payment_correlation_ambiguous", 409)
        return dict(rows[0]) if rows else None

    @staticmethod
    def _status(row):
        return (
            "disputed"
            if row["dispute_open"]
            else "refunded"
            if row["refunded_cents"]
            else "paid"
            if row["paid_at"] is not None
            else row["status"]
        )

    def _clawback(self, connection, row, credits, refund_target):
        if not credits:
            return 0
        from .credits import _account, _change

        account = _account(
            connection,
            row["tenant"],
            row["user_id"],
            now=self.repo.clock(),
            create=True,
        )
        actual = min(credits, account["balance"] - account["held"])
        if actual:
            _change(
                connection,
                account,
                kind="refund",
                amount=-actual,
                held_delta=0,
                reference_type="payment",
                reference_id=row["id"],
                actor="stripe:webhook",
                reason="Stripe refund credit clawback",
                idempotency_key="stripe-refund:"
                + request_hash({"payment_id": row["id"], "target": refund_target}),
                now=self.repo.clock(),
                origin="stripe-live" if row["livemode"] else "stripe-test",
                receipt_url=row["receipt_url"],
            )
        return actual

    def event_for_processing(self, tenant, event_id):
        _require(tenant == self.settings.tenant_id, "payment_event_not_found", 404)
        with self.repo.engine.connect() as connection:
            self._enabled(connection)
            event = (
                connection.execute(
                    select(events).where(
                        events.c.tenant == tenant, events.c.event_id == event_id
                    )
                )
                .mappings()
                .first()
            )
            if event is None:
                raise PaymentError("payment_event_not_found", 404)
            self._enabled(connection, livemode=bool(event["livemode"]))
            return (
                None
                if event["status"] in {"processed", "review"}
                else copy.deepcopy(event["verified_payload"])
            )

    def defer_event(self, tenant, event_id):
        """Finite SDK/configuration diagnostic; never store upstream error text."""
        _require(tenant == self.settings.tenant_id, "payment_event_not_found", 404)
        with self.repo.transaction() as connection:
            lock_account_gate(connection)
            self._enabled(connection)
            event = (
                connection.execute(
                    select(events)
                    .where(events.c.tenant == tenant, events.c.event_id == event_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if event is None:
                raise PaymentError("payment_event_not_found", 404)
            self._enabled(connection, livemode=bool(event["livemode"]))
            if event["status"] in {"processed", "review"}:
                return {"event_id": event_id, "state": event["status"], "done": True}
            return self._finish_event(
                connection,
                event,
                "review"
                if self.repo.clock() - event["received_at"] >= DEFER_SECONDS
                else "deferred",
                "payment_processing_unavailable",
                event["payment_id"],
            )

    def process_event(self, tenant, event_id, *, proof=None):
        _require(tenant == self.settings.tenant_id, "payment_event_not_found", 404)
        with self.repo.transaction() as connection:
            lock_account_gate(connection)
            self._enabled(connection)
            event = (
                connection.execute(
                    select(events)
                    .where(events.c.tenant == tenant, events.c.event_id == event_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if event is None:
                raise PaymentError("payment_event_not_found", 404)
            self._enabled(connection, livemode=bool(event["livemode"]))
            if event["status"] in {"processed", "review"}:
                return {"event_id": event_id, "state": event["status"], "done": True}
            value = _context(event["verified_payload"], event=True)
            _require(
                value["account_scope"] == event["account_scope"]
                and int(value["livemode"]) == event["livemode"],
                "payment_event_conflict",
                409,
            )
            if proof is not None:
                resolved = _context(proof, event=True)
                _require(
                    all(
                        resolved[field] == value[field]
                        for field in (
                            "id",
                            "type",
                            "created",
                            "account_scope",
                            "livemode",
                        )
                    )
                    and all(
                        resolved["object"].get(key) == item
                        for key, item in value["object"].items()
                    ),
                    "payment_resolved_proof_conflict",
                    409,
                )
                value = resolved
            obj = value["object"]
            try:
                original = self._find_payment(
                    connection, obj, value["account_scope"], value["livemode"]
                )
                if original is None:
                    return self._finish_event(
                        connection,
                        event,
                        "review"
                        if self.repo.clock() - event["received_at"] >= DEFER_SECONDS
                        else "deferred",
                        "payment_correlation_unavailable",
                    )
                # The same user row precedes payment/wallet locks on all checkout
                # and event writes. Suspension never discards a confirmed payment.
                user = self._user(connection, user_id=original["user_id"])
                row = self._payment(connection, original["id"])
                if (
                    value["type"]
                    in {
                        "checkout.session.completed",
                        "checkout.session.async_payment_succeeded",
                    }
                    and "payment_status" not in obj
                ):
                    return self._finish_event(
                        connection,
                        event,
                        "review"
                        if self.repo.clock() - event["received_at"] >= DEFER_SECONDS
                        else "deferred",
                        "payment_verification_required",
                        row["id"],
                    )
                paid = (
                    value["type"]
                    in {
                        "checkout.session.completed",
                        "checkout.session.async_payment_succeeded",
                    }
                    and obj.get("payment_status") == "paid"
                )
                if paid and not {
                    "payment_id",
                    "user_id",
                    "pack_id",
                    "pack_revision",
                    "credits",
                    "price_id",
                    "amount_cents",
                    "currency",
                    "session_id",
                } <= set(obj):
                    return self._finish_event(
                        connection,
                        event,
                        "review"
                        if self.repo.clock() - event["received_at"] >= DEFER_SECONDS
                        else "deferred",
                        "payment_verification_required",
                        row["id"],
                    )
                patch = self._proof(row, obj, full=paid)
                row.update(patch)
                if "receipt_url" in obj:
                    row["receipt_url"] = obj["receipt_url"]
            except PaymentError as error:
                return self._finish_event(connection, event, "review", error.code)
            kind = value["type"]
            now = self.repo.clock()
            if kind in {
                "checkout.session.completed",
                "checkout.session.async_payment_succeeded",
            }:
                if obj.get("payment_status") == "paid" and row["paid_at"] is None:
                    from .credits import grant

                    grant(
                        connection,
                        tenant,
                        user["owner"],
                        row["credits"],
                        actor="stripe:webhook",
                        reason="Stripe credit pack purchase",
                        idempotency_key="stripe-purchase:" + obj["session_id"],
                        now=now,
                        kind="purchase",
                        reference_type="payment",
                        reference_id=row["id"],
                        origin="stripe-live" if row["livemode"] else "stripe-test",
                        receipt_url=row["receipt_url"],
                    )
                    row["paid_at"] = now
                    # A refund can arrive before the paid session. Preserve both
                    # original ledger facts atomically, with no visible free credits.
                    row["clawed_back_credits"] += self._clawback(
                        connection, row, row["refund_credits"], row["refund_credits"]
                    )
                    row["unmet_credits"] = (
                        row["refund_credits"] - row["clawed_back_credits"]
                    )
                    self._audit(
                        connection,
                        row,
                        "payment credited",
                        {
                            "credits": row["credits"],
                            "amount_cents": row["amount_cents"],
                            "currency": row["currency"],
                            "gross_amount_cents": obj["amount_cents"],
                            **{
                                key: obj[key]
                                for key in (
                                    "base_amount_cents",
                                    "tax_amount_cents",
                                    "tax_behavior",
                                )
                                if key in obj
                            },
                        },
                        now,
                    )
            elif kind == "checkout.session.async_payment_failed":
                if row["paid_at"] is None:
                    row["status"] = "failed"
            elif kind == "checkout.session.expired":
                if row["paid_at"] is None:
                    row["status"] = "expired"
            elif kind == "charge.refunded":
                if not {"amount_cents", "currency", "amount_refunded"} <= set(obj):
                    return self._finish_event(
                        connection,
                        event,
                        "review",
                        "payment_refund_proof_missing",
                        row["id"],
                    )
                try:
                    refunded = refund_base_amount(obj)
                except ValueError as error:
                    return self._finish_event(
                        connection, event, "review", str(error), row["id"]
                    )
                if refunded > row["amount_cents"]:
                    return self._finish_event(
                        connection,
                        event,
                        "review",
                        "payment_refund_exceeds_original",
                        row["id"],
                    )
                cumulative = max(row["refunded_cents"], refunded)
                target = (row["credits"] * cumulative + row["amount_cents"] - 1) // row[
                    "amount_cents"
                ]
                newly_refunded = target - row["refund_credits"]
                if cumulative > row["refunded_cents"]:
                    # Previously unmet debt is not silently debited from a later
                    # top-up. Only this newly proved refund increment is attempted.
                    if row["paid_at"] is not None:
                        row["clawed_back_credits"] += self._clawback(
                            connection, row, newly_refunded, target
                        )
                    row.update(
                        refunded_cents=cumulative,
                        refund_credits=target,
                        unmet_credits=target - row["clawed_back_credits"],
                    )
                    self._audit(
                        connection,
                        row,
                        "payment refund recorded",
                        {
                            "refunded_cents": cumulative,
                            "gross_refunded_cents": obj["amount_refunded"],
                            "gross_amount_cents": obj["amount_cents"],
                            "refund_credits": target,
                            "clawed_back_credits": row["clawed_back_credits"],
                            "unmet_credits": row["unmet_credits"],
                        },
                        now,
                    )
            elif kind == "charge.dispute.created":
                if "dispute_id" not in obj:
                    return self._finish_event(
                        connection,
                        event,
                        "review",
                        "payment_dispute_proof_missing",
                        row["id"],
                    )
                closed = connection.execute(
                    select(events.c.verified_payload)
                    .where(
                        events.c.tenant == tenant,
                        events.c.payment_id == row["id"],
                        events.c.type == "charge.dispute.closed",
                        events.c.status == "processed",
                    )
                    .limit(1)
                ).scalar_one_or_none()
                already_won = (
                    closed is not None
                    and closed["object"].get("dispute_id") == obj["dispute_id"]
                    and closed["object"].get("dispute_won") is True
                )
                if not already_won and not row["dispute_open"]:
                    row["dispute_open"] = 1
                    self._audit(
                        connection,
                        row,
                        "purchases suspended",
                        {"dispute_open": True},
                        now,
                    )
            elif kind == "charge.dispute.closed":
                if "dispute_id" not in obj or "dispute_won" not in obj:
                    return self._finish_event(
                        connection,
                        event,
                        "review",
                        "payment_dispute_proof_missing",
                        row["id"],
                    )
                previous = row["dispute_open"]
                row["dispute_open"] = 0 if obj["dispute_won"] else 1
                if row["dispute_open"] != previous:
                    self._audit(
                        connection,
                        row,
                        "purchases suspended"
                        if row["dispute_open"]
                        else "purchases restored",
                        {"dispute_open": bool(row["dispute_open"])},
                        now,
                    )
            row["status"] = self._status(row)
            row["updated_at"] = now
            connection.execute(
                update(payments)
                .where(payments.c.id == row["id"])
                .values(
                    **{
                        key: row[key]
                        for key in (
                            "stripe_checkout_session_id",
                            "stripe_payment_intent_id",
                            "stripe_charge_id",
                            "stripe_dispute_id",
                            "receipt_url",
                            "paid_at",
                            "status",
                            "refunded_cents",
                            "refund_credits",
                            "clawed_back_credits",
                            "unmet_credits",
                            "dispute_open",
                            "updated_at",
                        )
                    }
                )
            )
            return self._finish_event(
                connection,
                event,
                "processed",
                "payment_refund_requires_review"
                if row["paid_at"] is not None and row["unmet_credits"]
                else None,
                row["id"],
            )
