"""Disposable budget ledger tests; no provider calls or customer credits."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from studio_platform.inky_budget import InkyBudget
from studio_platform.repository import Conflict
from test_platform_repository import LedgerCase


class InkyBudgetTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc).timestamp()
        self.budget = InkyBudget(self.repo)

    def reserve(self, ident="one", **changes):
        return self.budget.reserve(
            **{
                "tenant": "tenant",
                "owner": "owner",
                "project": "project",
                "source_id": ident,
                "max_cost_micros": 1000,
                **changes,
            }
        )

    def test_concurrent_account_limit_and_replay_do_not_authorize_more_calls(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda i: self.reserve(str(i)), range(40)))
        self.assertEqual(sum(v["allowed"] for v in values), 30)
        winner = next(str(i) for i, v in enumerate(values) if v["allowed"])
        self.assertEqual(self.reserve(winner)["reason"], "inky_example_replay")
        self.assertEqual(self.reserve("another")["reason"], "inky_example_budget")
        self.assertTrue(self.reserve("other-owner", owner="other")["allowed"])

    def test_global_monthly_cap_counts_unknown_reservations_across_accounts(self):
        self.assertTrue(self.reserve("a", max_cost_micros=19_000_000)["allowed"])
        self.assertTrue(
            self.reserve("b", owner="other", max_cost_micros=1_000_000)["allowed"]
        )
        self.assertEqual(
            self.reserve("c", tenant="other", owner="third")["reason"],
            "inky_example_budget",
        )
        self.now += 86400
        self.assertEqual(self.reserve("tomorrow")["reason"], "inky_example_budget")

    def test_settlement_releases_only_known_unused_ceiling_and_retains_daily_count(
        self,
    ):
        original = self.reserve("a", max_cost_micros=20_000_000)
        self.assertFalse(self.reserve("b")["allowed"])
        receipt = self.budget.settle(original["call_id"], 600)
        self.assertEqual(receipt["actual_micros"], 600)
        self.assertEqual(self.budget.settle(original["call_id"], 600), receipt)
        with self.assertRaisesRegex(Conflict, "inky_usage_conflict"):
            self.budget.settle(original["call_id"], 0)
        self.assertTrue(self.reserve("b")["allowed"])
        self.assertFalse(self.reserve("a")["allowed"])
        for i in range(28):
            self.assertTrue(self.reserve(f"rest-{i}")["allowed"])
        self.assertEqual(self.reserve("over")["reason"], "inky_example_budget")

    def test_unexpected_cost_is_preserved_and_freezes_further_monthly_calls(self):
        original = self.reserve()
        receipt = self.budget.settle(original["call_id"], 1001)
        self.assertTrue(receipt["overrun"])
        self.assertFalse(self.reserve("other", owner="different")["allowed"])

    def test_utc_rollover_does_not_replay_old_source(self):
        original = self.reserve(max_cost_micros=20_000_000)
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
        self.assertTrue(self.reserve("new-month")["allowed"])
        self.assertEqual(self.reserve()["reason"], "inky_example_replay")
        self.assertFalse(self.budget.settle(original["call_id"], 600)["overrun"])

    def test_invalid_ceiling_cannot_disable_cap(self):
        for ceiling in (True, 0, -1, 20_000_001, float("nan"), "1000"):
            with (
                self.subTest(ceiling=ceiling),
                self.assertRaisesRegex(ValueError, "inky_budget_ceiling_invalid"),
            ):
                self.reserve(max_cost_micros=ceiling)
