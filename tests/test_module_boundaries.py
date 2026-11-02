"""#812: no new cross-domain imports beyond tools/module-boundaries.json."""

import json
from pathlib import Path
import tempfile
import unittest

from tools import module_boundaries as boundaries


class ModuleBoundaryTests(unittest.TestCase):
    def test_raw_edges_include_local_composition_and_same_domain_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "api.py").write_text("from . import auth_routes\n")
            (package / "auth_routes.py").write_text(
                "def handler():\n    from .api import COOKIE\n"
                "from .identity import public_profile\n"
            )
            (package / "identity.py").write_text("")
            self.assertEqual(
                boundaries.import_edges(package),
                [
                    ("api", "auth_routes"),
                    ("auth_routes", "api"),
                    ("auth_routes", "identity"),
                ],
            )
            # The existing cross-domain ratchet still has its original policy.
            self.assertEqual(boundaries.edges(package), [("api", "auth_routes")])

    def test_domain_routes_and_evidence_do_not_import_their_composition_root(self):
        raw = set(boundaries.import_edges())
        for source in (
            "account_session_routes",
            "account_deletion_routes",
            "workos_routes",
        ):
            with self.subTest(source=source):
                self.assertNotIn((source, "api"), raw)
        self.assertNotIn(("controller_idle", "dstack_controller"), raw)

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
