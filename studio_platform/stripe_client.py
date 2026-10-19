"""Pinned Stripe test client and bounded provider DTOs, without ledger writes."""

import math
import re
from urllib.parse import urlsplit
import uuid

from .stripe_config import load_credentials

API_VERSION = "2026-09-30.endive"
ORIGIN = "https://staging.inkseq.com"
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
MAX_BODY = 512 * 1024
MAX_INTEGER = (1 << 63) - 1


class StripeGatewayError(Exception):
    def __init__(self, code, status=503, *, unknown=False):
        self.code, self.status, self.unknown = code, status, unknown
        messages = {
            "billing_unavailable": "Credit purchases are not enabled.",
            "billing_configuration_invalid": "Credit purchases are not configured correctly.",
            "billing_webhook_unavailable": "Payment confirmation is not configured yet.",
            "billing_price_mismatch": "This credit pack is unavailable. Please refresh or contact support.",
            "billing_provider_unavailable": "The payment provider is unavailable. Retry the same purchase request.",
            "billing_checkout_unknown": "Payment setup is still being checked. Retry the same request; do not create another purchase.",
            "billing_signature_invalid": "The payment notification signature is invalid.",
            "billing_event_invalid": "The payment notification is unsupported or invalid.",
            "billing_provider_response_invalid": "The payment provider returned an unexpected response.",
        }
        self.detail = messages.get(code, "The payment request could not be completed.")
        super().__init__(code)


def _get(value, key, default=None):
    try:
        return value[key]
    except (KeyError, TypeError):
        return default


def _id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(
        re.escape(prefix) + r"[A-Za-z0-9_]{1," + str(200 - len(prefix)) + "}", value
    ):
        raise StripeGatewayError("billing_event_invalid", 400)
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise StripeGatewayError("billing_event_invalid", 400) from None
    return value


def _integer(value):
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise StripeGatewayError("billing_event_invalid", 400)
    return value


def provider_url(value, *, receipt=False):
    hosts = {"pay.stripe.com"} if receipt else {"checkout.stripe.com"}
    try:
        parsed = urlsplit(value)
        valid = (
            isinstance(value, str)
            and 1 <= len(value) <= 2048
            and parsed.scheme == "https"
            and parsed.hostname in hosts
            and parsed.netloc == parsed.hostname
            and bool(parsed.path)
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise StripeGatewayError("billing_provider_response_invalid", 502)
    return value


def _metadata(obj, result):
    metadata = _get(obj, "metadata", {})
    for key in ("payment_id", "user_id"):
        value = _get(metadata, key)
        if value is not None:
            result[key] = _uuid(value)
    for key in ("pack_id", "pack_revision"):
        value = _get(metadata, key)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,64}", value
            ):
                raise StripeGatewayError("billing_event_invalid", 400)
            result[key] = value
    credits = _get(metadata, "credits")
    if credits is not None:
        if not isinstance(credits, str) or not re.fullmatch(r"[0-9]{1,19}", credits):
            raise StripeGatewayError("billing_event_invalid", 400)
        result["credits"] = _integer(int(credits))


def _currency_amount(obj, result, *, session=False):
    amount = _get(obj, "amount_total" if session else "amount")
    currency = _get(obj, "currency")
    if amount is not None:
        result["amount_cents"] = _integer(amount)
    if currency is not None:
        if currency != "usd":
            raise StripeGatewayError("billing_event_invalid", 400)
        result["currency"] = currency


def _session(obj):
    if (
        _get(obj, "object") != "checkout.session"
        or _get(obj, "livemode") is not False
        or _get(obj, "mode") != "payment"
    ):
        raise StripeGatewayError("billing_event_invalid", 400)
    result = {"session_id": _id(_get(obj, "id"), "cs_test_")}
    _metadata(obj, result)
    _currency_amount(obj, result, session=True)
    pi = _get(obj, "payment_intent")
    if pi is not None:
        result["payment_intent_id"] = _id(
            pi if isinstance(pi, str) else _get(pi, "id"), "pi_"
        )
    status = _get(obj, "payment_status")
    if status is not None:
        if status not in {"paid", "unpaid", "no_payment_required"}:
            raise StripeGatewayError("billing_event_invalid", 400)
        result["payment_status"] = status
    expires = _get(obj, "expires_at")
    if expires is not None:
        result["expires_at"] = _integer(expires)
    url = _get(obj, "url")
    if url is not None:
        result["checkout_url"] = provider_url(url)
    return result


