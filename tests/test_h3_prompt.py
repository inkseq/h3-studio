import json
import unittest

from studio_platform import h3_prompt
from studio_platform.inference import wangp_full_compiler as compiler

NL = chr(10)
LINE1 = "(S1) <d>[Chinese] 追！宰了他！</d>"
LINE2 = "(S2) <d>[Chinese] 要不……先问问？</d>"
ACTION = "黄昏低光，左头怒吼 " + LINE1 + "，中头迟疑说 " + LINE2 + "。"


def project():
    return {
        "entities": [
            {
                "id": "herd",
                "type": "character",
                "title": "三身牧牛人",
                "description": "三个头共用一个身体",
                "data": {
                    "looks": [{"id": "main", "imageIds": ["img-herd"]}],
                    "voices": [{"id": "v1", "assetId": "voice-herd"}],
                },
            },
            {
                "id": "pen",
                "type": "location",
                "title": "草地与石砌牛圈",
                "description": "海边草地",
                "data": {"versions": [{"id": "a", "imageIds": ["img-pen"]}]},
            },
            {
                "id": "shot",
                "type": "shot",
                "description": ACTION,
                "data": {"prompt": ACTION, "storyboard": {"mode": "ref"}},
            },
        ]
    }


def draft():
    return {
        "inputs": {
            "images": [{"asset_id": "a"}, {"asset_id": "b"}],
            "audios": [{"asset_id": "c"}],
            "videos": [],
            "first_frame": None,
            "last_frame": None,
            "guides": [],
        },
        "controls": {"duration": 6},
        "storyboard": {
            "prompt_parts": [
                {"kind": "place", "entity_id": "pen", "text": "x"},
                {"kind": "cast", "entity_id": "herd", "text": "x"},
                {"kind": "action", "text": ACTION},
            ],
            "references": [
                {"entity_id": "img-herd", "slot": "images", "sent": True},
                {"entity_id": "voice-herd", "slot": "audios", "sent": True},
                {"entity_id": "img-pen", "slot": "images", "sent": True},
            ],
        },
    }


def good():
    return NL.join(
        [
            "subject_definitions:",
            "<Subject 1> is the three-headed herdsman in <Picture 1>.",
            "<Subject 2> is the seaside cattle pen in <Picture 2>.",
            "<Audio 1> is the voice-timbre reference for <Subject 1> (S1).",
            "summary:",
            "[reference generation + audio reference] Dusk at the pen.",
            "retention_analysis:",
            "<Subject 1> (appears in [Shot 1]): fully_preserved - all three heads.",
            "<Audio 1>: reference - only the voice timbre is followed.",
            "detailed_description:",
            "A grounded cinematic style. [Shot 1] The left head <Subject 1> " + LINE1,
            "then the middle head " + LINE2 + " and closes its lips.",
            "overall_soundscape:",
            "Wind and surf.",
            "non_diegetic_music:",
            "N/A",
        ]
    )


class FakeClient:
    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    def generate(self, model, messages, **kwargs):
        self.calls.append(messages)
        return {"text": self.texts.pop(0)}


class SourceTests(unittest.TestCase):
    def test_numbers_pictures_and_voices_in_sent_order(self):
        value = project()
        facts = h3_prompt.source(value, value["entities"][2], draft())
        self.assertEqual(facts["counts"], {"Picture": 2, "Audio": 1, "Video": 0})
        self.assertEqual(facts["seconds"], 6)
        place, herd = facts["cards"]
        self.assertEqual(place["pictures"], ["<Picture 2>"])
        self.assertEqual(herd["pictures"], ["<Picture 1>"])
        self.assertEqual(herd["voice"], "<Audio 1>")

    def test_skips_shots_it_cannot_number_or_already_written(self):
        value = project()
        shot = value["entities"][2]
        bad = draft()
        bad["inputs"]["images"].append({"asset_id": "z"})
        self.assertIsNone(h3_prompt.source(value, shot, bad))
        written = dict(shot, data={**shot["data"], "prompt": good()})
        self.assertIsNone(h3_prompt.source(value, written, draft()))
        first_last = dict(shot, data={**shot["data"], "storyboard": {"mode": "fl"}})
        self.assertIsNone(h3_prompt.source(value, first_last, draft()))


