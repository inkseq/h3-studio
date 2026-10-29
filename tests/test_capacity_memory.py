"""Offer headroom is separate from execution evidence and accepted requests."""

import copy
from types import SimpleNamespace
import unittest

from studio_platform.capacity_candidates import _physical_limits, _spec_blockers
from studio_platform.capacity_memory import (
    catalog_resources,
    planning_ram_gib,
    require_preview_ram,
    vast_ram_floor,
)
from studio_platform.creator_controls import TAOMATE
from studio_platform.operator_capacity import OperatorError
from studio_platform.runtime_catalog import get_profile, PROFILE_IDS


class PlanningMemoryTests(unittest.TestCase):
    def test_taomate_floor_preserves_stricter_config_and_runtime_bytes(self):
        before = get_profile(TAOMATE)
        for configured, expected in ((29, 41), (32, 41), (41, 41), (48, 48), (96, 96)):
            self.assertEqual(planning_ram_gib(TAOMATE, configured), expected)
        self.assertEqual(before, get_profile(TAOMATE))
        for profile_id in PROFILE_IDS[:-2]:
            self.assertEqual(planning_ram_gib(profile_id, 32), 32)
        self.assertEqual(planning_ram_gib(PROFILE_IDS[-1], 32), 41)

    def test_catalog_and_candidates_show_same_effective_floor(self):
        profile = {
            'runtime_profile_id': TAOMATE,
            'memory_gib': 32,
            'max_price_microusd': 850000,
        }
        before = copy.deepcopy(profile)
        result = catalog_resources(profile, tuple(profile))
        self.assertEqual(result['memory_gib'], 41)
        self.assertEqual(result['max_price_microusd'], 850000)
        self.assertEqual(profile, before)
        self.assertEqual(_physical_limits(get_profile(TAOMATE))['min_ram_gib'], 41)
        row = dict(
            ram_gib=32,
            disk_gib=500,
            cpu_cores=16,
            gpu_count=1,
            hourly_cost_microusd=800000,
            price_per_gpu_hour_microusd=800000,
        )
        self.assertIn(
            'inventory_ram_below_minimum', _spec_blockers(get_profile(TAOMATE), row)
        )
        self.assertNotIn(
            'inventory_ram_below_minimum',
            _spec_blockers(get_profile(TAOMATE), {**row, 'ram_gib': 48}),
        )

    def test_old_preview_rejected_without_rewriting_document(self):
        profile = {'runtime_profile_id': TAOMATE, 'memory_gib': 32}
        for memory in (29, 32, 40, True, None):
            document = {'memory_gib': memory}
            before = copy.deepcopy(document)
            with self.assertRaisesRegex(OperatorError, 'preview_memory_headroom_stale'):
                require_preview_ram(document, profile)
            self.assertEqual(document, before)
        good = {'memory_gib': 41}
        self.assertIs(require_preview_ram(good, profile), good)
        with self.assertRaises(OperatorError):
            require_preview_ram(good, {**profile, 'memory_gib': 48})

    def test_vast_query_uses_exact_mib_for_taomate_not_wider_decimal_floor(self):
        request = SimpleNamespace(profile_id=TAOMATE, memory_gib=41)
        self.assertEqual(vast_ram_floor(request), 41984)
        self.assertGreater(vast_ram_floor(request), 32502)
        request.profile_id = PROFILE_IDS[0]
        self.assertEqual(vast_ram_floor(request), 41000)


if __name__ == '__main__':
    unittest.main()