def _charge(obj):
    if _get(obj, "object") != "charge" or _get(obj, "livemode") is not False:
        raise StripeGatewayError("billing_event_invalid", 400)
    result = {"charge_id": _id(_get(obj, "id"), "ch_")}
    _metadata(obj, result)
    _currency_amount(obj, result)
    pi = _get(obj, "payment_intent")
    if pi is not None:
        result["payment_intent_id"] = _id(
            pi if isinstance(pi, str) else _get(pi, "id"), "pi_"
        )
    refunded = _get(obj, "amount_refunded")
    if refunded is not None:
        result["amount_refunded"] = _integer(refunded)
        if result.get("amount_cents", MAX_INTEGER) < refunded:
            raise StripeGatewayError("billing_event_invalid", 400)
    receipt = _get(obj, "receipt_url")
    if receipt is not None:
        result["receipt_url"] = provider_url(receipt, receipt=True)
    return result


def _default_client(config):
    import stripe

    return stripe.StripeClient(
        config.secret_key,
        stripe_version=API_VERSION,
        base_addresses={"api": "https://api.stripe.com"},
        http_client=stripe.HTTPXClient(timeout=10, allow_sync_methods=True),
        max_network_retries=0,
    )


class StripeGateway:
    def __init__(self, settings, *, client_factory=None):
        self.settings = settings
        self.client_factory = client_factory or _default_client

    def configuration(self, *, for_checkout=False):
        if (
            getattr(self.settings, "stripe_mode", "off") not in {"off", "test"}
            or for_checkout
            and getattr(self.settings, "stripe_mode", "off") != "test"
            or self.settings.deployment_target != "staging"
            or self.settings.public_origin != ORIGIN
            or self.settings.public_origin_aliases
        ):
            raise StripeGatewayError("billing_unavailable")
        try:
            config = load_credentials(self.settings.stripe_config_file)
        except ValueError:
            raise StripeGatewayError("billing_configuration_invalid") from None
        if config is None:
            raise StripeGatewayError("billing_unavailable")
        if config.webhook_secret is None:
            raise StripeGatewayError("billing_webhook_unavailable")
        return config

    def checkout(self, payment, *, idempotency_key):
        """Call only with the core's persisted original payment and Stripe key."""
        config = self.configuration(for_checkout=True)
        if payment["account_scope"] != config.account_id:
            raise StripeGatewayError("billing_configuration_invalid")
        client = self.client_factory(config)
        import stripe

        try:
            price = client.v1.prices.retrieve(payment["price_id"])
            if (
                _get(price, "id") != payment["price_id"]
                or _get(price, "active") is not True
                or _get(price, "livemode") is not False
                or _get(price, "type") != "one_time"
                or _get(price, "unit_amount") != payment["amount_cents"]
                or _get(price, "currency") != payment["currency"]
                or _get(price, "tax_behavior") != "inclusive"
            ):
                raise StripeGatewayError("billing_price_mismatch", 409)
            metadata = {
                key: str(payment[key]) for key in ("user_id", "pack_id", "credits")
            }
            metadata.update(
                payment_id=payment["id"], pack_revision=payment["pack_revision"]
            )
            params = {
                "mode": "payment",
                "managed_payments": {"enabled": True},
                "line_items": [{"price": payment["price_id"], "quantity": 1}],
                "client_reference_id": payment["user_id"],
                "metadata": metadata,
                "payment_intent_data": {"metadata": metadata},
                "success_url": ORIGIN
                + "/settings?tab=credits&paid={CHECKOUT_SESSION_ID}",
                "cancel_url": ORIGIN + "/settings?tab=credits",
            }
            try:
                session = client.v1.checkout.sessions.create(
                    params=params, options={"idempotency_key": idempotency_key}
                )
            except stripe.StripeError:
                raise StripeGatewayError(
                    "billing_checkout_unknown", unknown=True
                ) from None
            result = _session(session)
            result["price_id"] = payment["price_id"]
            if not result.get("checkout_url"):
                raise StripeGatewayError(
                    "billing_provider_response_invalid", 502, unknown=True
                )
            return {
                "account_scope": config.account_id,
                "livemode": False,
                "object": result,
            }
        except stripe.StripeError:
            raise StripeGatewayError("billing_provider_unavailable") from None
        finally:
            client._requestor._client.close()

    def verify_event(self, raw, signature):
        """No HTTP or persistence. Signature validation precedes normalization."""
        config = self.configuration()
        if (
            not isinstance(raw, bytes)
            or not 1 <= len(raw) <= MAX_BODY
            or not isinstance(signature, str)
            or len(signature) > 4096
        ):
            raise StripeGatewayError("billing_signature_invalid", 400)
        import stripe

        try:
            event = stripe.Webhook.construct_event(
                raw, signature, config.webhook_secret, tolerance=300
            )
        except (stripe.SignatureVerificationError, ValueError, UnicodeError):
            raise StripeGatewayError("billing_signature_invalid", 400) from None
        if (
            _get(event, "livemode") is not False
            or _get(event, "api_version") != API_VERSION
            or _get(event, "account") not in (None, config.account_id)
        ):
            raise StripeGatewayError("billing_event_invalid", 400)
        kind = _get(event, "type")
        if kind not in EVENT_TYPES:
            return None
        obj = _get(_get(event, "data", {}), "object", {})
        if kind.startswith("checkout.session."):
            value = _session(obj)
        elif kind == "charge.refunded":
            value = _charge(obj)
        else:
            if _get(obj, "object") != "dispute" or _get(obj, "livemode") is not False:
                raise StripeGatewayError("billing_event_invalid", 400)
            charge = _get(obj, "charge")
            value = {
                "dispute_id": _id(_get(obj, "id"), "dp_"),
                "charge_id": _id(
                    charge if isinstance(charge, str) else _get(charge, "id"), "ch_"
                ),
            }
            if kind == "charge.dispute.closed":
                status = _get(obj, "status")
                if status not in {"won", "lost", "warning_closed"}:
                    raise StripeGatewayError("billing_event_invalid", 400)
                value["dispute_won"] = status == "won"
        created = _get(event, "created")
        if (
            not isinstance(created, (int, float))
            or isinstance(created, bool)
            or not math.isfinite(created)
            or created < 0
        ):
            raise StripeGatewayError("billing_event_invalid", 400)
        return {
            "id": _id(_get(event, "id"), "evt_"),
            "type": kind,
            "created": created,
            "livemode": False,
            "account_scope": config.account_id,
            "object": value,
        }

    def resolve_event(self, verified):
        """Existing DBOS worker calls this outside ledger transactions, never webhook."""
        config = self.configuration()
        if (
            verified["account_scope"] != config.account_id
            or verified["livemode"] is not False
        ):
            raise StripeGatewayError("billing_configuration_invalid")
        value = dict(verified["object"])
        client = self.client_factory(config)
        import stripe

        try:
            if verified["type"] == "checkout.session.async_payment_succeeded" or (
                verified["type"] == "checkout.session.completed"
                and value.get("payment_status") not in {"unpaid", "no_payment_required"}
            ):
                session = client.v1.checkout.sessions.retrieve(
                    value["session_id"],
                    params={
                        "expand": [
                            "line_items.data.price",
                            "payment_intent.latest_charge",
                        ]
                    },
                )
                resolved = _session(session)
                line_items = _get(session, "line_items", {})
                items = _get(line_items, "data", [])
                if (
                    _get(line_items, "has_more") is not False
                    or not isinstance(items, list)
                    or len(items) != 1
                    or _get(items[0], "quantity") != 1
                ):
                    raise StripeGatewayError("billing_provider_response_invalid", 502)
                price = _get(items[0], "price", {})
                if (
                    _get(price, "livemode") is not False
                    or _get(price, "currency") != "usd"
                    or _get(price, "tax_behavior") != "inclusive"
                ):
                    raise StripeGatewayError("billing_provider_response_invalid", 502)
                resolved["price_id"] = _id(_get(price, "id"), "price_")
                pi = _get(session, "payment_intent")
                charge = _get(pi, "latest_charge") if not isinstance(pi, str) else None
                if charge is not None and not isinstance(charge, str):
                    charge_values = _charge(charge)
                    resolved.update(
                        {
                            key: charge_values[key]
                            for key in ("charge_id", "receipt_url")
                            if key in charge_values
                        }
                    )
                if resolved["session_id"] != value["session_id"]:
                    raise StripeGatewayError("billing_provider_response_invalid", 502)
                if any(
                    key not in resolved or resolved[key] != original
                    for key, original in value.items()
                ):
                    raise StripeGatewayError("billing_provider_response_invalid", 502)
                value = resolved
            elif verified["type"].startswith("charge.dispute."):
                charge = _charge(client.v1.charges.retrieve(value["charge_id"]))
                if charge["charge_id"] != value["charge_id"]:
                    raise StripeGatewayError("billing_provider_response_invalid", 502)
                value = {**charge, **value}
            return {**verified, "object": value}
        except stripe.StripeError:
            raise StripeGatewayError("billing_provider_unavailable") from None
        finally:
            client._requestor._client.close()
