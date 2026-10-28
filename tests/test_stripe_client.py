"""Official SDK signing and closed Managed Payments/test-mode boundaries."""

from dataclasses import replace
import copy
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import stripe

from studio_platform.settings import Settings
from studio_platform.stripe_client import API_VERSION, StripeGateway, StripeGatewayError
from studio_platform.stripe_config import load_credentials, validate_credentials

USER = "11111111-1111-4111-8111-111111111111"
PAYMENT = "22222222-2222-4222-8222-222222222222"
CONFIG = {
    "version": 1,
    "account_id": "acct_testaccount",
    "secret_key": "sk_test_" + "s" * 32,
    "publishable_key": "pk_test_" + "p" * 32,
    "webhook_secret": "whsec_" + "w" * 32,
}


def payment():
    return {
        "id": PAYMENT,
        "user_id": USER,
        "pack_id": "starter",
        "pack_revision": "r1",
        "price_id": "price_starter",
        "credits": 500,
        "amount_cents": 500,
        "currency": "usd",
        "account_scope": CONFIG["account_id"],
    }


def session(**changes):
    value = {
        "object": "checkout.session",
        "id": "cs_test_original",
        "livemode": False,
        "mode": "payment",
        "amount_total": 500,
        "amount_subtotal": 500,
        "total_details": {"amount_tax": 0, "amount_discount": 0, "amount_shipping": 0},
        "currency": "usd",
        "payment_status": "unpaid",
        "url": "https://checkout.stripe.com/c/pay/cs_test_original#opaque",
        "expires_at": 2000000000,
        "metadata": {
            "payment_id": PAYMENT,
            "user_id": USER,
            "pack_id": "starter",
            "pack_revision": "r1",
            "credits": "500",
        },
    }
    value.update(changes)
    return value


class StripeClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "stripe.json"
        self.path.write_text(json.dumps(CONFIG))
        self.path.chmod(0o600)
        self.settings = Settings(
            data_dir=self.root / "data",
            deployment_target="staging",
            public_origin="https://staging.inkseq.com",
            stripe_mode="test",
            stripe_config_file=self.path,
        )
        self.client = Mock()
        self.client.v1.prices.retrieve.return_value = {
            "id": "price_starter",
            "active": True,
            "livemode": False,
            "type": "one_time",
            "unit_amount": 500,
            "currency": "usd",
            "tax_behavior": "exclusive",
        }
        self.client.v1.checkout.sessions.create.return_value = session()
        self.factory = Mock(return_value=self.client)
        self.gateway = StripeGateway(self.settings, client_factory=self.factory)

    def signed(self, obj=None, kind="checkout.session.completed", **changes):
        event = {
            "object": "event",
            "id": "evt_original",
            "type": kind,
            "created": 1900000000,
            "livemode": False,
            "api_version": API_VERSION,
            "data": {"object": obj or session()},
        }
        event.update(changes)
        raw = json.dumps(event).encode()
        signature = stripe.WebhookSignature.generate_signature_header(
            raw.decode(), CONFIG["webhook_secret"]
        )
        return raw, signature

    def test_test_only_settings_and_closed_private_configuration(self):
        for changes in (
            {"stripe_mode": "live"},
            {"deployment_target": "production"},
            {"public_origin": "https://inkseq.com"},
            {"public_origin_aliases": ("https://other.example",)},
            {"stripe_config_file": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.settings, **changes)
        with self.assertRaisesRegex(ValueError, "stripe_configuration_invalid"):
            validate_credentials({**CONFIG, "secret_key": "sk_live_" + "l" * 32})
        self.assertNotIn(CONFIG["secret_key"], repr(load_credentials(self.path)))
        self.path.write_text(json.dumps(CONFIG)[:-1] + ',"secret_key":"ignored"}')
        with self.assertRaisesRegex(ValueError, "stripe_configuration_invalid"):
            load_credentials(self.path)

    def test_off_blocks_new_purchase_but_retains_signed_obligation_processing(self):
        gateway = StripeGateway(
            replace(self.settings, stripe_mode="off"), client_factory=self.factory
        )
        with self.assertRaisesRegex(StripeGatewayError, "billing_unavailable"):
            gateway.checkout(payment(), idempotency_key="original")
        self.assertEqual(gateway.verify_event(*self.signed())["id"], "evt_original")
        self.factory.assert_not_called()

    def live_gateway(self, *, mode="live"):
        live = {
            **CONFIG,
            "version": 2,
            "mode": "live",
            "account_id": "acct_liveaccount",
            "publishable_key": None,
            "secret_key": "rk_live_" + "l" * 32,
            "webhook_endpoint_id": "we_liveendpoint",
        }
        self.path.write_text(json.dumps(live))
        self.live_config = live
        settings = replace(
            self.settings,
            deployment_target="production",
            public_origin="https://inkseq.com",
            public_origin_aliases=("https://sixnine.art", "https://www.sixnine.art"),
            stripe_mode=mode,
        )
        return StripeGateway(settings, client_factory=self.factory)

    def live_session(self, **changes):
        return session(
            id="cs_live_original",
            livemode=True,
            url="https://checkout.stripe.com/c/pay/cs_live_original#opaque",
            **changes,
        )

    def live_payment(self):
        return {**payment(), "account_scope": "acct_liveaccount", "livemode": True}

    def original_charge_session(
        self, *, live=False, behavior="inclusive", gross=500, tax=0
    ):
        """Actual single-Price expansion, including archived accepted Prices."""
        original = (self.live_session if live else session)(
            payment_status="paid",
            amount_total=gross,
            total_details={
                "amount_tax": tax,
                "amount_discount": 0,
                "amount_shipping": 0,
            },
            payment_intent={"id": "pi_original", "latest_charge": "ch_original"},
            line_items={
                "has_more": False,
                "data": [
                    {
                        "quantity": 1,
                        "price": {
                            "id": "price_starter",
                            "livemode": live,
                            "currency": "usd",
                            "unit_amount": 500,
                            "type": "one_time",
                            "tax_behavior": behavior,
                            "active": False,
                        },
                    }
                ],
            },
        )
        self.client.v1.checkout.sessions.list.return_value = {
            "has_more": False,
            "data": [{"id": original["id"]}],
        }
        self.client.v1.checkout.sessions.retrieve.return_value = original
        return original

    def test_live_hosted_checkout_is_exact_mode_account_and_canonical_origin_bound(
        self,
    ):
        gateway = self.live_gateway()
        self.client.v1.prices.retrieve.return_value["livemode"] = True
        self.client.v1.checkout.sessions.create.return_value = self.live_session()
        result = gateway.checkout(self.live_payment(), idempotency_key="original-live")
        self.assertIs(result["livemode"], True)
        self.assertEqual(result["account_scope"], "acct_liveaccount")
        params = self.client.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(
            params["success_url"],
            "https://inkseq.com/settings?tab=credits&paid={CHECKOUT_SESSION_ID}",
        )
        self.assertEqual(
            params["cancel_url"], "https://inkseq.com/settings?tab=credits"
        )
        self.client.reset_mock()
        for changed in (
            {"account_scope": "acct_other"},
            {"livemode": False},
            {"livemode": 1},
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(StripeGatewayError, "configuration_invalid"),
            ):
                gateway.checkout(
                    {**self.live_payment(), **changed}, idempotency_key="cross-context"
                )
        self.client.v1.checkout.sessions.create.assert_not_called()
        self.client.v1.prices.retrieve.return_value["livemode"] = False
        with self.assertRaisesRegex(StripeGatewayError, "price_mismatch"):
            gateway.checkout(self.live_payment(), idempotency_key="wrong-price-mode")

    def test_live_off_preserves_signed_events_and_refuses_cross_mode_objects(self):
        gateway = self.live_gateway(mode="off")
        with self.assertRaisesRegex(StripeGatewayError, "billing_unavailable"):
            gateway.checkout(self.live_payment(), idempotency_key="off")
        verified = gateway.verify_event(
            *self.signed(self.live_session(), livemode=True, account="acct_liveaccount")
        )
        self.assertIs(verified["livemode"], True)
        self.assertEqual(gateway.resolve_event(verified), verified)
        for obj, changes in (
            (self.live_session(), {"livemode": False}),
            (session(), {"livemode": True}),
            (self.live_session(), {"livemode": True, "account": "acct_otheraccount"}),
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(StripeGatewayError, "event_invalid"),
            ):
                gateway.verify_event(*self.signed(obj, **changes))
        with self.assertRaisesRegex(StripeGatewayError, "configuration_invalid"):
            gateway.resolve_event({**verified, "livemode": False})
        self.client.v1.checkout.sessions.create.assert_not_called()

    def test_live_key_never_enables_staging_and_test_key_never_enables_production(self):
        gateway = self.live_gateway(mode="off")
        for changes in (
            {
                "deployment_target": "staging",
                "public_origin": "https://staging.inkseq.com",
                "public_origin_aliases": (),
            },
            {"public_origin": "https://www.inkseq.com", "public_origin_aliases": ()},
            {"public_origin_aliases": ("https://untrusted.example",)},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(StripeGatewayError, "billing_unavailable"),
            ):
                StripeGateway(
                    replace(gateway.settings, **changes), client_factory=self.factory
                ).configuration()
        self.path.write_text(json.dumps(CONFIG))
        with self.assertRaisesRegex(StripeGatewayError, "billing_unavailable"):
            gateway.configuration()
        self.factory.assert_not_called()

    def test_live_dispute_charge_proof_refuses_test_mode_provider_charge(self):
        gateway = self.live_gateway(mode="off")
        dispute = {
            "object": "dispute",
            "id": "dp_original",
            "livemode": True,
            "charge": "ch_original",
        }
        verified = gateway.verify_event(
            *self.signed(dispute, kind="charge.dispute.created", livemode=True)
        )
        self.client.v1.charges.retrieve.return_value = {
            "object": "charge",
            "id": "ch_original",
            "livemode": False,
            "amount": 500,
            "currency": "usd",
            "payment_intent": "pi_original",
        }
        with self.assertRaisesRegex(StripeGatewayError, "event_invalid"):
            gateway.resolve_event(verified)
        self.client.v1.charges.retrieve.return_value["livemode"] = True
        self.original_charge_session(live=True)
        self.assertEqual(gateway.resolve_event(verified)["object"]["amount_cents"], 500)

    def test_readonly_configuration_can_inspect_inert_live_but_never_bypass_checkout_webhook(
        self,
    ):
        gateway = self.live_gateway()
        self.path.write_text(
            json.dumps(
                {
                    **self.live_config,
                    "webhook_secret": None,
                    "webhook_endpoint_id": None,
                }
            )
        )
        config = gateway.configuration(require_webhook=False)
        self.assertEqual(config.mode, "live")
        self.assertIsNone(config.webhook_secret)
        for kwargs in ({}, {"for_checkout": True, "require_webhook": False}):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaisesRegex(StripeGatewayError, "webhook_unavailable"),
            ):
                gateway.configuration(**kwargs)
        # The status exception does not relax production origin/mode restrictions.
        unsafe = replace(
            gateway.settings,
            stripe_mode="off",
            public_origin="https://staging.inkseq.com",
        )
        with self.assertRaisesRegex(StripeGatewayError, "billing_unavailable"):
            StripeGateway(unsafe).configuration(require_webhook=False)
        self.factory.assert_not_called()

    def test_managed_checkout_uses_original_price_key_and_supported_parameters_only(
        self,
    ):
        result = self.gateway.checkout(
            payment(), idempotency_key="inkseq-checkout-original"
        )
        self.assertEqual(result["object"]["price_id"], "price_starter")
        call = self.client.v1.checkout.sessions.create.call_args.kwargs
        self.assertEqual(
            call["options"], {"idempotency_key": "inkseq-checkout-original"}
        )
        params = call["params"]
        self.assertEqual(
            set(params),
            {
                "mode",
                "managed_payments",
                "line_items",
                "client_reference_id",
                "metadata",
                "payment_intent_data",
                "success_url",
                "cancel_url",
            },
        )
        self.assertEqual(params["managed_payments"], {"enabled": True})
        self.assertEqual(
            params["line_items"], [{"price": "price_starter", "quantity": 1}]
        )
        self.assertEqual(
            params["payment_intent_data"], {"metadata": params["metadata"]}
        )
        self.assertEqual(params["metadata"]["payment_id"], PAYMENT)
        self.assertEqual(
            params["success_url"],
            "https://staging.inkseq.com/settings?tab=credits&paid={CHECKOUT_SESSION_ID}",
        )

    def test_catalog_mismatch_never_creates_checkout(self):
        for key, value in (
            ("unit_amount", 600),
            ("currency", "aud"),
            ("tax_behavior", "inclusive"),
            ("active", False),
            ("livemode", True),
        ):
            with self.subTest(key=key):
                price = self.client.v1.prices.retrieve.return_value
                old = price[key]
                price[key] = value
                with self.assertRaisesRegex(
                    StripeGatewayError, "billing_price_mismatch"
                ):
                    self.gateway.checkout(payment(), idempotency_key="original")
                price[key] = old
        self.client.v1.checkout.sessions.create.assert_not_called()

    def test_signature_precedes_normalization_and_no_http_or_pii_persistence(self):
        obj = session(
            customer_details={
                "email": "private@example.test",
                "address": {"line1": "never retain"},
            },
            customer="cus_secret",
        )
        result = self.gateway.verify_event(*self.signed(obj))
        self.assertEqual(result["object"]["user_id"], USER)
        self.assertNotIn("private@example", json.dumps(result))
        self.assertNotIn("customer", json.dumps(result))
        self.assertNotIn("price_id", result["object"])
        with self.assertRaisesRegex(StripeGatewayError, "billing_signature_invalid"):
            self.gateway.verify_event(b"not json", "t=1,v1=bad")
        self.factory.assert_not_called()

    def test_real_sdk_signature_rejects_tampered_and_expired_payloads(self):
        # No HTTP or fake Webhook implementation: exercise the pinned SDK verifier.
        raw, _ = self.signed()

        def signature(timestamp):
            digest = hmac.new(
                CONFIG["webhook_secret"].encode(),
                str(timestamp).encode() + b"." + raw,
                hashlib.sha256,
            ).hexdigest()
            return f"t={timestamp},v1={digest}"

        current = int(time.time())
        valid = signature(current)
        self.assertEqual(self.gateway.verify_event(raw, valid)["id"], "evt_original")
        for body, header in (
            (raw.replace(b"evt_original", b"evt_tampered"), valid),
            (raw, signature(current - 301)),
        ):
            with (
                self.subTest(body=body == raw),
                self.assertRaisesRegex(StripeGatewayError, "billing_signature_invalid"),
            ):
                self.gateway.verify_event(body, header)
        self.factory.assert_not_called()

    def test_live_or_version_mismatch_notifications_refused(self):
        for changes in (
            {"livemode": True},
            {"api_version": "2025-03-31.basil"},
            {"account": "acct_otheraccount"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(StripeGatewayError, "billing_event_invalid"),
            ):
                self.gateway.verify_event(*self.signed(**changes))

    def test_paid_proof_comes_from_single_actual_price_not_metadata_or_presentment(
        self,
    ):
        obj = session(
            payment_status="paid",
            url=None,
            payment_intent="pi_original",
            presentment_details={
                "presentment_amount": 900,
                "presentment_currency": "aud",
            },
        )
        verified = self.gateway.verify_event(*self.signed(obj))
        expanded = session(
            **{
                **obj,
                "line_items": {
                    "has_more": False,
                    "data": [
                        {
                            "quantity": 1,
                            "price": {
                                "id": "price_actual",
                                "livemode": False,
                                "currency": "usd",
                                "tax_behavior": "inclusive",
                                "unit_amount": 500,
                                "type": "one_time",
                            },
                        }
                    ],
                },
            }
        )
        self.client.v1.checkout.sessions.retrieve.return_value = expanded
        result = self.gateway.resolve_event(verified)
        self.assertEqual(result["object"]["price_id"], "price_actual")
        self.assertEqual(result["object"]["amount_cents"], 500)
        self.assertEqual(result["object"]["currency"], "usd")
        self.assertNotIn("presentment_details", result["object"])
        expanded["metadata"]["credits"] = "99999"
        with self.assertRaisesRegex(
            StripeGatewayError, "billing_provider_response_invalid"
        ):
            self.gateway.resolve_event(verified)

    def test_unpaid_completion_has_no_provider_fetch(self):
        verified = self.gateway.verify_event(*self.signed(session(url=None)))
        self.assertEqual(self.gateway.resolve_event(verified), verified)
        self.client.v1.checkout.sessions.retrieve.assert_not_called()

    def test_missing_completion_status_is_resolved_by_provider_proof(self):
        original = session(url=None)
        del original["payment_status"]
        verified = self.gateway.verify_event(*self.signed(original))
        expanded = session(
            url=None,
            payment_status="paid",
            line_items={
                "has_more": False,
                "data": [
                    {
                        "quantity": 1,
                        "price": {
                            "id": "price_actual",
                            "livemode": False,
                            "currency": "usd",
                            "tax_behavior": "inclusive",
                            "unit_amount": 500,
                            "type": "one_time",
                        },
                    }
                ],
            },
        )
        self.client.v1.checkout.sessions.retrieve.return_value = expanded
        proof = self.gateway.resolve_event(verified)
        self.assertEqual(proof["object"]["payment_status"], "paid")
        self.assertEqual(proof["object"]["price_id"], "price_actual")
        self.assertTrue(
            all(
                proof["object"][key] == value
                for key, value in verified["object"].items()
            )
        )

    def test_dispute_uses_original_charge_amount_not_partial_dispute_amount(self):
        obj = {
            "object": "dispute",
            "id": "dp_original",
            "livemode": False,
            "charge": "ch_original",
            "amount": 100,
            "currency": "usd",
        }
        verified = self.gateway.verify_event(
            *self.signed(obj, kind="charge.dispute.created")
        )
        self.assertNotIn("amount_cents", verified["object"])
        self.client.v1.charges.retrieve.return_value = {
            "object": "charge",
            "id": "ch_original",
            "livemode": False,
            "amount": 500,
            "currency": "usd",
            "payment_intent": "pi_original",
        }
        self.original_charge_session()
        self.assertEqual(
            self.gateway.resolve_event(verified)["object"]["amount_cents"], 500
        )

    def test_exclusive_full_refund_resolves_the_original_gross_and_base(self):
        original = self.original_charge_session(behavior="exclusive", gross=550, tax=50)
        charge = {
            "object": "charge",
            "id": "ch_original",
            "livemode": False,
            "amount": 550,
            "amount_refunded": 550,
            "currency": "usd",
            "payment_intent": "pi_original",
            "metadata": original["metadata"],
        }
        verified = self.gateway.verify_event(
            *self.signed(charge, kind="charge.refunded")
        )
        self.assertNotIn("base_amount_cents", verified["object"])
        proof = self.gateway.resolve_event(verified)
        self.assertEqual(proof["object"]["amount_cents"], 550)
        self.assertEqual(proof["object"]["amount_refunded"], 550)
        self.assertEqual(proof["object"]["base_amount_cents"], 500)
        self.assertEqual(proof["object"]["tax_amount_cents"], 50)
        self.assertTrue(
            all(
                proof["object"][key] == value
                for key, value in verified["object"].items()
            )
        )
        self.client.v1.checkout.sessions.list.assert_called_once_with(
            params={"payment_intent": "pi_original", "limit": 2}
        )
        self.client.v1.checkout.sessions.create.assert_not_called()

    def test_refund_original_lookup_refuses_missing_ambiguous_or_foreign_sessions(self):
        original = self.original_charge_session(behavior="exclusive", gross=550, tax=50)
        charge = {
            "object": "charge",
            "id": "ch_original",
            "livemode": False,
            "amount": 550,
            "amount_refunded": 550,
            "currency": "usd",
            "payment_intent": "pi_original",
            "metadata": copy.deepcopy(original["metadata"]),
        }
        verified = self.gateway.verify_event(
            *self.signed(charge, kind="charge.refunded")
        )
        for data, more in (
            ([], False),
            ([{"id": original["id"]}] * 2, False),
            ([{"id": original["id"]}], True),
        ):
            self.client.v1.checkout.sessions.list.return_value = {
                "has_more": more,
                "data": data,
            }
            with (
                self.subTest(data=data, more=more),
                self.assertRaises(StripeGatewayError),
            ):
                self.gateway.resolve_event(verified)
        self.client.v1.checkout.sessions.list.return_value = {
            "has_more": False,
            "data": [{"id": original["id"]}],
        }
        changes = (
            {"id": "cs_test_foreign"},
            {"livemode": True},
            {"currency": "aud"},
            {"payment_intent": {"id": "pi_foreign", "latest_charge": "ch_original"}},
            {"payment_intent": {"id": "pi_original", "latest_charge": "ch_foreign"}},
            {
                "metadata": {
                    **original["metadata"],
                    "payment_id": "33333333-3333-4333-8333-333333333333",
                }
            },
            {"amount_total": 549},
        )
        for changed in changes:
            self.client.v1.checkout.sessions.retrieve.return_value = {
                **copy.deepcopy(original),
                **changed,
            }
            with self.subTest(changed=changed), self.assertRaises(StripeGatewayError):
                self.gateway.resolve_event(verified)
        self.client.v1.checkout.sessions.create.assert_not_called()

    def test_new_checkout_keeps_gross_tax_separate_from_credit_pack_base(self):
        self.client.v1.checkout.sessions.create.return_value = session(
            amount_total=550,
            total_details={
                "amount_tax": 50,
                "amount_discount": 0,
                "amount_shipping": 0,
            },
        )
        result = self.gateway.checkout(
            payment(), idempotency_key="original-taxed-checkout"
        )
        self.assertEqual(result["object"]["amount_cents"], 550)
        self.assertEqual(result["object"]["base_amount_cents"], 500)
        self.assertEqual(result["object"]["tax_behavior"], "exclusive")
        self.assertEqual(result["object"]["tax_amount_cents"], 50)

    def test_provider_error_and_redirect_host_are_sanitized(self):
        self.client.v1.checkout.sessions.create.side_effect = stripe.APIConnectionError(
            "secret " + CONFIG["secret_key"]
        )
        with self.assertRaises(StripeGatewayError) as caught:
            self.gateway.checkout(payment(), idempotency_key="original")
        self.assertTrue(caught.exception.unknown)
        self.assertNotIn(
            CONFIG["secret_key"], str(caught.exception) + caught.exception.detail
        )
        self.client.v1.checkout.sessions.create.side_effect = None
        self.client.v1.checkout.sessions.create.return_value = session(
            url="https://checkout.stripe.com.attacker.test/pay/original"
        )
        with self.assertRaisesRegex(
            StripeGatewayError, "billing_provider_response_invalid"
        ):
            self.gateway.checkout(payment(), idempotency_key="original")

    def test_sdk_factory_pins_api_version_and_fixed_endpoint(self):
        from studio_platform.stripe_client import _default_client

        client = _default_client(load_credentials(self.path))
        self.addCleanup(client._requestor._client.close)
        self.assertEqual(client._requestor._options.stripe_version, API_VERSION)
        self.assertEqual(
            client._requestor._options.base_addresses["api"], "https://api.stripe.com"
        )
        self.assertEqual(client._requestor._options.max_network_retries, 0)


if __name__ == "__main__":
    unittest.main()