class ProblemTests(unittest.TestCase):
    def setUp(self):
        value = project()
        self.facts = h3_prompt.source(value, value["entities"][2], draft())

    def test_valid_prompt_passes(self):
        self.assertEqual(h3_prompt.problems(good(), self.facts), [])

    def test_refuses_chinese_outside_lines(self):
        found = h3_prompt.problems(good().replace("Wind", "风声"), self.facts)
        self.assertIn("Chinese text outside <d> lines", found)

    def test_refuses_a_lost_or_changed_line(self):
        found = h3_prompt.problems(good().replace("先问问", "问问"), self.facts)
        self.assertTrue(any("<d> line" in item for item in found))

    def test_refuses_unsent_and_uncited_labels(self):
        found = h3_prompt.problems(
            good().replace("<Picture 2>", "<Picture 3>"), self.facts
        )
        self.assertIn("<Picture 3> was not sent", found)
        self.assertIn("<Picture 2> is never cited", found)

    def test_refuses_a_line_without_its_speaker(self):
        found = h3_prompt.problems(good().replace("(S2) <d>", "<d>"), self.facts)
        self.assertTrue(any(item.startswith("write (S2)") for item in found))

    def test_refuses_a_loose_task_type(self):
        found = h3_prompt.problems(
            good().replace(
                "[reference generation + audio reference]",
                "[reference generation] + audio reference.",
            ),
            self.facts,
        )
        self.assertTrue(any(item.startswith("summary must") for item in found))

    def test_refuses_missing_section(self):
        found = h3_prompt.problems(
            good().replace("retention_analysis:", "retention:"), self.facts
        )
        self.assertTrue(found)


class SoundtrackTests(unittest.TestCase):
    def test_soundtrack_needs_audio_reuse_and_carries_timing(self):
        value = project()
        value["entities"].append(
            {
                "id": "track",
                "type": "audio",
                "title": "台词音轨",
                "description": "S1 0.5-1.6 S2 2.0-3.4",
                "data": {},
            }
        )
        shot_draft = draft()
        shot_draft["controls"]["audio_conditioning"] = "soundtrack"
        shot_draft["storyboard"]["references"][1]["entity_id"] = "track"
        facts = h3_prompt.source(value, value["entities"][2], shot_draft)
        self.assertTrue(facts["soundtrack"])
        self.assertEqual(
            facts["extra_references"][0]["timing"], "S1 0.5-1.6 S2 2.0-3.4"
        )
        found = h3_prompt.problems(good(), facts)
        self.assertTrue(any(item.startswith("summary must") for item in found))
        reused = good().replace("audio reference]", "audio reuse]")
        self.assertEqual(h3_prompt.problems(reused, facts), [])


class RewriteTests(unittest.TestCase):
    def setUp(self):
        h3_prompt._CACHE.clear()
        value = project()
        self.facts = h3_prompt.source(value, value["entities"][2], draft())

    def test_retries_once_with_the_problems_then_caches(self):
        client = FakeClient("bad 草地", good())
        self.assertEqual(h3_prompt.rewrite(client, self.facts), good())
        self.assertEqual(len(client.calls), 2)
        self.assertIn("Fix these problems", client.calls[1][-1]["text"])
        self.assertEqual(h3_prompt.rewrite(FakeClient(), self.facts), good())

    def test_blank_lines_and_fences_are_removed(self):
        client = FakeClient("```text" + NL + good().replace(NL, NL + NL) + NL + "```")
        self.assertEqual(h3_prompt.rewrite(client, self.facts), good())

    def test_gives_up_and_sends_the_original(self):
        client = FakeClient("x", "y")
        self.assertIsNone(h3_prompt.rewrite(client, self.facts))
        self.assertIsNone(h3_prompt.rewrite(None, self.facts))


