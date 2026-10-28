"""Decision boundaries for future capacity integration, without cloud calls."""

import unittest
from dataclasses import asdict

from studio_platform.capacity_lookahead import Decision, Machine, Policy, plan


class LookaheadTests(unittest.TestCase):
    def decide(self, queued, machines=(), **kwargs):
        options = dict(available_slots=10, admission_blocked=False)
        options.update(kwargs)
        return plan(Policy(), machines, queued=queued, **options)

    def test_floor_without_threshold_and_without_forecast_rentals(self):
        self.assertEqual(self.decide(1), Decision(1, 1, (), "floor"))
        self.assertEqual(self.decide(0, expected_arrivals=200).start_count, 0)
        self.assertEqual(self.decide(0, remaining_job_seconds=300).start_count, 0)

    def test_owner_ready_examples_and_threshold(self):
        ready = (Machine("ready", "ready"),)
        for queued, left, count in [(30, 10, 0), (39, 19, 0), (40, 20, 1), (45, 25, 1)]:
            with self.subTest(queued=queued):
                decision = self.decide(queued, ready)
                self.assertEqual(
                    (decision.left_at_ready, decision.start_count), (left, count)
                )

    def test_starting_machine_counts_partial_throughput(self):
        for eta, left in [(0, 5), (300, 15), (600, 25), (900, 25), (None, 25)]:
            with self.subTest(eta=eta):
                decision = self.decide(25, (Machine("boot", "starting", eta),))
                self.assertEqual(decision.left_at_ready, left)
        self.assertEqual(self.decide(1, (Machine("boot", "starting"),)).start_count, 0)

    def test_busy_remaining_work_is_fractional_job_equivalent(self):
        decision = self.decide(
            39, (Machine("busy", "ready"),), remaining_job_seconds=30
        )
        self.assertEqual(decision.start_count, 1)
        self.assertEqual(decision.left_at_ready, 20)
        self.assertEqual(self.decide(1, remaining_job_seconds=15).left_at_ready, 1.5)

    def test_forecast_adds_to_existing_demand_only(self):
        self.assertEqual(
            self.decide(30, (Machine("r", "ready"),), expected_arrivals=10).start_count,
            1,
        )

    def test_all_hard_gates_win_over_floor_and_backlog(self):
        for queued in (1, 1000):
            self.assertEqual(self.decide(queued, available_slots=0).start_count, 0)
            self.assertEqual(self.decide(queued, admission_blocked=True).start_count, 0)
        self.assertEqual(self.decide(1000, available_slots=2).start_count, 2)
        self.assertEqual(self.decide(1000).start_count, 10)
        machines = tuple(Machine(str(i), "starting") for i in range(10))
        self.assertEqual(self.decide(1000, machines).start_count, 0)

    def test_idle_boundary_requires_zero_work(self):
        machines = (
            Machine("a", "ready", idle_seconds=599.9),
            Machine("b", "ready", idle_seconds=600),
            Machine("c", "ready"),
            Machine("d", "starting", 0),
        )
        self.assertEqual(self.decide(0, machines).idle_candidates, ("b",))
        self.assertEqual(self.decide(1, machines).idle_candidates, ())
        self.assertEqual(
            self.decide(0, machines, remaining_job_seconds=1).idle_candidates, ()
        )
        # Admission-off never weakens the independent idle-stop guarantee.
        self.assertEqual(
            self.decide(0, machines, admission_blocked=True).idle_candidates, ("b",)
        )

    def test_settings_change_both_horizon_and_idle_interval(self):
        decision = plan(
            Policy(job_seconds=60, boot_seconds=120, minimum_jobs=3),
            (Machine("r", "ready", idle_seconds=120),),
            queued=5,
            available_slots=2,
            admission_blocked=False,
        )
        self.assertEqual(decision, Decision(3, 1, (), "lookahead"))

    def test_duplicate_machine_never_double_counts(self):
        with self.assertRaisesRegex(ValueError, "duplicate_machine"):
            self.decide(1, (Machine("same", "ready"), Machine("same", "starting")))

    def test_invalid_snapshots_fail_without_decision(self):
        for value in (True, -1, float("nan"), float("inf"), "1"):
            for field in (
                "queued",
                "remaining_job_seconds",
                "available_slots",
                "expected_arrivals",
            ):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    options = dict(queued=1, available_slots=1, admission_blocked=False)
                    options[field] = value
                    plan(Policy(), (), **options)
        with self.assertRaises(ValueError):
            self.decide(1, admission_blocked=0)

    def test_invalid_policy_and_machine_state(self):
        for options in (
            dict(job_seconds=0),
            dict(boot_seconds=0),
            dict(minimum_jobs=0),
            dict(max_machines=-1),
            dict(minimum_jobs=1.5),
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Policy(**options)
        for args in (
            ("x", "unknown"),
            ("", "ready"),
            ("x", "ready", 10),
            ("x", "starting", 10, 0),
            ("x", "starting", float("nan")),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                Machine(*args)

    def test_order_independent_immutable_and_serializable(self):
        machines = (
            Machine("b", "ready", idle_seconds=600),
            Machine("a", "ready", idle_seconds=700),
        )
        before = tuple(asdict(machine) for machine in machines)
        decision = self.decide(0, machines)
        self.assertEqual(decision, self.decide(0, machines[::-1]))
        self.assertEqual(tuple(asdict(machine) for machine in machines), before)
        self.assertEqual(asdict(decision)["idle_candidates"], ("a", "b"))


if __name__ == "__main__":
    unittest.main()
