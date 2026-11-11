"""Character voice design: brief, three previews, keep one; never a vendor name out."""

import json
import unittest
from unittest.mock import patch

import httpx

from studio_platform import voice_design
from studio_platform.voice_design import (
    PROFILE,
    FalVoiceDesign,
    VoiceDesignConfigError,
    VoiceDesignError,
    brief,
    validate,
)
from test_speech import (
    EDIT_SCOPES,
    SPEND_SCOPES,
    assert_paid_work_refused,
    connection_headers,
    key_headers,
    mp3,
    set_switches,
)
import test_platform_api as fixtures

KEY = "k" * 32


class FakeDesigner:
    def __init__(self):
        self.calls, self.fail = [], {}

    def design(self, description, text, language):
        self.calls.append((description, text, language))
        failure = self.fail.get(len(self.calls) - 1)
        if failure:
            raise failure
        return mp3(2)


class FakeChat:
    def __init__(self, text):
        self.text, self.calls = text, []

    def generate(self, model, messages, **kwargs):
        self.calls.append(json.loads(messages[-1]["text"]))
        return {"text": self.text}


class ConfigTests(unittest.TestCase):
    def test_only_the_inkseq_profile_with_a_plain_key_is_accepted(self):
        good = {"enabled": True, "fal": {"profile": PROFILE, "api_key": KEY}}
        self.assertEqual(validate(good).api_key, KEY)
        self.assertNotIn(KEY, repr(validate(good)))
        for bad in (
            {**good, "enabled": False},
            {"enabled": True, "fal": {"profile": "fal--sn117-miner", "api_key": KEY}},
            {"enabled": True, "fal": {"profile": PROFILE, "api_key": "short"}},
            {"enabled": True, "fal": {"profile": PROFILE, "api_key": KEY + " x"}},
            {**good, "extra": 1},
        ):
            with (
                self.subTest(bad=str(bad)[:60]),
                self.assertRaises(VoiceDesignConfigError),
            ):
                validate(bad)


