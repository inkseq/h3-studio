"""Frozen caps, atomic admin CAS and original intent replay; fake offers only."""

import copy

from sqlalchemy import func, select

from studio_platform.auth import Principal
from studio_platform.dstack_operator import DstackOperator, OperatorError
from studio_platform.operator_capacity import operator_policy, operator_previews
from studio_platform.repository import budget_accounts, outbox
import test_dstack_operator as fixtures
import test_platform_repository as ledger_fixtures


class DstackPriceTests(ledger_fixtures.LedgerCase):
    _setup_dstack = fixtures.DstackOperatorTests._setup_dstack
    counts = fixtures.DstackOperatorTests.counts
    start = fixtures.DstackOperatorTests.start
    preview = fixtures.DstackOperatorTests.preview

    def setUp(self):
        super().setUp()
        self._setup_dstack()
        self.settings.operator_admin_owners = ("superdan",)

    def price(self):
        return self.service.admin_prices(self.owner)["profiles"][0]

    def update(self, default, ceiling):
        return self.service.update_price(
            self.owner,
            "5090-fl",
            {
                "expected_snapshot": self.price()["snapshot"],
                "max_price_microusd": default,
                "price_ceiling_microusd": ceiling,
            },
        )

    def test_admin_price_change_is_audited_without_mutating_original_balances(self):
        before = self.repo.get_budget("owner-budget")
        old = self.price()
        result = self.update(700000, 800000)
        self.assertTrue(result["changed"])
        self.assertEqual(before, self.repo.get_budget("owner-budget"))
        self.assertEqual(self.service.profiles["5090-fl"]["max_price_microusd"], 750000)
        audit = self.service.admin_prices(self.owner)["audits"][0]
        self.assertEqual(
            (audit["old"]["max_price_microusd"], audit["new"]["max_price_microusd"]),
            (750000, 700000),
        )
        self.assertEqual(
            (audit["actor_owner"], audit["old_version"], audit["new_version"]),
            ("superdan", 0, 1),
        )
        with self.assertRaisesRegex(OperatorError, "price_policy_conflict"):
            self.service.update_price(
                self.owner,
                "5090-fl",
                {
                    "expected_snapshot": old["snapshot"],
                    "max_price_microusd": 700000,
                    "price_ceiling_microusd": 800000,
                },
            )
        self.assertFalse(self.update(700000, 800000)["changed"])
        self.assertEqual(len(self.service.admin_prices(self.owner)["audits"]), 1)

    def test_owner_and_machine_cannot_edit_or_read_admin_prices(self):
        for principal in (self.other_owner, Principal("superdan", "pat", machine=True)):
            with self.assertRaises(OperatorError):
                self.service.admin_prices(principal)
            with self.assertRaises(OperatorError):
                self.service.update_price(principal, "5090-fl", {})
        self.assertEqual(self.service.catalog(self.other_owner)["profiles"], [])

    def test_lowered_cap_freezes_plan_apply_reservation_and_original_replay(self):
        self.update(750000, 800000)
        preview = self.service.preview(
            self.owner,
            {"profile_id": "5090-fl", "ttl_seconds": 900, "max_price_microusd": 700000},
        )
        self.assertEqual(preview["hourly_cost_microusd"], 700000)
        self.assertEqual(preview["reservation_microusd"], 233334)
        result = self.start(preview=preview)
        run = next(iter(self.client.runs.values()))
        self.assertEqual(run["run_spec"]["configuration"]["max_price"], 0.7)
        before = self.repo.get_budget("owner-budget")
        self.update(650000, 650000)
        self.assertEqual(self.start(preview=preview), result)
        self.assertEqual(before, self.repo.get_budget("owner-budget"))
        self.assertEqual(len(self.client.applied), 1)

    def test_exceeding_ceiling_rejected_before_provider_plan_and_no_offer_below_cap(
        self,
    ):
        calls = []
        original = self.client.plan
        self.client.plan = lambda spec: calls.append(spec) or original(spec)
        for value in (750001, 0, True, 750000.0):
            with self.assertRaisesRegex(OperatorError, "price_ceiling_exceeded"):
                self.service.preview(
                    self.owner,
                    {
                        "profile_id": "5090-fl",
                        "ttl_seconds": 900,
                        "max_price_microusd": value,
                    },
                )
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(OperatorError, "no_compatible_offers"):
            self.service.preview(
                self.owner,
                {
                    "profile_id": "5090-fl",
                    "ttl_seconds": 900,
                    "max_price_microusd": 600000,
                },
            )
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_price_change_or_aba_invalidates_unconsumed_preview(self):
        preview = self.preview()
        self.update(700000, 750000)
        self.update(750000, 750000)
        with self.assertRaisesRegex(OperatorError, "price_policy_changed"):
            self.start(preview=preview)
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_changed_during_plan_does_not_store_usable_preview(self):
        original = self.client.plan

        def changing_plan(spec):
            result = original(spec)
            self.update(700000, 750000)
            return result

        self.client.plan = changing_plan
        with self.assertRaisesRegex(OperatorError, "price_policy_changed"):
            self.preview()
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(operator_previews)
                ).scalar_one(),
                0,
            )

    def test_stale_profile_overlay_fails_closed(self):
        self.update(700000, 750000)
        config = copy.deepcopy(self.config)
        config["profiles"][0]["cpu_count"] += 1
        service = DstackOperator(self.repo, self.settings, self.capacity, config)
        with self.assertRaisesRegex(OperatorError, "price_policy_binding_changed"):
            service.catalog(self.owner)

    def test_concurrent_first_admin_cas_has_one_winner_and_one_audit(self):
        snapshot = self.price()["snapshot"]

        def change(index):
            try:
                return self.service.update_price(
                    self.owner,
                    "5090-fl",
                    {
                        "expected_snapshot": snapshot,
                        "max_price_microusd": 700000 + index,
                        "price_ceiling_microusd": 800000,
                    },
                )
            except OperatorError as error:
                self.assertEqual(error.code, "dstack_price_policy_conflict")
                return None

        results = self.parallel(change)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.service.admin_prices(self.owner)["audits"]), 1)
