"""Frozen caps, atomic admin CAS and original intent replay; fake offers only."""

import copy
from unittest.mock import patch

from sqlalchemy import func, select

from studio_platform.auth import Principal
from studio_platform.dstack_operator import DstackOperator, OperatorError
from studio_platform.operator_capacity import operator_policy, operator_previews
from studio_platform.operator_prices import canonical_mode_price_ceiling
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

    def continuing(self, modes=("fl",), *, fleet=1_700_000):
        config = copy.deepcopy(self.config)
        config["policy"].update(
            lifecycle_version=2,
            reservation_seconds=3600,
            expires_at=None,
            max_hourly_cost_microusd=fleet,
            demand_start={"profile_ids": ["5090-" + mode for mode in modes]},
        )
        template = config["profiles"][0]
        config["profiles"] = [
            {
                **copy.deepcopy(template),
                "id": "5090-" + mode,
                "mode": mode,
                "max_price_microusd": canonical_mode_price_ceiling(mode),
            }
            for mode in modes
        ]
        self.service = DstackOperator(self.repo, self.settings, self.capacity, config)
        policies = {}
        for profile in self.service.profiles.values():
            policies[profile["recipe_id"]] = {
                "enabled": True,
                "backend": "wangp-worker",
                "dispatch_backend": "hatchet-v1",
                "pool": profile["pool"],
                "configuration_id": profile["configuration_id"],
                "model_id": profile["model_id"],
                "engine_manifest_digest": profile["manifest_digest"],
                "qualification": {
                    "status": "accepted",
                    "verified_at": 1.0,
                    "expires_at": None,
                },
                "reservation": {"expected_runtime_s": 1800, "expires_at": None},
                "envelope": {"max_duration_seconds": 4.0},
            }
        return policies

    def test_optional_reason_uses_same_audit_and_preserves_legacy_rows(self):
        self.update(700000, 800000)
        old_audit = self.service.admin_prices(self.owner)["audits"][0]
        self.assertNotIn("reason", old_audit)
        before = self.repo.get_budget("owner-budget")
        snapshot = self.price()["snapshot"]
        reason = "#193 owner decision6335811113"
        result = self.service.update_price(
            self.owner,
            "5090-fl",
            {
                "expected_snapshot": snapshot,
                "max_price_microusd": 710000,
                "price_ceiling_microusd": 800000,
                "reason": reason,
            },
        )
        audits = self.service.admin_prices(self.owner)["audits"]
        self.assertEqual(len(audits), 2)
        self.assertEqual(
            next(a for a in audits if a["id"] == result["audit_event_id"])["reason"],
            reason,
        )
        self.assertEqual(
            next(a for a in audits if a["id"] == old_audit["id"]), old_audit
        )
        self.assertEqual(before, self.repo.get_budget("owner-budget"))
        with self.assertRaisesRegex(OperatorError, "price_policy_conflict"):
            self.service.update_price(
                self.owner,
                "5090-fl",
                {
                    "expected_snapshot": snapshot,
                    "max_price_microusd": 720000,
                    "price_ceiling_microusd": 800000,
                    "reason": "stale",
                },
            )
        self.assertEqual(len(self.service.admin_prices(self.owner)["audits"]), 2)

    def test_invalid_reason_never_changes_price_or_audit(self):
        old = self.price()
        for reason in (None, True, 1, "", "  ", "x" * 501):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(OperatorError, "price_policy_invalid"),
            ):
                self.service.update_price(
                    self.owner,
                    "5090-fl",
                    {
                        "expected_snapshot": old["snapshot"],
                        "max_price_microusd": 700000,
                        "price_ceiling_microusd": 800000,
                        "reason": reason,
                    },
                )
        self.assertEqual(self.price(), old)
        self.assertEqual(self.service.admin_prices(self.owner)["audits"], [])
        self.assertTrue(
            self.service.update_price(
                self.owner,
                "5090-fl",
                {
                    "expected_snapshot": old["snapshot"],
                    "max_price_microusd": 700000,
                    "price_ceiling_microusd": 800000,
                    "reason": "x" * 500,
                },
            )["changed"]
        )

    def test_mode_ceiling_is_shared_and_unknown_modes_never_gain_ref_limit(self):
        from studio_platform.dstack_demand import demand_configuration

        self.continuing(("fl", "ref"))
        before = copy.deepcopy(self.service.config)
        self.assertIsNotNone(demand_configuration(before, now=self.now))
        self.assertEqual(self.service.config, before)
        for mode in ("fl", "REF", "ref ", "ref2va", "both", "unknown", "", None, True):
            self.assertEqual(canonical_mode_price_ceiling(mode), 850000)
        self.assertEqual(canonical_mode_price_ceiling("ref"), 1500000)
        for mode, cap in (("fl", 850001), ("ref", 1500001), ("unknown", 850000)):
            config = copy.deepcopy(before)
            config["profiles"] = [
                {**config["profiles"][0], "mode": mode, "max_price_microusd": cap}
            ]
            config["policy"]["demand_start"]["profile_ids"] = [
                config["profiles"][0]["id"]
            ]
            self.assertIsNone(demand_configuration(config, now=self.now))

    def test_ref_manual_preview_and_existing_fleet_guard_preserve_first_machine(self):
        policies = self.continuing(("fl", "ref"))
        with patch(
            "studio_platform.execution_profiles.selected_policy",
            side_effect=lambda settings, *, profile_id, recipe_id: policies[recipe_id],
        ):
            first = self.service.preview(self.owner, {"profile_id": "5090-fl"})
            self.start(preview=first)
            before = self.repo.get_budget("owner-budget")
            counts = self.counts()
            second = self.service.preview(self.owner, {"profile_id": "5090-ref"})
            self.assertEqual(second["max_price_microusd"], 1500000)
            with self.assertRaisesRegex(OperatorError, "dstack_hourly_limit_exceeded"):
                self.start(key="second-ref", preview=second)
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.client.stopped, [])

    def test_ref_price_still_obeys_profile_admin_and_owner_caps(self):
        policies = self.continuing(("ref",))
        with patch(
            "studio_platform.execution_profiles.selected_policy",
            return_value=next(iter(policies.values())),
        ):
            for cap in (1_000_000, 1_500_000):
                self.service.update_price(
                    self.owner,
                    "5090-ref",
                    {
                        "expected_snapshot": self.price()["snapshot"],
                        "max_price_microusd": cap,
                        "price_ceiling_microusd": cap,
                    },
                )
                if cap == 1_000_000:
                    with self.assertRaisesRegex(
                        OperatorError, "price_ceiling_exceeded"
                    ):
                        self.service.preview(
                            self.owner,
                            {"profile_id": "5090-ref", "max_price_microusd": 1_200_000},
                        )
            with self.assertRaisesRegex(OperatorError, "price_ceiling_exceeded"):
                self.service.preview(
                    self.owner,
                    {"profile_id": "5090-ref", "max_price_microusd": 1500001},
                )
            original = self.service._admin_limits
            limits = original()
            limits["value"]["max_hourly_price_microusd"] = 1_000_000
            with (
                patch.object(self.service, "_admin_limits", return_value=limits),
                self.assertRaisesRegex(
                    OperatorError, "admin_hourly_price_limit_reached"
                ),
            ):
                self.service.preview(self.owner, {"profile_id": "5090-ref"})
            preview = self.service.preview(self.owner, {"profile_id": "5090-ref"})
            self.repo.configure_budget(
                "owner-budget",
                tenant_id=self.scope.tenant_id,
                owner_id=self.scope.owner_id,
                limit_microusd=1,
            )
            with self.assertRaises(fixtures.BudgetExceeded):
                self.start(preview=preview)
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_legacy_profile_without_demand_keeps_original_price_semantics(self):
        config = copy.deepcopy(self.config)
        config["profiles"][0]["max_price_microusd"] = 2_000_000
        self.service = DstackOperator(self.repo, self.settings, self.capacity, config)
        preview = self.preview()
        self.assertEqual(preview["max_price_microusd"], 2_000_000)
        self.assertEqual(self.client.applied, [])

    def test_legacy_catalog_matches_admission_without_reading_execution_policy(self):
        with patch(
            "studio_platform.execution_profiles.selected_policy",
            side_effect=AssertionError("legacy policy read forbidden"),
        ):
            self.assertEqual(
                self.service.catalog(self.owner)["profiles"][0][
                    "execution_eligibility"
                ],
                {"status": "confirmed", "reason_code": None, "observed_at": self.now},
            )
            result = self.start(preview=self.preview())
        self.assertEqual(self.client.applied, [result["node_id"]])
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_catalog_retains_confirmed_and_expired_choices_without_renewal(self):
        policies = self.continuing(("fl", "ref"))
        profiles = self.service.profiles
        current = policies[profiles["5090-fl"]["recipe_id"]]
        current["qualification"]["status"] = "runtime_required"
        expired = policies[profiles["5090-ref"]["recipe_id"]]
        expired["qualification"]["expires_at"] = self.now - 1
        before = copy.deepcopy(policies)
        with (
            patch(
                "studio_platform.execution_profiles.selected_policy",
                side_effect=lambda settings, *, profile_id, recipe_id: policies[
                    recipe_id
                ],
            ),
            patch.object(
                self.client, "plan", side_effect=AssertionError("plan forbidden")
            ),
        ):
            items = {
                item["id"]: item
                for item in self.service.catalog(self.owner)["profiles"]
            }
            self.assertEqual(set(items), {"5090-fl", "5090-ref"})
            self.assertEqual(
                items["5090-fl"]["execution_eligibility"]["status"], "confirmed"
            )
            self.assertEqual(
                items["5090-ref"]["execution_eligibility"]["reason_code"],
                "execution_policy_expired",
            )
            self.assertEqual(self.service.catalog(self.other_owner)["profiles"], [])
        self.assertEqual(policies, before)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_catalog_does_not_convert_unexpected_errors_to_policy_status(self):
        self.continuing()
        with (
            patch(
                "studio_platform.operator_prices.OperatorPrices.read",
                side_effect=RuntimeError("database unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            self.service.catalog(self.owner)
        with (
            patch(
                "studio_platform.execution_profiles.selected_policy",
                side_effect=RuntimeError("unexpected"),
            ),
            self.assertRaisesRegex(RuntimeError, "unexpected"),
        ):
            self.service.catalog(self.owner)

    def test_fl_cannot_borrow_ref_limit_even_with_higher_audited_ceiling(self):
        self.continuing()
        self.update(850000, 1500000)
        with (
            patch.object(
                self.client, "plan", side_effect=AssertionError("plan forbidden")
            ),
            self.assertRaisesRegex(OperatorError, "price_ceiling_exceeded"),
        ):
            self.service.preview(
                self.owner, {"profile_id": "5090-fl", "max_price_microusd": 1500000}
            )
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_catalog_eligibility_and_admission_share_policy_checks_without_writes(self):
        policies = self.continuing()
        profile = self.service.profiles["5090-fl"]
        policy = next(iter(policies.values()))
        cases = [
            ("execution_policy_missing", None),
            ("execution_policy_invalid", {}),
            ("execution_policy_disabled", {**policy, "enabled": False}),
            ("execution_policy_binding_mismatch", {**policy, "pool": "wrong"}),
            (
                "execution_policy_unqualified",
                {
                    **policy,
                    "qualification": {**policy["qualification"], "status": "rejected"},
                },
            ),
            (
                "execution_policy_expired",
                {
                    **policy,
                    "reservation": {**policy["reservation"], "expires_at": self.now},
                },
            ),
            (
                "execution_policy_not_yet_valid",
                {
                    **policy,
                    "qualification": {
                        **policy["qualification"],
                        "verified_at": self.now + 1,
                    },
                },
            ),
        ]
        config_before = copy.deepcopy(self.service.config)
        with patch.object(
            self.client, "plan", side_effect=AssertionError("plan forbidden")
        ):
            for reason, selected in cases:
                with (
                    self.subTest(reason=reason),
                    patch(
                        "studio_platform.execution_profiles.selected_policy",
                        return_value=selected,
                    ),
                ):
                    item = self.service.catalog(self.owner)["profiles"][0]
                    self.assertEqual(item["id"], "5090-fl")
                    self.assertEqual(
                        item["configuration_id"], profile["configuration_id"]
                    )
                    self.assertEqual(item["recipe_id"], profile["recipe_id"])
                    self.assertEqual(
                        item["execution_eligibility"],
                        {
                            "status": "blocked",
                            "reason_code": reason,
                            "observed_at": self.now,
                        },
                    )
                    with self.assertRaisesRegex(
                        OperatorError, "execution_policy_unconfirmed"
                    ):
                        self.service.preview(self.owner, {"profile_id": "5090-fl"})
        self.assertEqual(self.service.config, config_before)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(operator_previews)),
                0,
            )

    def test_catalog_confirmation_is_advisory_and_fresh_expiry_still_blocks_start(self):
        policies = self.continuing()
        policy = next(iter(policies.values()))
        with patch(
            "studio_platform.execution_profiles.selected_policy", return_value=policy
        ):
            self.assertEqual(
                self.service.catalog(self.owner)["profiles"][0][
                    "execution_eligibility"
                ],
                {"status": "confirmed", "reason_code": None, "observed_at": self.now},
            )
            preview = self.service.preview(self.owner, {"profile_id": "5090-fl"})
            policy["reservation"]["expires_at"] = self.now
            with self.assertRaisesRegex(OperatorError, "execution_policy_unconfirmed"):
                self.start(preview=preview)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.client.applied, [])

    def test_invalid_runtime_is_blocked_in_catalog_and_admission(self):
        policies = self.continuing()
        with (
            patch(
                "studio_platform.execution_profiles.selected_policy",
                return_value=next(iter(policies.values())),
            ),
            patch(
                "studio_platform.execution_policy.reservation_for_duration",
                return_value={"expected_runtime_s": float("nan")},
            ),
        ):
            self.assertEqual(
                self.service.catalog(self.owner)["profiles"][0][
                    "execution_eligibility"
                ]["reason_code"],
                "execution_runtime_unconfirmed",
            )
            with self.assertRaisesRegex(OperatorError, "execution_runtime_unconfirmed"):
                self.service.preview(self.owner, {"profile_id": "5090-fl"})
        self.assertEqual(self.counts(), (0, 0, 0, 0))

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
