"""Money/wait simulation boundaries; no provider or credentials are loaded."""

from dataclasses import replace
import unittest

from studio_platform.capacity_wait_plan import (
    ModalOption,
    Shot,
    Slot,
    Targets,
    fair_order,
    plan,
)


class CapacityWaitPlanTests(unittest.TestCase):
    def setUp(self):
        self.targets = Targets(300, 120, 180, 20_000_000, 10)
        self.option = ModalOption("fl", 120, 4_000_000, "fake-qualified-receipt")

    def shot(self, key, owner=None, seconds=40, age=0, group="fl"):
        return Shot(
            key, owner or key, group, age, (("modal", seconds), ("dedicated", seconds))
        )

    def decide(self, shots, slots=(), **kwargs):
        return plan(
            shots, slots, self.targets, self.option, admission_blocked=False, **kwargs
        )

    def test_no_demand_has_no_floor_or_creation(self):
        result = self.decide([])
        self.assertEqual(result["additional_modal"], 0)
        self.assertEqual(result["reason"], "no_demand")
        self.assertTrue(result["advisory_only"])

    def test_first_pending_before_later_and_fifo_within_owner(self):
        shots = [
            self.shot("a2", "a", age=80),
            self.shot("a1", "a", age=100),
            self.shot("b1", "b", age=10),
        ]
        self.assertEqual([s.id for s in fair_order(shots, ())], ["a1", "b1", "a2"])

    def test_starting_host_covers_multiple_inputs_without_an_extra_create(self):
        slots = [Slot("boot", "modal", ("fl",), "starting", 4_000_000, 60)]
        result = self.decide([self.shot("a"), self.shot("b")], slots)
        self.assertEqual(result["additional_modal"], 0)
        self.assertEqual(
            [r["first_video_eta_seconds"] for r in result["before"]], [100, 140]
        )

    def test_ready_dedicated_wins_tie_with_ready_modal(self):
        slots = [
            Slot("m", "modal", ("fl",), "ready", 4_000_000, 0),
            Slot("v", "dedicated", ("fl",), "ready", 850_000, 0),
        ]
        shot = replace(self.shot("a"), durations=(("modal", 10), ("dedicated", 35)))
        self.assertEqual(
            self.decide([shot], slots)["before"][0]["first_video_eta_seconds"], 35
        )

    def test_burst_uses_smallest_target_count_bounded_by_full_hourly_ceiling(self):
        result = self.decide([self.shot(str(i)) for i in range(4)])
        # 120s start + one40s input per slot beats the180s trigger; all four
        # first completions beat300s. No fixed two-container product cap.
        self.assertEqual(result["additional_modal"], 4)
        self.assertEqual(result["proposed_hourly_microusd"], 16_000_000)
        self.assertEqual(result["reason"], "targets_projected")

    def test_cap_bind_reports_real_projection_without_claiming_sla(self):
        result = self.decide([self.shot(str(i)) for i in range(30)])
        self.assertEqual(result["additional_modal"], 5)
        self.assertEqual(result["reason"], "capacity_limit")
        self.assertEqual(
            max(r["first_video_eta_seconds"] for r in result["projected"]), 360
        )
        self.assertTrue(any(not r["first_target_met"] for r in result["projected"]))

    def test_queue_age_is_included_even_when_eta_from_now_looks_fast(self):
        slots = [Slot("v", "dedicated", ("fl",), "ready", 850_000, 0)]
        result = self.decide([self.shot("a", age=290)], slots)
        self.assertEqual(result["before"][0]["first_video_wait_seconds"], 330)
        self.assertFalse(result["before"][0]["first_target_met"])
        # A slower extra cold host cannot reverse time or improve this shot.
        self.assertEqual(result["additional_modal"], 0)

    def test_parallel_shots_reduce_completion_interval_for_existing_owner(self):
        self.option = replace(self.option, boot_seconds=0)
        shots = [self.shot(str(i), "a", seconds=200) for i in range(4)]
        slots = [Slot("v", "dedicated", ("fl",), "running", 850_000, 100, "a")]
        result = self.decide(shots, slots, last_delivery={"a": 20})
        self.assertGreater(result["additional_modal"], 0)
        row = result["projected"][0]
        self.assertIsNone(row["first_video_wait_seconds"])
        self.assertLessEqual(row["maximum_video_interval_seconds"], 120)

    def test_cold_start_cannot_repair_unavoidable_gap_or_justify_useless_starts(self):
        shots = [self.shot(str(i), "a", seconds=200) for i in range(4)]
        slots = [Slot("v", "dedicated", ("fl",), "running", 850_000, 10, "a")]
        result = self.decide(shots, slots, last_delivery={"a": 20})
        self.assertEqual(result["additional_modal"], 0)
        self.assertEqual(result["reason"], "target_unreachable")
        self.assertFalse(result["projected"][0]["interval_target_met"])

    def test_heavy_reference_serial_limit_cannot_be_bypassed_by_projection(self):
        self.option = replace(self.option, boot_seconds=0)
        shots = [
            replace(
                self.shot(str(i), "a", seconds=230), serial_key="tenant:a:sharp-ref"
            )
            for i in range(3)
        ]
        slots = [
            Slot(
                "v",
                "dedicated",
                ("fl",),
                "running",
                850_000,
                100,
                "a",
                "tenant:a:sharp-ref",
            )
        ]
        result = self.decide(shots, slots, last_delivery={"a": 20})
        self.assertEqual(result["additional_modal"], 0)
        self.assertEqual(result["before"][0]["maximum_video_interval_seconds"], 230)
        self.assertFalse(result["projected"][0]["interval_target_met"])

    def test_current_original_delivery_included_once_and_episode_age_required(self):
        slots = [Slot("v", "dedicated", ("fl",), "running", 850_000, 100, "a")]
        shots = [self.shot("next", "a", age=10)]
        with self.assertRaisesRegex(ValueError, "episode_required"):
            self.decide(shots, slots)
        result = self.decide(shots, slots, episode_seconds={"a": 250})
        row = result["before"][0]
        self.assertEqual(row["first_video_wait_seconds"], 350)
        self.assertEqual(row["maximum_video_interval_seconds"], 40)

    def test_unknown_and_missing_start_time_preserve_occupancy_and_refuse(self):
        for state, price, ready in [
            ("unknown", 4_000_000, None),
            ("starting", 4_000_000, None),
            ("stopping", None, None),
        ]:
            with self.subTest(state=state):
                slot = Slot("old", "modal", ("fl",), state, price, ready)
                result = self.decide([self.shot("a")], [slot])
                self.assertEqual(result["additional_modal"], 0)
                self.assertEqual(result["occupied_machines"], 1)
                self.assertEqual(result["committed_hourly_microusd"], price)
                self.assertEqual(result["reason"], "capacity_unconfirmed")

    def test_stopping_and_incompatible_resources_still_consume_global_budget(self):
        slots = [
            Slot("stop", "modal", ("ref",), "stopping", 4_000_000, None),
            Slot("other", "dedicated", ("other",), "ready", 12_000_000, 0),
        ]
        result = self.decide([self.shot(str(i)) for i in range(8)], slots)
        self.assertEqual(result["additional_modal"], 1)
        self.assertEqual(result["committed_hourly_microusd"], 16_000_000)
        self.assertEqual(result["reason"], "capacity_limit")

    def test_machine_and_hourly_caps_independently_bind_without_raising_them(self):
        shots = [self.shot(str(i)) for i in range(10)]
        for target, count in [
            (replace(self.targets, machine_limit=2), 2),
            (replace(self.targets, hourly_limit_microusd=7_999_999), 1),
        ]:
            result = plan(shots, (), target, self.option, admission_blocked=False)
            self.assertEqual(result["additional_modal"], count)

    def test_unmeasured_option_missing_durations_and_incompatible_jobs_fail_closed(
        self,
    ):
        result = plan(
            [self.shot("a")],
            (),
            self.targets,
            replace(self.option, qualification=None),
            admission_blocked=False,
        )
        self.assertEqual(result["reason"], "timing_unqualified")
        self.assertEqual(result["additional_modal"], 0)
        for shot in [
            replace(self.shot("a"), durations=()),
            self.shot("b", group="ref"),
        ]:
            result = self.decide([shot])
            self.assertEqual(result["additional_modal"], 0)
            self.assertEqual(result["projected"][0]["unplaced_shots"], 1)
            self.assertFalse(result["projected"][0]["first_target_met"])

    def test_original_admission_gate_defaults_closed(self):
        result = plan([self.shot("a")], (), self.targets, self.option)
        self.assertEqual(result["reason"], "admission_blocked")
        self.assertEqual(result["additional_modal"], 0)

    def test_modal_proposal_requires_the_existing_full_resource_price_floor(self):
        for price in (1, 3_503_339, 4_000_001):
            with self.subTest(price=price), self.assertRaises(ValueError):
                replace(self.option, hourly_microusd=price)

    def test_invalid_or_duplicate_inputs_are_not_partial_plans(self):
        for value in (True, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.shot("a", seconds=value)
        with self.assertRaises(ValueError):
            self.decide([self.shot("a"), self.shot("a")])
        with self.assertRaises(ValueError):
            replace(self.targets, hourly_limit_microusd=20_000_001)
        with self.assertRaises(ValueError):
            self.decide([self.shot("a")], last_delivery={"a": -1})

    def test_1000_shots_are_fair_and_financially_bounded_without_providers(self):
        shots = tuple(
            self.shot(f"{owner:03}-{depth:02}", f"owner-{owner:03}", age=10 - depth)
            for depth in range(10)
            for owner in range(100)
        )
        original = tuple(shots)
        result = self.decide(shots)
        self.assertEqual(shots, original)
        self.assertEqual(result["additional_modal"], 5)
        self.assertEqual(len(result["projected"]), 100)
        self.assertEqual(sum(r["unplaced_shots"] for r in result["projected"]), 0)
        self.assertTrue(all(not r["interval_target_met"] for r in result["projected"]))
        self.assertEqual(result["proposed_hourly_microusd"], 20_000_000)


if __name__ == "__main__":
    unittest.main()
