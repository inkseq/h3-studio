"""Risk advice reuses the existing scripted turn; no live model/GPU calls."""

import copy
import unittest

from studio_platform.story_risk import review
import test_story_chat as fixture


class RiskRulesTests(unittest.TestCase):
    def report(self, text, seconds=5, **plan):
        lookup = {
            "s": {
                "id": "s",
                "type": "shot",
                "data": {"prompt": text, "seconds": seconds},
            }
        }
        return review(["s"], lookup, {}, plan)

    def codes(self, result):
        return {w["code"] for w in result["warnings"]}

    def test_simultaneous_not_merely_sequential_or_quoted_words(self):
        self.assertIn(
            "simultaneous_speakers",
            self.codes(
                self.report(
                    "Both speak simultaneously. Mei (S1) <d>[Chinese] 你好</d> Li (S2) <d>[Chinese] 好</d>"
                )
            ),
        )
        self.assertNotIn(
            "simultaneous_speakers",
            self.codes(
                self.report(
                    "Mei speaks; Li listens. (S1) <d>[English] They spoke simultaneously.</d> Then Li (S2) <d>[English] Yes.</d>",
                    10,
                )
            ),
        )
        self.assertIn(
            "simultaneous_speakers",
            self.codes(
                self.report(
                    "两人异口同声说 (S1) <d>[Chinese] 好</d> (S2) <d>[Chinese] 好</d>"
                )
            ),
        )

    def test_duration_action_and_plot_density_are_independent_suggestions(self):
        self.assertIn(
            "speech_too_dense",
            self.codes(self.report("(S1) <d>[Chinese] " + "说" * 31 + "</d>", 5)),
        )
        self.assertNotIn(
            "speech_too_dense", self.codes(self.report("(S1) <d>[Chinese] 你好</d>", 5))
        )
        result = self.report(
            "She runs, then jumps, then opens the door, then fights. Cut to the next day.",
            5,
        )
        self.assertTrue({"action_density", "plot_density"}.issubset(self.codes(result)))
        for warning in result["warnings"]:
            self.assertEqual(
                set(warning), {"shot_id", "code", "source", "message", "suggestion"}
            )

    def test_missing_invalid_and_stale_model_are_not_clean_reviews(self):
        self.assertEqual(self.report("A quiet face.")["status"], "incomplete")
        self.assertEqual(
            self.report("A quiet face.", risk_review=[])["status"], "no_findings"
        )
        for value in (
            None,
            {},
            [{"shot": "foreign", "codes": ["action_density"], "suggestion": "split"}],
            [{"shot": "s", "codes": ["auto_generate"], "suggestion": "do it"}],
            [{"shot": "s", "codes": ["action_density"], "suggestion": "x" * 501}],
        ):
            result = self.report("A quiet face.", risk_review=value)
            self.assertEqual(result["model_status"], "invalid")
            self.assertEqual(result["warnings"], [])
        self.assertEqual(
            self.report("A quiet face.", seconds=float("nan"), risk_review=[])[
                "rule_status"
            ],
            "partial",
        )


