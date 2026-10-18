from dataclasses import replace
import unittest

from studio_platform.autoscale import Demand, ScalePolicy, ScaleState, Slot, may_drain, predict, recommend, runtime_estimate


class AutoscaleTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.demands = [Demand(str(i), "owner", 900, 180) for i in range(12)]
        self.slot = Slot("worker", "ready", 0)
        self.policy = ScalePolicy(max_instances=2, max_physical_gpus=2,
            cold_start_s=60, approved_remaining_microusd=2_000_000,
            instance_reservation_microusd=1_000_000, hard_deadline=5000)

    def check(self, **kwargs):
        arguments = dict(now=self.now, policy=self.policy, state=ScaleState(1))
        arguments.update(kwargs)
        return recommend(self.demands, [self.slot],
            [{"state": "ready", "physical_gpus": 1}], **arguments)

    def test_default_max_zero_never_proposes(self):
        result = self.check(policy=ScalePolicy())
        self.assertEqual(result.action, "none")
        self.assertEqual(result.reason, "capacity_limit")

    def test_nonfinite_policy_and_negative_budget_rejected(self):
        for policy in (replace(self.policy, cold_start_s=float("nan")),
                       replace(self.policy, hard_deadline=float("inf")),
                       replace(self.policy, approved_remaining_microusd=-1)):
            with self.assertRaises(ValueError):
                self.check(policy=policy)

    def test_two_observations_and_default_dry_run(self):
        one = self.check(state=ScaleState())
        self.assertEqual(one.reason, "observe_again")
        two = self.check(state=one.state)
        self.assertEqual(two.action, "dry_run")
        self.assertEqual(two.reason, "scale_one_instance")
        self.assertGreater(two.improvement_s, 120)

    def test_explicit_proposal_still_only_a_plan(self):
        self.assertEqual(self.check(policy=replace(self.policy, dry_run=False)).action, "propose")

    def test_starting_counts_future_capacity_and_unknown_counts_limits(self):
        prediction = predict([Demand("a", "u", 1000, 100)], [Slot("starting", "starting", 300)], now=1000)
        self.assertEqual(prediction["starts"]["a"], 300)
        result = recommend(self.demands, [self.slot], [
            {"state": "ready", "physical_gpus": 1}, {"state": "creation_unknown", "physical_gpus": 1}],
            now=self.now, policy=self.policy, state=ScaleState(1))
        self.assertEqual(result.reason, "capacity_limit")

    def test_cold_start_later_than_existing_completion_does_not_scale(self):
        result = self.check(policy=replace(self.policy, cold_start_s=3000))
        self.assertEqual(result.reason, "cold_start_no_benefit")

    def test_budget_deadline_and_cooldown_guards(self):
        self.assertEqual(self.check(policy=replace(self.policy, approved_remaining_microusd=None)).reason, "budget_not_approved")
        self.assertEqual(self.check(policy=replace(self.policy, approved_remaining_microusd=1)).reason, "budget_not_approved")
        self.assertEqual(self.check(policy=replace(self.policy, hard_deadline=1100)).reason, "deadline_not_safe")
        self.assertEqual(self.check(state=ScaleState(1, 990)).reason, "cooldown")

    def test_no_current_capacity_requires_cold_start_and_approved_policy(self):
        result = recommend(self.demands, [], [], now=self.now, policy=self.policy, state=ScaleState(1))
        self.assertEqual(result.action, "dry_run")
        self.assertIsNone(result.predicted_finish_s)

    def test_no_demand_resets_observation(self):
        result = recommend([], [], [], now=1000, state=ScaleState(4))
        self.assertEqual(result.reason, "capacity_sufficient")
        self.assertEqual(result.state.consecutive_breaches, 0)

    def test_runtime_sample_confidence_and_unknown_blocks_prediction(self):
        self.assertEqual(runtime_estimate([])["confidence"], "unknown")
        self.assertEqual(runtime_estimate([10, 20])["runtime_s"], 25)
        self.assertEqual(runtime_estimate(range(1, 11))["runtime_s"], 9)
        result = recommend([Demand("unknown", "u", 1000, 1, "unknown")], [self.slot], [],
            now=1000, policy=self.policy, state=ScaleState(1))
        self.assertEqual(result.reason, "runtime_unknown")

    def test_tp2_slot_is_one_execution_slot(self):
        prediction = predict(self.demands[:2], [Slot("tp2", "ready")], now=self.now)
        self.assertEqual(prediction["finish_s"], 360)

    def test_draining_requires_no_jobs_or_unresolved_collection(self):
        instance = {"state": "ready", "idle_since": 0}
        self.assertTrue(may_drain(instance, now=1000, active_job_count=0, unresolved_attempt_count=0, collection_count=0))
        self.assertFalse(may_drain(instance, now=1000, active_job_count=1, unresolved_attempt_count=0, collection_count=0))
        self.assertFalse(may_drain(instance, now=1000, active_job_count=0, unresolved_attempt_count=1, collection_count=0))
        self.assertFalse(may_drain(instance, now=1000, active_job_count=0, unresolved_attempt_count=0, collection_count=1))