class ProviderTests(unittest.TestCase):
    def designer(self, handler):
        return FalVoiceDesign(
            voice_design.Credential(KEY), transport=httpx.MockTransport(handler)
        )

    def test_designs_and_downloads_only_from_the_provider_media_host(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.host == "fal.run":
                body = json.loads(request.content)
                self.assertEqual(body["language"], "Chinese")
                self.assertEqual(request.headers["Authorization"], "Key " + KEY)
                return httpx.Response(
                    200, json={"audio": {"url": "https://v3.fal.media/files/a.mp3"}}
                )
            return httpx.Response(200, content=b"ID3audio")

        self.assertEqual(
            self.designer(handler).design("粗哑的男人", "回家吧。", "zh"), b"ID3audio"
        )
        self.assertEqual([r.url.host for r in seen], ["fal.run", "v3.fal.media"])

    def test_foreign_audio_host_refusal_and_outage_fail_closed(self):
        cases = (
            (
                lambda r: httpx.Response(
                    200, json={"audio": {"url": "https://evil.example/a.mp3"}}
                ),
                "voice_design_failed",
            ),
            (
                lambda r: httpx.Response(
                    422, text='{"type":"content_policy_violation"}'
                ),
                "voice_design_refused",
            ),
            (lambda r: httpx.Response(500), "voice_design_failed"),
        )
        for handler, code in cases:
            with self.subTest(code=code), self.assertRaises(VoiceDesignError) as raised:
                self.designer(handler).design("x", "y", "en")
            self.assertEqual(raised.exception.code, code)


class BriefTests(unittest.TestCase):
    card = {"title": "阿妈", "description": "七十岁的老母亲，说话很轻。"}

    def test_model_brief_is_cleaned_and_bounded(self):
        chat = FakeChat(
            '{"description": "  年迈女声，沙哑，  很轻很慢 ", "sample": "早点回来。"}'
        )
        self.assertEqual(
            brief(chat, self.card, "", "zh"),
            {"description": "年迈女声，沙哑， 很轻很慢", "sample": "早点回来。"},
        )
        self.assertEqual(chat.calls[0]["language"], "zh")

    def test_no_model_or_bad_output_falls_back_to_the_card_and_a_calm_line(self):
        for client in (None, FakeChat("not json"), FakeChat('{"description": ""}')):
            with self.subTest(client=type(client).__name__):
                value = brief(client, self.card, "", "en")
                self.assertIn("阿妈", value["description"])
                self.assertEqual(value["sample"], voice_design.SAMPLE["en"])


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
                            "id": "character-mom",
                            "type": "character",
                            "parentId": None,
                            "title": "阿妈",
                            "description": "七十岁的老母亲",
                            "data": {},
                        },
                    }
                ],
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.designer = FakeDesigner()
        self.app.state.voice_designer = self.designer
        if voice:
            set_switches(self.app, voice_enabled=True)

    def design(self, request_id="vd-000001", headers=None, **body):
        return self.client.post(
            "/v1/projects/story-one/voice-designs",
            json={
                "request_id": request_id,
                "character_id": "character-mom",
                "language": "zh",
                "description": "年迈女声，很轻",
                "sample": "早点回来。",
                **body,
            },
            headers=headers,
        )

    def test_a_design_needs_the_permission_a_generation_job_needs(self):
        self.prepare()
        organise = connection_headers(self.app, "organise")
        editor = key_headers(self.client, EDIT_SCOPES)
        from studio_platform import voice_design_routes

        with (
            patch.object(voice_design_routes, "DAILY_LIMIT", 3),
            patch.object(voice_design_routes, "brief") as drafted,
        ):
            # No words given: a refusal must come before the brief is drafted too.
            assert_paid_work_refused(
                self,
                self.design(headers=organise, description="", sample=""),
                self.design(headers=editor, description="", sample=""),
            )
            drafted.assert_not_called()
            self.assertEqual(self.designer.calls, [])
            assets = self.client.get(
                "/v1/assets", params={"client_project_id": "story-one"}
            )
            self.assertEqual(assets.json()["assets"], [])
            # Refusals took none of the day's three designs.
            for number, headers in enumerate(
                (
                    None,
                    connection_headers(self.app, "generate"),
                    key_headers(self.client, SPEND_SCOPES),
                )
            ):
                reply = self.design(f"vd-00010{number}", headers=headers)
                self.assertEqual(reply.status_code, 200, reply.text)
                self.assertEqual(len(reply.json()["previews"]), 3)
        self.assertEqual(len(self.designer.calls), 9)

    def test_voice_switch_off_designs_nothing(self):
        self.prepare(voice=False)
        reply = self.design()
        self.assertEqual(reply.json()["detail"]["code"], "voice_disabled")
        self.assertEqual(self.designer.calls, [])

    def test_three_previews_then_keep_one_as_story_audio(self):
        self.prepare()
        value = self.design().json()
        self.assertEqual(len(value["previews"]), 3)
        self.assertEqual(value["description"], "年迈女声，很轻")
        self.assertFalse(value["partial"])
        self.assertEqual({c[2] for c in self.designer.calls}, {"zh"})
        for vendor in ("fal.run", "fal-ai", "qwen"):
            self.assertNotIn(vendor, json.dumps(value).lower())
        chosen = value["previews"][1]
        kept = self.client.post(
            "/v1/projects/story-one/voice-designs/keep",
            json={
                "request_id": "vd-000001",
                "index": 1,
                "asset_id": chosen["asset_id"],
                "title": "阿妈的音色",
            },
        )
        self.assertEqual(kept.status_code, 200, kept.text)
        entity_id = kept.json()["entity_id"]
        project = self.client.get("/v1/projects/story-one").json()["project"]
        audio = next(e for e in project["entities"] if e["id"] == entity_id)
        self.assertEqual(audio["type"], "audio")
        # Keeping again is the same receipt, not a second clip.
        again = self.client.post(
            "/v1/projects/story-one/voice-designs/keep",
            json={
                "request_id": "vd-000001",
                "index": 1,
                "asset_id": chosen["asset_id"],
                "title": "阿妈的音色",
            },
        )
        self.assertEqual(again.json(), {"entity_id": entity_id})

    def test_keep_refuses_an_asset_from_another_request_or_index(self):
        self.prepare()
        value = self.design().json()
        for request_id, index in (("vd-000002", 0), ("vd-000001", 2)):
            with self.subTest(request_id=request_id, index=index):
                reply = self.client.post(
                    "/v1/projects/story-one/voice-designs/keep",
                    json={
                        "request_id": request_id,
                        "index": index,
                        "asset_id": value["previews"][0]["asset_id"],
                        "title": "x",
                    },
                )
                self.assertEqual(reply.status_code, 404)

    def test_partial_and_refused_designs(self):
        self.prepare()
        self.designer.fail = {0: VoiceDesignError("voice_design_failed")}
        value = self.design().json()
        self.assertEqual(len(value["previews"]), 2)
        self.assertTrue(value["partial"])
        refused = VoiceDesignError("voice_design_refused", 422)
        self.designer.calls, self.designer.fail = (
            [],
            {0: refused, 1: refused, 2: refused},
        )
        reply = self.design("vd-000003")
        self.assertEqual(reply.status_code, 422)
        self.assertEqual(reply.json()["detail"]["code"], "voice_design_refused")

    def test_daily_cap_and_unknown_character(self):
        self.prepare()
        from studio_platform import voice_design_routes

        original = voice_design_routes.DAILY_LIMIT
        voice_design_routes.DAILY_LIMIT = 1
        try:
            self.assertEqual(self.design().status_code, 200)
            capped = self.design("vd-000004")
            self.assertEqual(capped.status_code, 429)
        finally:
            voice_design_routes.DAILY_LIMIT = original
        missing = self.design("vd-000005", character_id="shot-one")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
