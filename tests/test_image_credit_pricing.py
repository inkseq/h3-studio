"""Explicit image pricing never rewrites legacy immutable video prices."""

from sqlalchemy import select

from studio_platform.credit_schema import prices
from studio_platform.credits_pricing import (
    DEFAULT_RATES,
    PricingError,
    current,
    image_price,
)
import test_credits_routes as fixtures
import unittest


class ImagePricingTests(fixtures.CreditsRouteTests):
    def image(self):
        with self.repo.engine.connect() as conn:
            return image_price(conn, "sixnine")

    def test_recommended_five_does_not_price_legacy_revision(self):
        revision = self.enable()
        before = self.service.admin_prices(self.admin)
        self.assertIsNone(before["image_credits"])
        self.assertEqual(before["recommended_image_credits"], 5)
        self.assertIsNone(self.image())
        with self.repo.engine.connect() as conn:
            original = conn.execute(select(prices.c.configuration)).scalar_one()
            self.assertNotIn("image_credits", original)
            self.assertEqual(current(conn, "sixnine")["revision"], revision)
        self.assertEqual(self.count(prices), 1)

    def test_explicit_price_publishes_audited_cas_revision(self):
        old = self.enable()
        body = {
            "expected_revision": old,
            "rates": DEFAULT_RATES,
            "image_credits": 5,
            "reason": "QA explicit image price",
        }
        result = self.service.publish(self.admin, body, key="image-five")
        self.assertEqual(
            self.service.publish(self.admin, body, key="image-five"), result
        )
        self.assertEqual(
            self.image(), {"credits": 5, "price_revision": result["revision"]}
        )
        with self.repo.engine.connect() as conn:
            configs = conn.execute(
                select(prices.c.revision, prices.c.configuration)
            ).all()
        self.assertEqual(len(configs), 2)
        self.assertNotIn("image_credits", dict(configs)[old])
        with self.assertRaisesRegex(PricingError, "credit_prices_changed"):
            self.service.publish(self.admin, {**body, "image_credits": 8})

    def test_off_read_creates_nothing_and_does_not_adopt_price(self):
        self.assertIsNone(self.image())
        self.assertEqual(self.count(prices), 0)
        first = self.publish(image_credits=5)
        self.assertIsNone(self.image())
        self.service.policy(
            self.admin,
            {
                "expected_revision": first["revision"],
                "enabled": True,
                "reason": "QA enable",
            },
            key="enable-image-price",
        )
        self.assertEqual(self.image()["credits"], 5)
        self.service.policy(
            self.admin,
            {
                "expected_revision": first["revision"],
                "enabled": False,
                "reason": "QA disable",
            },
            key="disable-image-price",
        )
        self.assertIsNone(self.image())
        self.assertEqual(self.count(prices), 1)

    def test_image_price_requires_positive_integer(self):
        for value in (0, -1, True, 1.5, "5", None, 1_000_001):
            with self.subTest(value=value), self.assertRaises(PricingError):
                self.publish(image_credits=value)
        self.assertEqual(self.count(prices), 0)

    def test_other_price_change_preserves_explicit_image_rate(self):
        first = self.publish(image_credits=7)
        self.service.publish(
            self.admin,
            {
                "expected_revision": first["revision"],
                "rates": {**DEFAULT_RATES, "standard": 9},
                "reason": "QA video-only change",
            },
        )
        self.assertEqual(self.service.admin_prices(self.admin)["image_credits"], 7)


def load_tests(loader, tests, pattern):
    # Reuse the fixture, not a second copy of the video-pricing test suite.
    return unittest.TestSuite(
        ImagePricingTests(name)
        for name in ImagePricingTests.__dict__
        if name.startswith("test_")
    )