class GateTests(unittest.TestCase):
    def admission(self, default):
        from types import SimpleNamespace

        from studio_platform.generation_admission import GenerationAdmission

        value = GenerationAdmission.__new__(GenerationAdmission)
        value.settings = SimpleNamespace(
            execution_backend="wangp-worker", default_deployment_profile_id=default
        )
        value.prompt_client = FakeClient(good())
        return value

    def test_story_without_a_profile_uses_the_h3_default(self):
        # Quality-tier stories reach preflight with no profile; staging skipped
        # every rewrite because of that (2026-11-08).
        h3_prompt._CACHE.clear()
        value = project()
        gate = self.admission("h3-pruned-rank8-int8-sage2-cu130-wgp9cb2-taomate5-v9")
        body = {"controls": {"duration": 6}}
        self.assertEqual(gate.model_prompt(value, "shot", draft(), body), good())
        h3_prompt._CACHE.clear()
        self.assertIsNone(
            self.admission("vivix-hosted").model_prompt(value, "shot", draft(), body)
        )
        windows = {"controls": {"script_window_prompts": ["a", "b"]}}
        self.assertIsNone(gate.model_prompt(value, "shot", draft(), windows))


def frame_draft(first=True, last=False):
    value = draft()
    value["inputs"].update(
        images=[],
        audios=[],
        first_frame={"asset_id": "f"} if first else None,
        last_frame={"asset_id": "l"} if last else None,
    )
    value["storyboard"]["prompt_parts"][:2] = [
        {"kind": "place", "entity_id": "pen", "text": "草地与石砌牛圈（海边草地）"},
        {
            "kind": "cast",
            "entity_id": "herd",
            "text": "三身牧牛人（三个头共用一个身体）",
        },
    ]
    value["storyboard"]["references"] = []
    return value


def frame_project():
    value = project()
    value["entities"][2]["data"]["storyboard"] = {"mode": "fl"}
    return value


def frame_good(last=False):
    ending = " The clip ends exactly on <Picture 2>." if last else ""
    return NL.join(
        [
            "integrated_multimodal_description:",
            "<Picture 1> is the first frame at 00:00.000; the clip continues from it."
            + ending,
            "A grounded cinematic style. The three-headed herdsman stands by the "
            "stone cattle pen at dusk. The left head (S1) shouts " + LINE1,
            "and closes its lips; the middle head (S2) hesitates " + LINE2,
            "and closes its lips.",
            "overall_soundscape:",
            "Wind and surf.",
            "non_diegetic_music:",
            "N/A",
        ]
    )