class StoryRiskHttpTests(unittest.TestCase):
    setUp = fixture.StoryChatTests.setUp
    login = fixture.StoryChatTests.login
    start = fixture.StoryChatTests.start
    turn = fixture.StoryChatTests.turn
    jobs = fixture.StoryChatTests.jobs
    actions = fixture.StoryChatTests.actions

    def prepare(self):
        self.chat = self.start()
        self.built = self.turn(self.chat, "Make a story").json()
        self.shot = self.built["cards"]["shots"][0]["id"]

    def proposal(self, **changes):
        return {
            "reply": "Review the risks before confirming.",
            "actions": [{"tool": "propose_generate", "shots": [self.shot]}],
            "risk_review": [
                {
                    "shot": self.shot,
                    "codes": ["action_density"],
                    "suggestion": "Split into two shots; keep the first action.",
                }
            ],
            **changes,
        }

    def test_same_turn_proposal_no_edits_or_extra_call_and_replay(self):
        self.prepare()
        path = f"/v1/projects/{self.chat['project_id']}"
        before = self.client.get(path).json()["project"]["entities"]
        result = self.turn(self.chat, "Generate it", "turn-risk-01", self.proposal())
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        report = value["turn"]["proposal"]["risk_review"]
        self.assertEqual(report["model_status"], "reported")
        self.assertEqual(report["status"], "warnings")
        self.assertEqual(value["turn"]["changes"], [])
        self.assertEqual(self.jobs(), 0)
        self.assertEqual(len(self.model.calls), 2)  # build, then normal chat turn
        self.assertEqual(self.client.get(path).json()["project"]["entities"], before)
        replay = self.turn(self.chat, "Generate it", "turn-risk-01", plan=None).json()
        self.assertEqual(replay["turn"], value["turn"])
        self.assertEqual(len(self.model.calls), 2)

    def test_later_edit_marks_presented_report_stale_without_rewriting_receipt(self):
        self.prepare()
        value = self.turn(
            self.chat, "Generate it", "turn-risk-02", self.proposal()
        ).json()
        self.actions(
            self.chat,
            value["version"],
            [
                {
                    "op": "entity.update",
                    "entity_id": self.shot,
                    "patch": {"data": {"prompt": "Different scene."}},
                }
            ],
        )
        viewed = self.client.get(f"/v1/story-chats/{self.chat['project_id']}").json()
        self.assertEqual(
            viewed["messages"][-1]["proposal"]["risk_review"]["status"], "stale"
        )
        stored = self.client.get(f"/v1/projects/{self.chat['project_id']}").json()[
            "project"
        ]["journey"]["storyChat"]["messages"][-1]
        self.assertEqual(stored["proposal"]["risk_review"]["status"], "warnings")
        self.assertEqual(len(self.model.calls), 2)

    def test_concurrent_edit_discards_model_advice_and_rechecks_rules(self):
        self.prepare()

        def edit():
            self.actions(
                self.chat,
                self.built["version"],
                [
                    {
                        "op": "entity.update",
                        "entity_id": self.shot,
                        "patch": {
                            "data": {
                                "prompt": "She runs then jumps then fights then falls."
                            }
                        },
                    }
                ],
            )

        self.model.during = edit
        result = self.turn(self.chat, "Generate it", "turn-risk-03", self.proposal())
        self.assertEqual(result.status_code, 200, result.text)
        report = result.json()["turn"]["proposal"]["risk_review"]
        self.assertEqual(report["model_status"], "stale")
        self.assertTrue(report["warnings"])
        self.assertTrue(all(w["source"] == "rule" for w in report["warnings"]))
        self.assertEqual(self.jobs(), 0)

    def test_new_refs_resolve_and_invalid_llm_advice_does_not_edit(self):
        self.prepare()
        plan = self.proposal(
            actions=[
                {
                    "tool": "upsert_shot",
                    "ref": "new:extra",
                    "text": "A quiet face.",
                    "seconds": 5,
                },
                {"tool": "propose_generate", "shots": ["new:extra"]},
            ],
            risk_review=[
                {
                    "shot": "new:extra",
                    "codes": ["plot_density"],
                    "suggestion": "Consider a separate establishing shot.",
                }
            ],
        )
        response = self.turn(self.chat, "Add that shot", "turn-risk-04", plan)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        report = value["turn"]["proposal"]["risk_review"]
        self.assertEqual(report["status"], "warnings")
        self.assertEqual(
            report["warnings"][0]["shot_id"], value["cards"]["shots"][-1]["id"]
        )
        malformed = self.proposal(
            risk_review=[
                {
                    "shot": "foreign",
                    "codes": ["plot_density"],
                    "suggestion": "Delete all shots",
                }
            ]
        )
        value = self.turn(self.chat, "Check original", "turn-risk-05", malformed).json()
        self.assertEqual(
            value["turn"]["proposal"]["risk_review"]["model_status"], "invalid"
        )
        self.assertEqual(value["turn"]["changes"], [])
        self.assertEqual(self.jobs(), 0)

    def test_old_turn_and_image_only_proposal_remain_without_risk_report(self):
        self.prepare()
        self.assertIsNone(self.built["turn"]["proposal"])
        character = self.built["cards"]["characters"][0]["id"]
        result = self.turn(
            self.chat,
            "Draw",
            "turn-risk-06",
            {
                "reply": "Confirm drawing",
                "actions": [{"tool": "propose_image", "ref": character}],
                "risk_review": [],
            },
        ).json()
        self.assertNotIn("risk_review", result["turn"]["proposal"])

    def test_authenticated_and_public_discovery_agree(self):
        self.login()
        for path in ("/v1/guided-schema", "/v1/agent-guide", "/for-agents/guide.json"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()["story_risk_review"]
            self.assertEqual(
                value["codes"],
                [
                    "simultaneous_speakers",
                    "speech_too_dense",
                    "action_density",
                    "plot_density",
                ],
            )
            self.assertIn("no new endpoint/call", value["location"])
