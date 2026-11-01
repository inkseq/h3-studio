"""Per-lane start backoff (#931 step 2); pure, no provider or database."""

import copy
import unittest

from studio_platform.capacity_health import start_loop
from studio_platform.scheduling.health import failed_starts, lane_backoff


def node(identity, backend, started, stopped, ready=None):
    binding = {
        "backend": backend,
        "apply_started": True,
        "apply_started_at": started,
        "first_stopped_at": stopped,
    }
    if ready is not None:
        binding["first_runtime_ready_at"] = ready
    return {
        "intent_id": identity * 32,
        "provider": "modal-dialout",
        "provider_instance_id": "sb-" + identity,
        "desired_state": "stopped",
        "runtime_state": "stopped",
        "payload": {"dstack": binding},
    }


class LaneBackoffTests(unittest.TestCase):
    def test_backoff_doubles_per_lane_caps_and_resets(self):
        self.assertEqual(
            lane_backoff([(1000, "a", "modal-dialout")], 1100), {"modal-dialout": 1300}
        )
        failures = [(1000, "a", "modal-dialout"), (2000, "b", "modal-dialout")]
        self.assertEqual(lane_backoff(failures, 2100), {"modal-dialout": 2600})
        many = [(t, str(t), "vastai") for t in range(1000, 8000, 1000)]
        self.assertEqual(lane_backoff(many, 7100), {"vastai": 8800})  # 30 min cap.
        self.assertEqual(lane_backoff(failures, 2601), {})
        # After 3 h without a failure the count starts again at 5 min.
        later = failures + [(2000 + 10801, "c", "modal-dialout")]
        self.assertEqual(lane_backoff(later, 12802), {"modal-dialout": 13101})
        # Lanes are independent.
        mixed = [(1000, "a", "vastai"), (1000, "b", "lium"), (1500, "c", "lium")]
        self.assertEqual(lane_backoff(mixed, 1600), {"lium": 2100})

    def test_only_starts_that_never_became_ready_and_never_served_count(self):
        broken = node("a", "modal-dialout", 100, 1300)
        ready = node("b", "vastai", 100, 1300, ready=400)
        served = node("c", "lium", 100, 1300)
        running = copy.deepcopy(broken)
        running.update(intent_id="d" * 32, runtime_state="ready")
        attempt = {"worker_id": "dstack-" + "c" * 32, "submission_started_at": 500}
        self.assertEqual(
            failed_starts([broken, ready, served, running], [attempt], [], 2000),
            [(1300, "a" * 32, "modal-dialout")],
        )

    def test_one_failing_lane_backs_off_alone_and_cross_lane_loops_still_pause(self):
        one = [(200, "a", "modal-dialout"), (400, "b", "modal-dialout")]
        self.assertFalse(start_loop(one))
        self.assertTrue(start_loop([(200, "a", "modal-dialout"), (400, "b", "vastai")]))
        self.assertTrue(start_loop([(200, "a", None), (400, "b", "vastai")]))
        self.assertTrue(start_loop([(t, str(t), "vastai") for t in range(4)]))
        self.assertFalse(start_loop([(200, "a", None)]))


if __name__ == "__main__":
    unittest.main()
