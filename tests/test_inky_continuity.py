import json
import unittest

from studio_platform import inky_continuity as continuity
from studio_platform import inky_models as core


def project(*texts, cards=()):
    entities = [
        {"id": "scene", "type": "scene", "parentId": None, "order": 0, "data": {}}
    ]
    entities += [
        {
            "id": ident,
            "type": kind,
            "parentId": None,
            "title": name,
            "description": text,
            "data": {},
        }
        for ident, kind, name, text in cards
    ]
    cast = [card[0] for card in cards if card[1] == "character"]
    entities += [
        {
            "id": f"s{index}",
            "type": "shot",
            "parentId": "scene",
            "order": index,
            "version": 1,
            "data": {"prompt": text, "seconds": 5, "cast": cast},
        }
        for index, text in enumerate(texts, start=1)
    ]
    return {"entities": entities, "links": []}


class ReviewTests(unittest.TestCase):
    def test_each_shot_is_compared_with_what_came_before_it_in_reading_order(self):
        value = project("first", "second", "third")
        value["entities"][1]["order"], value["entities"][3]["order"] = 3, 1
        result = continuity.reviews(value)
        self.assertEqual(list(result), ["s2", "s1"])
        self.assertIn("third", result["s2"]["state"]["story_so_far"])
        self.assertNotIn("first", result["s2"]["state"]["story_so_far"])
        self.assertEqual(result["s1"]["numbers"], {1: "s3", 2: "s2"})

    def test_an_empty_shot_is_neither_reviewed_nor_compared_against(self):
        result = continuity.reviews(project("first", "", "third"))
        self.assertEqual(list(result), ["s3"])
        self.assertEqual(list(result["s3"]["numbers"]), [1])

    def test_only_the_shot_s_own_cards_go_along(self):
        value = project(
            "first",
            "second",
            cards=[("lin", "character", "Lin", "beige coat")],
        )
        value["entities"].append(
            {
                "id": "zhou",
                "type": "character",
                "parentId": None,
                "title": "Zhou",
                "description": "lantern",
                "data": {},
            }
        )
        cards = continuity.reviews(value)["s2"]["state"]["cards"]
        self.assertEqual(cards, {"Lin": "beige coat"})

    def test_a_long_story_stays_under_the_judge_s_limit_and_keeps_the_nearest_shots(
        self,
    ):
        texts = [f"第{index}镜" + "雨" * 1900 for index in range(60)]
        state = continuity.reviews(project(*texts))["s60"]["state"]
        self.assertLessEqual(
            len(json.dumps(state, ensure_ascii=False)), continuity.MAX_STATE_CHARS
        )
        self.assertIn("第58镜", state["story_so_far"])
        self.assertNotIn("第0镜", state["story_so_far"])
        self.assertGreater(state["earlier_shots_not_shown"], 0)

    def test_an_earlier_edit_changes_the_review_of_what_follows(self):
        before = continuity.reviews(project("a", "b", "c"))
        after = continuity.reviews(project("changed", "b", "c"))
        self.assertNotEqual(before["s3"]["digest"], after["s3"]["digest"])

    def test_the_same_story_gets_the_same_review(self):
        self.assertEqual(
            continuity.reviews(project("a", "b"))["s2"]["digest"],
            continuity.reviews(project("a", "b"))["s2"]["digest"],
        )


class NoteTests(unittest.TestCase):
    numbers = {1: "s1", 2: "s2"}

    def note(
        self, verdict="mismatch", with_shot=1, note="信已撕开，这里完好。", lang="zh"
    ):
        raw = json.dumps({"verdict": verdict, "with_shot": with_shot, "note": note})
        return core.continuity_note(raw, lang, self.numbers)

    def test_mismatch_and_question(self):
        self.assertEqual(
            self.note(),
            {
                "kind": "continuity_mismatch",
                "note": "信已撕开，这里完好。",
                "other_shot_id": "s1",
            },
        )
        self.assertEqual(self.note("explain")["kind"], "continuity_question")

    def test_a_clash_with_a_card_names_no_shot(self):
        self.assertIsNone(self.note(with_shot=None)["other_shot_id"])

    def test_none_means_nothing_to_say(self):
        self.assertIsNone(self.note("none", None, ""))

    def test_refuses_what_it_cannot_show(self):
        for value in (
            {"with_shot": 9},
            {"note": ""},
            {"note": "x" * 61},
            {"note": "English note"},
            {"note": "模型认为有矛盾"},
            {"verdict": "maybe"},
        ):
            with self.subTest(value), self.assertRaises(ValueError):
                self.note(**value)
        with self.assertRaises(ValueError):
            self.note(note="中文", lang="en")
        with self.assertRaises(ValueError):
            core.continuity_note("not json", "zh", self.numbers)

    def test_score_needs_a_real_probability(self):
        good = {"answers": {"conflict": {"noul": 0.7}}}
        self.assertEqual(core.continuity_score(good), 0.7)
        for bad in ({}, {"answers": {"conflict": {"noul": 2}}}, None):
            with self.subTest(bad), self.assertRaises(ValueError):
                core.continuity_score(bad)