class FrameTests(unittest.TestCase):
    def setUp(self):
        h3_prompt._CACHE.clear()
        self.value = frame_project()
        self.shot = self.value["entities"][2]

    def test_first_frame_shot_gets_frame_facts_with_card_text(self):
        facts = h3_prompt.source(self.value, self.shot, frame_draft())
        self.assertEqual(facts["format"], "frames")
        self.assertEqual(facts["first_frame"], "<Picture 1>")
        self.assertIsNone(facts["last_frame"])
        self.assertEqual(facts["counts"], {"Picture": 1, "Audio": 0, "Video": 0})
        self.assertEqual(
            [(c["kind"], c["name"]) for c in facts["cards"]],
            [("place", "草地与石砌牛圈"), ("character", "三身牧牛人")],
        )
        self.assertIn("三个头共用一个身体", facts["cards"][1]["description"])
        both = h3_prompt.source(self.value, self.shot, frame_draft(last=True))
        self.assertEqual(
            (both["first_frame"], both["last_frame"]), ("<Picture 1>", "<Picture 2>")
        )
        last_only = h3_prompt.source(self.value, self.shot, frame_draft(False, True))
        self.assertEqual(last_only["last_frame"], "<Picture 1>")
        text_only = h3_prompt.source(self.value, self.shot, frame_draft(False))
        self.assertEqual(text_only["counts"]["Picture"], 0)

    def test_frame_shot_with_other_references_is_not_rewritten(self):
        extra = frame_draft()
        extra["inputs"]["audios"] = [{"asset_id": "c"}]
        self.assertIsNone(h3_prompt.source(self.value, self.shot, extra))
        written = dict(self.shot, data={**self.shot["data"], "prompt": frame_good()})
        self.assertIsNone(h3_prompt.source(self.value, written, frame_draft()))

    def test_frame_checks(self):
        facts = h3_prompt.source(self.value, self.shot, frame_draft())
        self.assertEqual(h3_prompt.problems(frame_good(), facts), [])
        named = frame_good().replace("The three-headed herdsman", "三身牧牛人")
        self.assertIn(
            "Chinese text outside <d> lines", h3_prompt.problems(named, facts)
        )
        self.assertTrue(
            any(
                item.startswith("must start with integrated")
                for item in h3_prompt.problems(good(), facts)
            )
        )
        self.assertIn(
            "<Picture 2> was not sent",
            h3_prompt.problems(frame_good(last=True), facts),
        )
        both = h3_prompt.source(self.value, self.shot, frame_draft(last=True))
        self.assertIn(
            "<Picture 2> is never cited", h3_prompt.problems(frame_good(), both)
        )
        self.assertEqual(h3_prompt.problems(frame_good(last=True), both), [])
        lost = frame_good().replace("先问问", "问问")
        self.assertTrue(any("<d> line" in i for i in h3_prompt.problems(lost, facts)))

    def test_latin_card_names_must_become_identities(self):
        self.value["entities"][0]["title"] = "Geryon"
        facts = h3_prompt.source(self.value, self.shot, frame_draft())
        kept = frame_good().replace("The three-headed herdsman", "Geryon")
        self.assertIn(
            "call Geryon by a generic English identity, not the name",
            h3_prompt.problems(kept, facts),
        )
        self.assertEqual(h3_prompt.problems(frame_good(), facts), [])

    def test_rewrite_uses_the_frame_instruction(self):
        facts = h3_prompt.source(self.value, self.shot, frame_draft())
        calls = []

        class Client(FakeClient):
            def generate(self, model, messages, **kwargs):
                calls.append(kwargs["instruction"])
                return super().generate(model, messages, **kwargs)

        client = Client(frame_good())
        self.assertEqual(h3_prompt.rewrite(client, facts), frame_good())
        self.assertEqual(calls, [h3_prompt.FRAME_INSTRUCTION])
        request = json.loads(client.calls[0][0]["text"])
        self.assertNotIn("format", request)
        self.assertNotIn("counts", request)
        self.assertEqual(request["first_frame"], "<Picture 1>")

    def test_gate_rewrites_frame_shots_for_h3(self):
        gate = GateTests.admission(
            None, "h3-pruned-rank8-int8-sage2-cu130-wgp9cb2-taomate5-v9"
        )
        gate.prompt_client = FakeClient(frame_good())
        body = {"controls": {"duration": 6}}
        self.assertEqual(
            gate.model_prompt(self.value, "shot", frame_draft(), body), frame_good()
        )

    def test_compiled_first_last_storyboard_shot_feeds_the_rewrite(self):
        from studio_platform.generation_draft import read_draft
        from test_platform_storyboard import cap, storyboard_project

        value = storyboard_project()
        shot = next(e for e in value["entities"] if e["id"] == "shot-one")
        shot["data"]["storyboard"]["mode"] = "fl"
        shot["data"]["h3"]["recipeId"] = "h3-base-fl2va-v1"
        compiled, issues = read_draft(value, "shot-one", capabilities=cap())
        self.assertEqual(issues, [])
        facts = h3_prompt.source(value, shot, compiled)
        self.assertEqual(
            (facts["first_frame"], facts["last_frame"]), ("<Picture 1>", "<Picture 2>")
        )
        self.assertEqual([c["name"] for c in facts["cards"]], ["站台", "林岚", "铁剑"])


class FramingTests(unittest.TestCase):
    def test_turbo_frame_goes_inside_detailed_description(self):
        v7 = "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7"
        framed = compiler.turbo_prompt(good())
        self.assertIn(
            "detailed_description:" + NL + compiler.FILM_FRAME + "A grounded", framed
        )
        self.assertIn(compiler.CLEAN_FRAME + NL + "overall_soundscape:", framed)
        self.assertTrue(framed.startswith("subject_definitions:"))
        self.assertEqual(compiler._authored_prompt(framed, v7), good())

    def test_turbo_frame_goes_inside_the_frame_format_body(self):
        v7 = "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7"
        framed = compiler.turbo_prompt(frame_good())
        self.assertIn(
            "integrated_multimodal_description:" + NL + compiler.FILM_FRAME, framed
        )
        self.assertIn(compiler.CLEAN_FRAME + NL + "overall_soundscape:", framed)
        self.assertEqual(compiler._authored_prompt(framed, v7), frame_good())


if __name__ == "__main__":
    unittest.main()
