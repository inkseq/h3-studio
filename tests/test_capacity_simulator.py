"""Every controller-simulator scenario (#1034); fake provider, no network or GPU.

A scenario either keeps every invariant, or fails exactly as its recorded
known controller bug says. A known-bug scenario that starts passing fails
here too, so its ``expected_failure`` is removed together with the fix.
"""

import unittest

from capacity_scenarios import SCENARIOS
from capacity_sim import format_report, run_scenario


class ControllerSimulatorTests(unittest.TestCase):
    def test_scenario_names_are_unique(self):
        names = [scenario.name for scenario in SCENARIOS]
        self.assertEqual(len(names), len(set(names)))


def _case(scenario):
    def test(self):
        report = run_scenario(scenario)
        expected = "expected_fail" if scenario.expected_failure else "pass"
        self.assertEqual(report["status"], expected, "\n" + format_report(report))

    test.__doc__ = scenario.description
    return test


for _scenario in SCENARIOS:
    setattr(ControllerSimulatorTests, "test_" + _scenario.name, _case(_scenario))


if __name__ == "__main__":
    unittest.main()
