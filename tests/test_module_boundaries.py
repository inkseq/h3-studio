"""#812: no new cross-domain imports beyond tools/module-boundaries.json."""

import json
import unittest

from tools import module_boundaries as boundaries


class ModuleBoundaryTests(unittest.TestCase):
    def test_no_new_cross_domain_imports(self):
        recorded = set(
            json.loads(boundaries.BASELINE.read_text(encoding="utf-8"))["edges"]
        )
        added = [
            f"{s} -> {t}"
            for s, t in boundaries.edges()
            if f"{s} -> {t}" not in recorded
        ]
        self.assertEqual(
            added,
            [],
            "New cross-domain imports; use the owning domain's interface or "
            "record a deliberate edge with python tools/module_boundaries.py --write",
        )

    def test_domains_are_assigned_by_prefix(self):
        self.assertEqual(boundaries.domain("credits_routes"), "billing")
        self.assertEqual(boundaries.domain("modal_host"), "capacity")
        self.assertEqual(boundaries.domain("story_chat"), "creation")
        self.assertEqual(boundaries.domain("repository"), "platform")


if __name__ == "__main__":
    unittest.main()
