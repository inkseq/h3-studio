"""Waiting-time price ladder for demand starts; no provider, fake prices only."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform import dstack_demand_start as demand


class FakeConnection:
    def __init__(self, created):
        self.created = created

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement):
        return SimpleNamespace(scalar=lambda: self.created)


class LadderTests(unittest.TestCase):
    def service(self, *, now, created=1000.0, mode_fl=1_500_000, admin=3_000_000):
        repo = SimpleNamespace(
            engine=SimpleNamespace(connect=lambda: FakeConnection(created)),
            clock=lambda: now,
        )
        limits = {
            "adopted": True,
            "value": {
                "max_hourly_price_microusd": admin,
                "mode_hourly_ceiling_microusd": {
                    "fl": mode_fl,
                    "ref": 3_000_000,
                    "other": 850_000,
                },
            },
        }
        return SimpleNamespace(
            repo=repo,
            settings=SimpleNamespace(tenant_id="sixnine"),
            policy={"demand_start": {"profile_ids": ["p"]}},
            _admin_limits=lambda connection: limits,
        )

    def price(self, service, ceiling, base=1_000_000):
        read = lambda self_, connection, profile: (
            {"max_price_microusd": base, "price_ceiling_microusd": ceiling},
            None,
            None,
        )
        with patch("studio_platform.operator_prices.OperatorPrices.read", read):
            return demand.ladder_price(service, {"id": "p", "mode": "fl"}, "job-1")

    def test_short_wait_keeps_the_profile_price(self):
        self.assertIsNone(self.price(self.service(now=1000.0 + 179), 2_000_000))

    def test_each_three_minutes_adds_fifteen_percent(self):
        self.assertEqual(
            self.price(self.service(now=1000.0 + 180), 2_000_000), 1_150_000
        )
        self.assertEqual(
            self.price(self.service(now=1000.0 + 360), 2_000_000), 1_300_000
        )

    def test_never_above_the_lowest_ceiling(self):
        long_wait = 1000.0 + 3600
        self.assertEqual(self.price(self.service(now=long_wait), 2_000_000), 1_500_000)
        self.assertEqual(
            self.price(
                self.service(now=long_wait, mode_fl=3_000_000, admin=1_400_000),
                2_000_000,
            ),
            1_400_000,
        )
        self.assertEqual(
            self.price(self.service(now=long_wait, mode_fl=3_000_000), 1_200_000),
            1_200_000,
        )

    def test_ceiling_at_the_base_price_never_raises(self):
        self.assertIsNone(self.price(self.service(now=1000.0 + 3600), 1_000_000))

    def test_unknown_job_time_keeps_the_profile_price(self):
        self.assertIsNone(self.price(self.service(now=5000.0, created=None), 2_000_000))


if __name__ == "__main__":
    unittest.main()
