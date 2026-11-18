import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from studio_platform import controller_finished as finished


class _Connection:
    def execute(self, _statement):
        return SimpleNamespace(mappings=lambda: [])


class FinishedRevisitTests(unittest.TestCase):
    def test_due_revisits_are_spread_oldest_first(self):
        # Staging 2026-11-18: every node fell due in one 82 s pass.
        now = 10_000.0

        @contextmanager
        def connect():
            yield _Connection()

        controller = SimpleNamespace(
            repo=SimpleNamespace(
                clock=lambda: now, engine=SimpleNamespace(connect=connect)
            ),
            _finished={},
        )
        old = now - finished.RECHECK_SECONDS
        for index in range(30):
            controller._finished[f"due-{index:02d}"] = ("fp", old - index)
        controller._finished["future"] = ("fp", now + 5)  # Clock went back.
        controller._finished["fresh-a"] = ("fp", now - 10)
        controller._finished["fresh-b"] = ("fp", now - 20)
        finished.finished_skips(controller, [])
        left = set(controller._finished)
        self.assertNotIn("future", left)
        self.assertTrue({"fresh-a", "fresh-b"} <= left)
        due_left = sorted(key for key in left if key.startswith("due-"))
        self.assertEqual(len(due_left), 30 - finished.REVISITS_PER_PASS)
        # The oldest (largest index) were revisited first.
        self.assertEqual(due_left, [f"due-{i:02d}" for i in range(10)])

    def test_a_fully_reported_watchdog_marker_needs_no_report_call(self):
        from studio_platform.capacity_watchdog import unreported_reclaims

        def node(marker):
            return {"payload": {"capacity_watchdog": marker}}

        self.assertFalse(unreported_reclaims({"payload": {}}))
        self.assertFalse(
            unreported_reclaims(node({"event_ids": ["a"], "reported_event_ids": ["a"]}))
        )
        self.assertTrue(unreported_reclaims(node({"event_ids": ["a", "b"]})))
        self.assertTrue(
            unreported_reclaims(node({"event_ids": ["a"], "reported_event_ids": []}))
        )
        self.assertTrue(unreported_reclaims(node("odd")))  # Original call decides.


if __name__ == "__main__":
    unittest.main()
