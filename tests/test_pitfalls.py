"""The pitfalls table: one format, and an entry acts only as far as its evidence goes (#1237)."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

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
        ids = [entry["id"] for entry in pitfalls.BASE]
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))
        for entry in pitfalls.BASE:
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

    def test_advice_before_shooting_may_be_in_two_languages_with_narrowing_rules(self):
        both = {"zh": "把东西放进首帧。", "en": "Put the object in the first frame."}
        entry = {
            **RULE,
            "prevent": both,
            "ask": {"zh": "帮我改第 {number} 镜", "en": "Fix shot {number} for me"},
            "match": {"shot_text": ["皮筋"], "with": ["抽|拿"], "unless": ["松开"]},
        }
        self.assertEqual(pitfalls.problems(entry), [])
        self.assertEqual(pitfalls.in_language(both, "zh"), both["zh"])
        self.assertEqual(pitfalls.in_language(both, "fr"), both["en"])
        self.assertEqual(pitfalls.in_language("one text", "zh"), "one text")
        cases = (
            ({**entry, "prevent": {"zh": "只有中文"}}, "prevent"),
            ({**entry, "ask": {"zh": "", "en": "x"}}, "ask"),
            ({**entry, "match": {"shot_text": ["a"], "unless": ["("]}}, "match_unless"),
            ({**entry, "match": {"shot_text": ["a"], "with": "b"}}, "match_with"),
            (
                {**entry, "prevent": {"zh": "换成 Pika 试试", "en": "x"}},
                "names_internals",
            ),
            ({**entry, "ask": "Ask Gemini about shot {number}"}, "names_internals"),
        )
        for bad, problem in cases:
            self.assertIn(problem, pitfalls.problems(bad), problem)

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
        answering = [e["id"] for e in pitfalls.BASE if "complaint" in e["uses"]]
        self.assertEqual([e["id"] for e in pitfalls.used_for("complaint")], answering)
        self.assertEqual(pitfalls.used_for("nothing"), ())

    def test_releases_carry_the_table(self):
        for ignore in (".gitignore", ".dockerignore"):
            text = (REPO / ignore).read_text(encoding="utf-8")
            self.assertIn("!studio_platform/pitfalls.json", text, ignore)


class PrivateTableTests(unittest.TestCase):
    """Measured lessons live outside this public repository and are laid over the table."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(pitfalls.configure, None)
        self.path = Path(folder.name) / "runtime-secrets" / pitfalls.PRIVATE_NAME
        self.path.parent.mkdir()
        # The real reader wants a root-owned file; these tests are about the table.
        plain = mock.patch.object(
            pitfalls, "_read_private", lambda path: json.loads(path.read_text("utf-8"))
        )
        plain.start()
        self.addCleanup(plain.stop)
        every_look = mock.patch.object(pitfalls, "RECHECK_SECONDS", 0)
        every_look.start()
        self.addCleanup(every_look.stop)
        pitfalls.configure(SimpleNamespace(data_dir=folder.name))

    def write(self, entries):
        self.path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        os.utime(self.path, ns=(self.stamp(), self.stamp()))

    def stamp(self):
        self.tick = getattr(self, "tick", 1_000_000_000) + 1_000_000_000
        return self.tick

    def test_without_a_private_file_the_public_table_is_in_force(self):
        self.assertEqual(pitfalls.state(), "absent")
        self.assertIs(pitfalls.entries(), pitfalls.BASE)

    def test_private_entries_replace_by_id_and_new_ones_follow(self):
        first = pitfalls.BASE[0]
        mine = {**first, "fix": "A better fix."}
        self.write([RULE, mine])
        self.assertEqual(pitfalls.state(), "loaded")
        table = pitfalls.entries()
        self.assertEqual(table[0]["fix"], "A better fix.")
        self.assertEqual(
            [e["id"] for e in table[:-1]], [e["id"] for e in pitfalls.BASE]
        )
        self.assertEqual(table[-1]["id"], RULE["id"])
        self.assertEqual([e["id"] for e in pitfalls.used_for("prevent")], [RULE["id"]])

    def test_the_fix_guide_follows_the_table_in_force(self):
        from studio_platform import inky_fix_guide as guide

        self.assertEqual(guide.match("画面里有只猫头鹰"), [])
        owl = {**ANSWER, "id": "take.owl", "user_words": ["猫头鹰", "an owl"]}
        self.write([owl])
        self.assertEqual(
            [row["id"] for row in guide.match("画面里有只猫头鹰")], ["take.owl"]
        )
        self.path.unlink()
        self.assertEqual(guide.match("画面里有只猫头鹰"), [])
        self.assertEqual(pitfalls.state(), "absent")

    def test_an_unsound_private_file_changes_nothing_and_is_logged_without_content(
        self,
    ):
        cases = (
            [{**ANSWER, "fix": "Use Gemini instead."}],
            [{**RULE, "status": "proposed"}],
            [ANSWER, ANSWER],
            "not a table",
        )
        for entries in cases:
            with self.assertLogs("studio_platform.pitfalls", "WARNING") as logs:
                self.write(entries)
                self.assertEqual(pitfalls.state(), "invalid")
            self.assertIs(pitfalls.entries(), pitfalls.BASE)
            self.assertNotIn("Gemini", "".join(logs.output))
        self.write([ANSWER])
        self.assertEqual(pitfalls.state(), "loaded")

    def test_the_file_is_read_once_per_change(self):
        reads = []

        def counting(path):
            reads.append(1)
            return json.loads(path.read_text("utf-8"))

        with mock.patch.object(pitfalls, "_read_private", counting):
            self.write([ANSWER])
            for _ in range(5):
                pitfalls.entries()
            self.assertEqual(len(reads), 1)
            self.write([ANSWER, RULE])
            self.assertEqual(len(pitfalls.entries()), len(pitfalls.BASE) + 2)
            self.assertEqual(len(reads), 2)

    @unittest.skipIf(
        os.name == "nt" or os.geteuid() == 0, "needs a non-root POSIX user"
    )
    def test_a_private_file_must_be_root_owned(self):
        mock.patch.stopall()
        self.addCleanup(pitfalls.configure, None)
        with mock.patch.object(pitfalls, "RECHECK_SECONDS", 0):
            pitfalls.configure(SimpleNamespace(data_dir=str(self.path.parents[1])))
            self.path.write_text(json.dumps({"entries": [ANSWER]}), encoding="utf-8")
            with self.assertLogs("studio_platform.pitfalls", "WARNING"):
                self.assertEqual(pitfalls.state(), "invalid")
            self.assertIs(pitfalls.entries(), pitfalls.BASE)

    def test_the_check_command(self):
        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pitfalls.main(list(argv))
            return code, out.getvalue()

        self.assertEqual(run("check", str(pitfalls.PATH))[0], 0)
        self.write([ANSWER, RULE])
        code, said = run("check", str(self.path))
        self.assertEqual(code, 0)
        self.assertIn("2 entries", said)
        self.write([{**RULE, "status": "measured"}])
        code, said = run("check", str(self.path))
        self.assertEqual(code, 1)
        self.assertIn("unmeasured_entry_switched_on", said)
        self.assertEqual(run("install")[0], 2)


if __name__ == "__main__":
    unittest.main()
