"""Ratchets must catch regression even when a PR raises its baseline numbers."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.check_maintainability import (
    compare,
    inventory,
    prior_baseline,
    snapshot,
    validate,
)


class RatchetTests(unittest.TestCase):
    def test_rule_increase_cannot_be_hidden_by_editing_baseline(self):
        before = snapshot({"C901": 2, "F401": 0}, {})
        after = snapshot({"C901": 3, "F401": 1}, {})
        errors = compare(after, after, before, enforce_lines=True)
        self.assertTrue(any("C901" in item for item in errors))
        self.assertTrue(any("F401" in item for item in errors))

    def test_reductions_pass_and_lower_snapshot_prevents_regrowth(self):
        before = snapshot({"B905": 4}, {"studio_platform/big.py": 1700})
        after = snapshot({"B905": 3}, {"studio_platform/big.py": 1690})
        self.assertFalse(compare(after, before, before, enforce_lines=True))
        self.assertFalse(compare(after, after, before, enforce_lines=True))
        self.assertTrue(compare(before, before, after, enforce_lines=True))

    def test_unused_baseline_headroom_cannot_be_raised(self):
        before = snapshot({"C901": 2}, {"studio_platform/big.py": 1700})
        raised = snapshot({"C901": 3}, {"studio_platform/big.py": 1701})
        self.assertTrue(compare(before, raised, before, enforce_lines=True))

    def test_new_or_renamed_oversized_module_refused(self):
        before = snapshot({}, {"studio_platform/big.py": 1800})
        for sizes in (
            {"studio_platform/new.py": 1501},
            {"studio_platform/big.py": 1801},
        ):
            after = snapshot({}, sizes)
            self.assertTrue(compare(after, after, before, enforce_lines=True))
        allowed = snapshot(
            {}, {"studio_platform/new.py": 1500, "studio_platform/big.py": 1800}
        )
        self.assertFalse(compare(allowed, allowed, before, enforce_lines=True))

    def test_module_line_caps_only_warn_during_development(self):
        before = snapshot({"C901": 2}, {"studio_platform/big.py": 1800})
        after = snapshot({"C901": 2}, {"studio_platform/big.py": 1801})
        warnings = []
        self.assertEqual(compare(after, after, before, warnings=warnings), [])
        self.assertEqual(
            warnings, ["studio_platform/big.py: 1801 lines > prior cap 1800"]
        )
        rules = snapshot({"C901": 3}, {"studio_platform/big.py": 1801})
        self.assertTrue(compare(rules, rules, before, warnings=[]))

    def test_new_rule_debt_is_zero_not_unlimited(self):
        before = snapshot({}, {})
        after = snapshot({"B904": 1}, {})
        self.assertTrue(compare(after, after, before, enforce_lines=True))

    def test_fatal_or_invalid_baseline_refused(self):
        for rules in ({"E999": 1}, {"F401": -1}, {"F401": True}):
            with self.assertRaises(ValueError):
                validate(snapshot(rules, {}))
        value = snapshot({}, {})
        value["max_module_lines"] = 9999
        with self.assertRaises(ValueError):
            validate(value)

    def test_git_base_absence_is_distinct_from_missing_ref_and_private_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True
                )

            git("init")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "user.name", "Fixture")
            (root / ".gitignore").write_text("private.py\n")
            (root / "studio_platform").mkdir()
            (root / "studio_platform/public.py").write_text("x=1\nx=2\n")
            (root / "studio_platform/private.py").write_text("hidden\n" * 2000)
            git("add", ".")
            git("commit", "-m", "fixture")
            self.assertIsNone(prior_baseline(root, "HEAD"))
            with self.assertRaises(subprocess.CalledProcessError):
                prior_baseline(root, "missing")
            (root / "studio_platform/new.py").write_text("new\n")
            self.assertEqual(
                inventory(root),
                {"studio_platform/public.py": 2, "studio_platform/new.py": 1},
            )
            (root / "tools").mkdir()
            before = snapshot({"F401": 0}, {})
            (root / "tools/maintainability-baseline.json").write_text(
                json.dumps(before)
            )
            git("add", ".")
            git("commit", "-m", "baseline")
            self.assertEqual(prior_baseline(root, "HEAD"), before)


if __name__ == "__main__":
    unittest.main()
