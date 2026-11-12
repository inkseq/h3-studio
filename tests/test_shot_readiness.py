"""Deterministic shot readiness; pure, no model or database."""

import unittest

from studio_platform.shot_readiness import findings, project_readiness


VOICE = {
    "voices": [{"id": "v1", "assetId": "clip", "clipStart": 0}],
    "mainVoiceId": "v1",
}


def project(*shots, looks=None, voice=VOICE, extra=()):
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
                    **(voice or {}),
                },
            },
            *extra,
            *shots,
        ]
    }


def person(ident, title, **data):
    looks = [{"id": "l1", "imageIds": ["img"]}]
    return {
        "id": ident,
        "type": "character",
        "title": title,
        "data": {"defaultLookId": "l1", "looks": looks, **data},
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
            ("左头大声呵斥 (S1) <d>[Chinese] 今天吃牛。</d>", []),
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


class VoiceMissingTests(unittest.TestCase):
    LINE = "Mia looks up (S1) <d>[English] You were here all along.</d>"

    def test_a_line_for_a_character_without_a_sample_is_reported_by_name(self):
        value = shot(self.LINE, board={"mode": "ref"})
        self.assertEqual(
            findings(project(value, voice=None), value),
            [{"code": "voice_missing", "names": ["Mia"]}],
        )
        self.assertEqual(codes(project(value), value), [])
        # A main voice id that names no sample sends nothing either.
        dangling = {"voices": [], "mainVoiceId": "gone"}
        self.assertEqual(
            codes(project(value, voice=dangling), value), ["voice_missing"]
        )
        # The look's own voice is what compile sends first.
        look = [{"id": "l1", "imageIds": ["img"], "voiceId": "v1"}]
        own = {
            "voices": [{"id": "v1", "assetId": "clip", "clipStart": 0}],
            "mainVoiceId": "",
        }
        self.assertEqual(codes(project(value, looks=look, voice=own), value), [])

    def test_nothing_is_reported_where_a_sample_would_not_be_sent_or_heard(self):
        for name, value in (
            ("first and last frame shot", shot(self.LINE, board={"mode": "fl"})),
            ("shot with no mode yet", shot(self.LINE)),
            (
                "finished dialogue track",
                shot(
                    self.LINE, board={"mode": "ref", "dialogueTrack": {"entityId": "t"}}
                ),
            ),
            ("sound off", shot(self.LINE, board={"mode": "ref"}, audio=False)),
            ("no line", shot("Mia walks, no dialogue.", board={"mode": "ref"})),
            ("quoted text only", shot('A sign reads "OPEN".', board={"mode": "ref"})),
        ):
            with self.subTest(name):
                self.assertNotIn(
                    "voice_missing", codes(project(value, voice=None), value)
                )

    def test_a_numbered_speaker_counts_only_when_its_number_speaks(self):
        zib = person("zib", "Zib", speaker="S2")
        tom = person("tom", "Tom")
        value = shot(self.LINE, cast=("mia", "zib", "tom"), board={"mode": "ref"})
        mia = {"speaker": "S1"}
        # Mia (S1) speaks; Zib (S2) does not; Tom has no number, so he may.
        self.assertEqual(
            findings(project(value, voice=mia, extra=(zib, tom)), value),
            [{"code": "voice_missing", "names": ["Mia", "Tom"]}],
        )
        untagged = shot(
            "Mia says <d>[English] Go.</d>", cast=("mia", "zib"), board={"mode": "ref"}
        )
        self.assertEqual(
            findings(project(untagged, voice=mia, extra=(zib,)), untagged),
            [{"code": "voice_missing", "names": ["Mia", "Zib"]}],
        )


if __name__ == "__main__":
    unittest.main()

    def test_a_fourth_voice_in_one_shot_is_reported_as_not_sent(self):
        def voiced(ident, title, clip):
            sample = {"id": "v", "assetId": clip, "clipStart": 0}
            return person(ident, title, voices=[sample], mainVoiceId="v")

        others = (
            voiced("zib", "Zib", "clip-z"),
            voiced("tom", "Tom", "clip-t"),
            voiced("ann", "Ann", "clip-a"),
            # The same clip as Mia's goes out once, so it takes no place.
            voiced("twin", "Twin", "clip"),
        )
        cast = ("mia", "twin", "zib", "tom", "ann")
        value = shot(self.LINE, cast=cast, board={"mode": "ref"})
        self.assertEqual(
            findings(project(value, extra=others), value),
            [{"code": "voice_dropped", "names": ["Ann"]}],
        )
        three = shot(self.LINE, cast=cast[:4], board={"mode": "ref"})
        self.assertEqual(codes(project(three, extra=others), three), [])
        track = {"mode": "ref", "dialogueTrack": {"entityId": "t"}}
        kept = shot(self.LINE, cast=cast, board=track)
        self.assertEqual(codes(project(kept, extra=others), kept), [])
