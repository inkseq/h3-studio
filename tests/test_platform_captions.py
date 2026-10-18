"""Caption drafts survive timeline changes; invalid structures remain bounded."""
import copy
import unittest

from studio_platform.project_validation import validate_project
from test_platform_api import project


class CaptionDraftTests(unittest.TestCase):
    def value(self):
        value = project()
        cue = {"id": "cue-one", "start": 0.125, "end": 4.5, "text": "欢迎来到雨夜。"}
        value["journey"] = {"captionTracks": {"chapter-one": {"cues": [cue],
            "confirmedSnapshot": {"basis": [{"id": "shot-one", "start": 0, "end": 5, "asset": None}],
                                  "cues": [copy.deepcopy(cue)]},
            "confirmedAt": "2026-10-04T06:00:00+11:00"}}}
        return value

    def test_timeline_shortening_preserves_historical_confirmation_and_draft(self):
        value = self.value()
        value["entities"][2]["data"]["seconds"] = 2
        track = value["journey"]["captionTracks"]["chapter-one"]
        track["cues"].append({"id": "cue-two", "start": 1, "end": 4, "text": "待修复重叠"})
        self.assertIs(validate_project(value), value)

    def test_invalid_types_unknown_chapter_duplicate_and_excessive_fields_rejected(self):
        for change in (
            lambda t: t["cues"][0].update(start=True),
            lambda t: t["cues"][0].update(end=float("nan")),
            lambda t: t["cues"][0].update(text="x"*2001),
            lambda t: t["cues"].append(copy.deepcopy(t["cues"][0])),
            lambda t: t.update(confirmedSnapshot="untrusted-string"),
            lambda t: t.update(confirmedAt=1234),
            lambda t: t.update(cues=[{"id": f"cue-{n}", "start": 0, "end": 1, "text": "x"*2000} for n in range(51)]),
        ):
            value = self.value()
            change(value["journey"]["captionTracks"]["chapter-one"])
            with self.assertRaises(ValueError):
                validate_project(value)
        value = self.value()
        value["journey"]["captionTracks"]["missing-chapter"] = value["journey"]["captionTracks"].pop("chapter-one")
        with self.assertRaises(ValueError):
            validate_project(value)

    def test_old_projects_without_captions_are_unchanged(self):
        value = project()
        self.assertIs(validate_project(value), value)


if __name__ == "__main__":
    unittest.main()
