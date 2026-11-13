"""The pitfalls table: one format, and an entry acts only as far as its evidence goes (#1237)."""

import json
from pathlib import Path
import tempfile
import unittest

from studio_platform import pitfalls

REPO = Path(__file__).resolve().parents[1]
ANSWER = {
    "id": "take.example",
    "status": "proposed",
    "uses": ["complaint"],
    "user_words": ["太慢", "too slow"],
    "cause": "c",
    "fix": "f",
    "assistant": "point",
}
MEASURED = "2026-11-13 3 shots on one model: the fault went from 3 of 3 to 0 of 3."
RULE = {
    "id": "prompt.example",
    "status": "in_use",
    "uses": ["prevent", "check"],
    "models": ["model-a 1.0"],
    "match": {"shot_text": ["slowly|缓缓"], "film": "multi_day", "shot": "voiced"},
    "risk": "r",
    "cause": "c",
    "prevent": "p",
    "check": {"stage": "frames", "how": "judge", "question": "q?", "expected": "yes"},
    "fix": "f",
    "measured": MEASURED,
}


class PitfallFormatTests(unittest.TestCase):
    def test_the_shipped_table_is_sound_and_ids_are_unique(self):
        ids = [entry["id"] for entry in pitfalls.ENTRIES]
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))
        for entry in pitfalls.ENTRIES:
            self.assertEqual(pitfalls.problems(entry), [], entry["id"])

    def test_sound_entries_pass(self):
        self.assertEqual(pitfalls.problems(ANSWER), [])
        self.assertEqual(pitfalls.problems(RULE), [])
        retired = {**RULE, "status": "retired", "uses": []}
        self.assertEqual(pitfalls.problems(retired), [])

    def test_an_unmeasured_entry_cannot_rewrite_or_judge(self):
        for use in ("prevent", "check"):
            entry = {**RULE, "status": "proposed", "uses": [use]}
            entry.pop("measured")
            self.assertIn("unmeasured_entry_switched_on", pitfalls.problems(entry), use)
        only_measured = {**RULE, "status": "measured"}
        self.assertIn("unmeasured_entry_switched_on", pitfalls.problems(only_measured))

    def test_a_status_past_proposed_needs_a_dated_line(self):
        for status in ("measured", "in_use", "retired"):
            for line in (None, "", "works well", "13 Nov: fine"):
                entry = {**RULE, "status": status, "uses": []}
                entry.pop("measured")
                if line is not None:
                    entry["measured"] = line
                self.assertIn(
                    "measured_needs_a_dated_line",
                    pitfalls.problems(entry),
                    (status, line),
                )

    def test_a_retired_entry_acts_nowhere(self):
        entry = {**RULE, "status": "retired"}
        self.assertIn("retired_entry_in_use", pitfalls.problems(entry))

    def test_each_use_needs_its_fields(self):
        cases = (
            ({**ANSWER, "user_words": []}, "user_words"),
            ({**ANSWER, "fix": " "}, "fix"),
            ({**ANSWER, "assistant": "maybe"}, "assistant"),
            ({**ANSWER, "assistant": "edit: "}, "assistant"),
            ({**RULE, "prevent": ""}, "prevent"),
            ({**RULE, "match": {}}, "match"),
            ({**RULE, "check": {**RULE["check"], "how": "guess"}}, "check"),
            ({**RULE, "match": {"shot_text": ["("]}}, "match_shot_text"),
            ({**RULE, "match": {"words": ["x"]}}, "match"),
            ({**ANSWER, "id": "Take.Example"}, "id"),
            ({**ANSWER, "status": "live"}, "status"),
            ({**ANSWER, "uses": ["complaint", "ship"]}, "uses"),
            ({**ANSWER, "note": "x"}, "unknown_field:note"),
        )
        for entry, problem in cases:
            self.assertIn(problem, pitfalls.problems(entry), problem)

    def test_a_broken_table_stops_the_app(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pitfalls.json"
            for entries in ([{**ANSWER, "fix": ""}], [ANSWER, ANSWER], "x"):
                path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    pitfalls.load(path)
            path.write_text(json.dumps({"entries": [ANSWER, RULE]}), encoding="utf-8")
            self.assertEqual(len(pitfalls.load(path)), 2)

    def test_used_for_keeps_the_table_order(self):
        answering = [e["id"] for e in pitfalls.ENTRIES if "complaint" in e["uses"]]
        self.assertEqual([e["id"] for e in pitfalls.used_for("complaint")], answering)
        self.assertEqual(pitfalls.used_for("nothing"), ())

    def test_releases_carry_the_table(self):
        for ignore in (".gitignore", ".dockerignore"):
            text = (REPO / ignore).read_text(encoding="utf-8")
            self.assertIn("!studio_platform/pitfalls.json", text, ignore)


if __name__ == "__main__":
    unittest.main()
