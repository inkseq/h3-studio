"""Deterministic shot readiness; pure, no model or database."""

import unittest

from studio_platform.shot_readiness import findings, project_readiness


def project(*shots, looks=None):
    return {
        "entities": [
            {
                "id": "mia",
                "type": "character",
                "title": "Mia",
                "data": {
                    "defaultLookId": "l1",
                    "looks": looks
                    if looks is not None
                    else [{"id": "l1", "imageIds": ["img"]}],
                },
            },
            *shots,
        ]
    }


def shot(prompt='Mia says "hello".', cast=("mia",), board=None, audio=None):
    data = {"prompt": prompt, "cast": list(cast), "storyboard": board or {}}
    if audio is not None:
        data["h3"] = {"controls": {"generate_audio": audio}}
    return {"id": "s1", "type": "shot", "version": 3, "data": data}


def codes(project_value, shot_value):
    return [item["code"] for item in findings(project_value, shot_value)]


class ShotReadinessTests(unittest.TestCase):
    def test_a_shot_with_a_picture_and_written_lines_is_ready(self):
        value = shot()
        self.assertEqual(codes(project(value), value), [])

    def test_a_recurring_person_needs_a_picture_or_first_frame(self):
        value = shot()
        bare = project(value, looks=[{"id": "l1", "imageIds": []}])
        self.assertEqual(
            findings(bare, value),
            [{"code": "character_picture_missing", "names": ["Mia"]}],
        )
        framed = shot(board={"firstFrameId": "f"})
        self.assertEqual(codes(project(framed, looks=[]), framed), [])
        continued = shot(board={"continuePrev": True})
        self.assertEqual(codes(project(continued, looks=[]), continued), [])

    def test_sound_needs_lines_or_an_explicit_no_dialogue(self):
        for prompt, expected in (
            ("Mia walks through the corn.", ["speech_unspecified"]),
            ("Mia walks through the corn, no dialogue.", []),
            ("米娅穿过玉米地，无对白。", []),
            ("米娅：我们走吧。", []),
            ("米娅说「走吧」。", []),
        ):
            with self.subTest(prompt=prompt):
                value = shot(prompt)
                self.assertEqual(codes(project(value), value), expected)
        muted = shot("Mia walks through the corn.", audio=False)
        self.assertEqual(codes(project(muted), muted), [])

    def test_empty_action_is_its_own_finding_not_a_speech_one(self):
        value = shot("  ")
        self.assertEqual(codes(project(value), value), ["action_missing"])

    def test_project_view_lists_every_shot_with_its_version(self):
        value = shot("Mia walks.")
        self.assertEqual(
            project_readiness(project(value)),
            {
                "items": [
                    {
                        "shot_id": "s1",
                        "base_version": 3,
                        "ready": False,
                        "findings": [{"code": "speech_unspecified"}],
                    }
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
