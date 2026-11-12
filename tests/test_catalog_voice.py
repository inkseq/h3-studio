"""One of our own voices as a character's timbre sample; no provider, no charge."""

import unittest

from studio_platform.catalog_voice import _named, choice
from studio_platform.speech_catalog import LANGUAGES, VOICES
from studio_platform.catalog_voice import SAMPLES
from test_speech import set_switches
import test_platform_api as fixtures


class ChoiceTests(unittest.TestCase):
    def test_only_a_catalog_id_is_a_choice(self):
        self.assertEqual(choice("v-warm"), "v-warm")
        for value in ("george", "", None, 3, ["v-warm"], "V-WARM"):
            with self.subTest(value=value):
                self.assertIsNone(choice(value))

    def test_a_sample_is_owed_only_to_a_named_voice_with_nothing_to_send(self):
        def create(data):
            return {"op": "entity.create", "entity": {"data": data}}

        def update(data):
            return {"op": "entity.update", "patch": {"data": data}}

        self.assertIsNotNone(_named(create({"voiceId": "v-warm", "voices": []})))
        self.assertIsNotNone(_named(update({"voiceId": "v-warm"})))
        for action in (
            create({"voices": []}),
            create({"voiceId": "robot"}),
            create({"voiceId": "v-warm", "voices": [{"id": "own"}]}),
            update(None),
            {"op": "entity.update", "patch": {"title": "x"}},
            {"op": "project.update", "patch": {}},
        ):
            with self.subTest(action=action):
                self.assertIsNone(_named(action))

    def test_every_voice_has_a_sample_long_enough_to_clip(self):
        # Storyboard validation needs three seconds after clipStart.
        for voice in VOICES:
            for language in LANGUAGES:
                path = SAMPLES / f"{voice}-{language}.mp3"
                self.assertGreater(path.stat().st_size, 20_000, path.name)


class RouteTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login
    setup_project = fixtures.ApiTests.setup_project

    def prepare(self, voice=True):
        self.setup_project()
        current = self.client.get("/v1/projects/story-one").json()
        reply = self.client.post(
            "/v1/projects/story-one/actions",
            json={
                "expected_version": current["version"],
                "actions": [
                    {
                        "op": "entity.create",
                        "entity": {
                            "id": f"character-{name}",
                            "type": "character",
                            "parentId": None,
                            "title": name,
                            "description": "",
                            "data": {},
                        },
                    }
                    for name in ("mom", "son")
                ],
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        if voice:
            set_switches(self.app, voice_enabled=True)

    def give(self, character="character-mom", voice="v-steady", language="zh"):
        return self.client.post(
            f"/v1/projects/story-one/characters/{character}/catalog-voice",
            json={"voice_id": voice, "language": language},
        )

    def card(self, ident):
        project = self.client.get("/v1/projects/story-one").json()["project"]
        return next(e for e in project["entities"] if e["id"] == ident), project

    def test_the_sample_becomes_a_story_clip_and_the_main_voice(self):
        self.prepare()
        reply = self.give()
        self.assertEqual(reply.status_code, 200, reply.text)
        entry = reply.json()["voice"]
        mom, project = self.card("character-mom")
        self.assertEqual(mom["data"]["voices"], [entry])
        self.assertEqual(mom["data"]["mainVoiceId"], entry["id"])
        self.assertEqual(mom["data"]["voiceId"], "v-steady")
        clip = next(e for e in project["entities"] if e["id"] == entry["assetId"])
        self.assertEqual(clip["type"], "audio")
        self.assertEqual(clip["title"], VOICES["v-steady"]["name"]["zh"])

    def test_the_same_call_twice_adds_nothing(self):
        self.prepare()
        first = self.give().json()["voice"]
        version = self.client.get("/v1/projects/story-one").json()["version"]
        self.assertEqual(self.give().json()["voice"], first)
        after = self.client.get("/v1/projects/story-one").json()
        self.assertEqual(after["version"], version)
        assets = self.client.get(
            "/v1/assets", params={"client_project_id": "story-one"}
        ).json()["assets"]
        self.assertEqual(len(assets), 1)

    def test_two_characters_share_one_clip_and_a_second_voice_replaces_the_main(self):
        self.prepare()
        mom = self.give().json()["voice"]
        son = self.give("character-son").json()["voice"]
        self.assertEqual(mom["assetId"], son["assetId"])
        other = self.give(voice="v-velvet").json()["voice"]
        card, project = self.card("character-mom")
        self.assertEqual(card["data"]["mainVoiceId"], other["id"])
        self.assertEqual(card["data"]["voiceId"], "v-velvet")
        self.assertEqual(
            [v["id"] for v in card["data"]["voices"]], [mom["id"], other["id"]]
        )
        clips = [e for e in project["entities"] if e["type"] == "audio"]
        self.assertEqual(len(clips), 2)

    def test_refusals_write_nothing(self):
        self.prepare()
        for reply, status, code in (
            (self.give(voice="george"), 404, "voice_unknown"),
            (self.give(character="character-nobody"), 404, "voice_character_missing"),
        ):
            self.assertEqual(reply.status_code, status, reply.text)
            self.assertEqual(reply.json()["detail"]["code"], code)
        self.assertEqual(self.give(language="fr").status_code, 422)
        _, project = self.card("character-mom")
        self.assertFalse([e for e in project["entities"] if e["type"] == "audio"])

    def test_our_voices_stay_hidden_while_voice_is_off(self):
        self.prepare(voice=False)
        reply = self.give()
        self.assertEqual(reply.status_code, 409, reply.text)
        self.assertEqual(reply.json()["detail"]["code"], "voice_disabled")


if __name__ == "__main__":
    unittest.main()
