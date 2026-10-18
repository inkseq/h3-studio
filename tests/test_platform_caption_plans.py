"""Manual captions compile from the owned document; no GPU/font download/API."""

import copy
import json
from pathlib import Path
import subprocess
import unittest

from studio_platform.caption_server import (
    caption_signature,
    validate_normalized_subtitles,
)
from studio_platform.project_validation import validate_project
from studio_platform.render_plans import (
    compile_render,
    ordered_chapter,
    validate_render_source,
    configuration_for,
)
from test_platform_render_plans import story, source, BODY


class CaptionPlanTests(unittest.TestCase):
    def draft(self, cues=None):
        value = story()
        cues = cues or [
            {
                "id": "cue-one",
                "start": 0.11,
                "end": 1.93,
                "text": "欢迎来到雨夜。\n我们终于见面了。",
            }
        ]
        # Independent literal of the existing frontend confirmation wire object.
        snapshot = {
            "basis": [
                {
                    "id": "shot-one",
                    "start": 0,
                    "end": 2,
                    "asset": {"id": "clip", "fileId": "local-clip", "missing": False},
                }
            ],
            "soundMode": "silent",
            "audio": [],
            "cues": copy.deepcopy(cues),
        }
        value["journey"]["captionTracks"] = {
            "chapter-one": {
                "cues": cues,
                "confirmedSnapshot": snapshot,
                "confirmedAt": "2026-10-04T20:00:00+11:00",
            }
        }
        return value

    def burn(self, project):
        return compile_render({**BODY, "burn_subtitles": True}, project, source)

    def reconfirm(self, value):
        value["journey"]["captionTracks"]["chapter-one"]["confirmedSnapshot"] = (
            caption_signature(
                value, "chapter-one", ordered_chapter(value, "chapter-one")[2]
            )
        )

    def test_confirmed_chinese_silent_captions_use_inward_frames_and_preserve_text(
        self,
    ):
        value = self.draft()
        compiled, display, blockers, warnings = self.burn(value)
        self.assertEqual(blockers, [])
        render = compiled["request"]["render"]
        self.assertEqual(
            (render["version"], configuration_for(compiled)), (3, "cpu-render-v3")
        )
        cue = render["subtitles"]["cues"][0]
        self.assertEqual((cue["start_frame"], cue["end_frame"]), (3, 46))
        self.assertEqual(
            cue["text"],
            value["journey"]["captionTracks"]["chapter-one"]["cues"][0]["text"],
        )
        self.assertFalse(compiled["request"]["generate_audio"])
        self.assertTrue(display["subtitles"]["burned"])
        self.assertEqual(display["subtitles"]["cues"][0]["start"], 3 / 24)
        self.assertTrue(any("向内对齐" in item for item in warnings))
        self.assertTrue(any("永久烧进" in item for item in warnings))
        self.assertTrue(validate_render_source(value, compiled))

    def test_confirmation_matches_released_javascript_for_edit_and_audio(self):
        value = self.draft()
        value["entities"][2]["data"].update(
            seconds=1.99,
            selectedVideoRange={
                "assetId": "clip",
                "fileId": "local-clip",
                "cloudAssetId": "asset-one",
                "cloudArtifactId": None,
                "start": 1,
                "end": 4,
            },
        )
        value["journey"]["soundTracks"]["chapter-one"] = [
            {
                "id": "sound-one",
                "audioId": "missing-audio",
                "shotId": "shot-one",
                "start": 0.1,
                "end": 1,
                "muted": True,
                "offset": None,
                "generatedFrom": {
                    "jobId": "job-a",
                    "videoEntityId": "clip",
                    "videoArtifactId": "video-a",
                    "audioArtifactId": "audio-a",
                },
                "needsReview": True,
            }
        ]
        # The standalone repository/CI contains the verified source snapshot,
        # not the creator's sibling workspace. Snapshot integrity is gated
        # separately; parity must use the exact JavaScript shipped to users.
        module_path = (
            Path(__file__).resolve().parents[1] / "yingxu" / "src" / "caption-model.js"
        )
        self.assertTrue(module_path.is_file())
        module = module_path.as_uri()
        code = (
            "import{captionSignature}from"
            + json.dumps(module)
            + ";let raw='';for await(const part of process.stdin)raw+=part;console.log(JSON.stringify(captionSignature(JSON.parse(raw),'chapter-one')));"
        )
        child = subprocess.run(
            ["node", "--input-type=module", "-e", code],
            input=json.dumps(value),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
            timeout=15,
        )
        expected = json.loads(child.stdout)
        self.assertEqual(
            caption_signature(
                value, "chapter-one", ordered_chapter(value, "chapter-one")[2]
            ),
            expected,
        )

    def test_warning_numbers_follow_displayed_time_order_after_editor_reorders_array(
        self,
    ):
        value = self.draft(
            [
                {"id": "later", "start": 1.11, "end": 1.93, "text": "后一句"},
                {"id": "first", "start": 0.11, "end": 0.93, "text": "先一句"},
            ]
        )
        self.reconfirm(value)
        compiled, display, blockers, warnings = self.burn(value)
        self.assertFalse(blockers)
        self.assertEqual(
            [cue["id"] for cue in compiled["request"]["render"]["subtitles"]["cues"]],
            ["first", "later"],
        )
        alignment = [
            warning for warning in warnings if "向内对齐24fps，实际显示" in warning
        ]
        self.assertEqual(len(alignment), 2)
        for index, (warning, cue) in enumerate(
            zip(alignment, display["subtitles"]["cues"]), 1
        ):
            self.assertTrue(warning.startswith(f"第{index}条字幕："))
            self.assertIn(f"{cue['start']:.3f}–{cue['end']:.3f}秒", warning)
        self.assertEqual(
            value["journey"]["captionTracks"]["chapter-one"]["cues"][0]["id"], "later"
        )

    def test_current_confirmation_required_even_if_caller_claims_confirmed(self):
        for snapshot in (None, {}, {"confirmed": True}, {"basis": [], "cues": []}):
            value = self.draft()
            track = value["journey"]["captionTracks"]["chapter-one"]
            track["confirmedSnapshot"] = snapshot
            track["confirmed"] = True
            self.assertTrue(any("人工确认" in item for item in self.burn(value)[2]))
        for change in (
            lambda p: p["entities"][2]["data"].update(seconds=1),
            lambda p: p["journey"]["sound"].update(mode="music"),
            lambda p: p["journey"]["captionTracks"]["chapter-one"]["cues"][0].update(
                text="改过的台词"
            ),
        ):
            value = self.draft()
            change(value)
            self.assertTrue(self.burn(value)[2])

    def test_burn_source_changes_invalidate_plan_but_off_and_legacy_ignore_captions(
        self,
    ):
        value = self.draft()
        on = self.burn(value)[0]
        off = compile_render(BODY, value, source)[0]
        for version in (1, 2, 3):
            legacy = copy.deepcopy(off)
            legacy["request"]["render"]["version"] = version
            if version < 3:
                legacy["request"]["render"].pop("subtitles")
            self.assertTrue(validate_render_source(value, legacy))
            edited = copy.deepcopy(value)
            edited["journey"]["captionTracks"]["chapter-one"]["cues"][0]["text"] = (
                "修改正文"
            )
            self.assertTrue(validate_render_source(edited, legacy))
        for change in (
            lambda t: t["cues"][0].update(text="修改正文"),
            lambda t: t["cues"][0].update(start=0.3),
            lambda t: t.update(confirmedSnapshot=None),
            lambda t: t.update(style={"preset": "another"}),
        ):
            edited = copy.deepcopy(value)
            change(edited["journey"]["captionTracks"]["chapter-one"])
            self.assertFalse(validate_render_source(edited, on))
            self.assertTrue(validate_render_source(edited, off))
        self.assertIsNone(off["request"]["render"]["subtitles"])

    def test_unsafe_or_overlong_text_stays_in_draft_but_cannot_burn(self):
        for text in (
            "{\\p1}m 0 0",
            "quote\\Nnext",
            "<b>台词</b>",
            "tab\ttext",
            "null\x00text",
            "rtl\u202etext",
            "rain\rnight",
            "一\u2028二\u2028三",
            "一\u2029二",
            "雨" * 19,
            "一\n二\n三",
            "一\n  ",
        ):
            with self.subTest(text=text):
                value = self.draft()
                track = value["journey"]["captionTracks"]["chapter-one"]
                track["cues"][0]["text"] = text
                self.reconfirm(value)
                validate_project(value)
                compiled, _, blockers, _ = self.burn(value)
                self.assertTrue(blockers)
                self.assertEqual(track["cues"][0]["text"], text)
                self.assertIsNotNone(compiled["request"]["render"]["subtitles"])
                self.assertFalse(compile_render(BODY, value, source)[2])
        value = self.draft()
        value["journey"]["captionTracks"]["chapter-one"]["cues"][0]["text"] = (
            "雨" * 18 + "\r\n" + "夜" * 18
        )
        self.reconfirm(value)
        compiled, _, blockers, _ = self.burn(value)
        self.assertFalse(blockers)
        self.assertEqual(
            compiled["request"]["render"]["subtitles"]["cues"][0]["text"],
            "雨" * 18 + "\n" + "夜" * 18,
        )

    def test_invalid_subframe_overlap_and_out_of_chapter_not_silently_fixed(self):
        cases = (
            [{"id": "tiny", "start": 0.01, "end": 0.03, "text": "太短"}],
            [{"id": "past", "start": 1, "end": 2.01, "text": "越界"}],
            [
                {"id": "one", "start": 0, "end": 1, "text": "甲"},
                {"id": "two", "start": 0.9, "end": 2, "text": "乙"},
            ],
            [{"id": "one", "start": 1, "end": 0, "text": "倒置"}],
            [{"id": "one", "start": True, "end": 1, "text": "布尔"}],
        )
        for cues in cases:
            with self.subTest(cues=cues):
                value = self.draft(cues)
                self.assertTrue(self.burn(value)[2])
                self.assertEqual(
                    value["journey"]["captionTracks"]["chapter-one"]["cues"], cues
                )
        value = self.draft(
            [{"id": "one", "start": 1 / 24, "end": 2 / 24, "text": "一帧"}]
        )
        compiled, _, blockers, _ = self.burn(value)
        self.assertFalse(blockers)
        cue = compiled["request"]["render"]["subtitles"]["cues"][0]
        self.assertEqual((cue["start_frame"], cue["end_frame"]), (1, 2))

    def test_shared_normalized_contract_rejects_fields_types_preset_injection_and_excess(
        self,
    ):
        original = self.burn(self.draft())[0]["request"]["render"]["subtitles"]
        self.assertIs(validate_normalized_subtitles(original, 48), original)
        for change in (
            lambda v: v.update(font_path="file:///secret"),
            lambda v: v.update(preset="arbitrary"),
            lambda v: v["cues"][0].update(start_frame=True),
            lambda v: v["cues"][0].update(end_frame=49),
            lambda v: v["cues"][0].update(text="{\\pos(0,0)}注入"),
            lambda v: v["cues"].append(copy.deepcopy(v["cues"][0])),
            lambda v: v.update(cues=[]),
            lambda v: v.update(
                cues=[
                    {
                        "id": str(i),
                        "start_frame": i * 2,
                        "end_frame": i * 2 + 1,
                        "text": "字",
                    }
                    for i in range(501)
                ]
            ),
        ):
            value = copy.deepcopy(original)
            change(value)
            with self.assertRaises(ValueError):
                validate_normalized_subtitles(value, 48)
        for switch in (1, "true", None, [], {}):
            with self.assertRaises(ValueError):
                compile_render({**BODY, "burn_subtitles": switch}, self.draft(), source)
        for field in ("subtitles", "font_path", "cues", "filters", "confirmed"):
            with self.assertRaises(ValueError):
                compile_render({**BODY, field: "untrusted"}, self.draft(), source)


if __name__ == "__main__":
    unittest.main()
