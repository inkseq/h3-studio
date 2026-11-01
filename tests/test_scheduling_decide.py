"""Routing decision table (#931 step 3): today's dstack start rules, pure."""

import unittest

from studio_platform.capacity_lookahead import Machine
from studio_platform.scheduling.decide import Rules, dstack_work

READY = Machine("modal-a", "ready")
STARTING = Machine("modal-b", "starting", ready_in_seconds=60)
MODAL_FIRST = Rules(vast_min_queue=20, vast_after_seconds=600)


def jobs(count, created=1000, prefix="j"):
    return [({"id": f"{prefix}{i}", "created_at": created}, {}) for i in range(count)]


# (name, queued, covered ids, rules, machines, expected ids)
CASES = [
    ("no rules: every uncovered job", jobs(3), set(), Rules(), (), ["j0", "j1", "j2"]),
    (
        "no rules: a Modal-covered job never also gets dstack",
        jobs(3),
        {"j1"},
        Rules(),
        (),
        ["j0", "j2"],
    ),
    (
        "modal first, no Modal host: dstack takes the floor",
        jobs(1),
        set(),
        MODAL_FIRST,
        (),
        ["j0"],
    ),
    (
        "modal first: Modal clears the queue before dstack is ready",
        jobs(25),
        set(),
        MODAL_FIRST,
        (READY, STARTING),
        [],
    ),
    (
        "modal first: 20 still left at ready starts dstack",
        jobs(60),
        set(),
        MODAL_FIRST,
        (READY, STARTING),
        [f"j{i}" for i in range(60)],
    ),
    (
        "modal first: a job past vast_after_seconds reaches dstack",
        jobs(1, created=300, prefix="old") + jobs(1),
        set(),
        MODAL_FIRST,
        (READY,),
        ["old0"],
    ),
    (
        "modal first: covered jobs stay covered even when old",
        jobs(1, created=300, prefix="old"),
        {"old0"},
        MODAL_FIRST,
        (READY,),
        [],
    ),
]


class DecideTableTests(unittest.TestCase):
    def test_table(self):
        for name, queued, covered, rules, machines, expected in CASES:
            with self.subTest(name):
                work = dstack_work(queued, covered, rules, machines, 1000)
                self.assertEqual([job["id"] for job, _ in work], expected)

    def test_rules_come_from_the_controller_modal_first_dict(self):
        self.assertEqual(Rules.from_modal_first(None), Rules())
        self.assertEqual(
            Rules.from_modal_first({"vast_min_queue": 20, "vast_after_seconds": 600}),
            MODAL_FIRST,
        )


if __name__ == "__main__":
    unittest.main()
