"""Voices and generated lines (#997) with a fake provider; no network, no credits."""

import json
import secrets
import unittest
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

import httpx
from sqlalchemy import insert

from studio_platform import speech_provider, speech_settings
from studio_platform.auth import digest
from studio_platform.repository import documents
from studio_platform.speech_catalog import (
    AGES,
    GENDERS,
    LANGUAGES,
    RETIRED,
    VOICES,
    maker,
    public,
    spoken,
)
from studio_platform.speech_provider import (
    OpenRouterSpeech,
    Speech,
    SpeechConfigError,
    SpeechError,
    validate,
)
from studio_platform.speech_routes import clean
import test_platform_api as fixtures


def mp3(seconds=1):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "line.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:duration={seconds}",
                str(path),
            ],
            check=True,
        )
        return path.read_bytes()


class FakeAdapter:
    NAME = "openrouter"

    def __init__(self):
        self.calls, self.fail = [], None

    def synthesize(self, text, voice, direction, language):
        self.calls.append((text, voice["provider_voice"], direction, language))
        if self.fail:
            raise self.fail
        return Speech(mp3(), len(text))


def set_switches(app, **value):
    repo = app.state.repository
    with repo.transaction() as connection:
        connection.execute(
            insert(documents).values(
                tenant_id=app.state.settings.tenant_id,
                owner_id=speech_settings.OWNER,
                project_id=speech_settings.PROJECT,
                kind=speech_settings.KIND,
                document_id=speech_settings.ID,
                payload={**speech_settings.DEFAULT, **value},
                version=1,
                updated_at=repo.clock(),
            )
        )


EDIT_SCOPES = ["projects:read", "projects:write"]
SPEND_SCOPES = [*EDIT_SCOPES, "assets:read", "assets:write", "jobs:write"]


def key_headers(client, scopes):
    """A synthetic API key with these scopes, minted by the signed-in client."""
    reply = client.post(
        "/v1/api-keys",
        json={
            "name": "Synthetic key",
            "scopes": scopes,
            "all_projects": True,
            "project_ids": [],
            "expires_in_days": 1,
        },
    )
    reply.raise_for_status()
    return {"Authorization": "Bearer " + reply.json()["api_key"]}


def connection_headers(app, permission):
    """A synthetic agent connection over all of the owner's stories."""
    auth, service = app.state.auth, app.state.agent_connect
    origin = "http://127.0.0.1:8850"
    grant = service.issue(
        auth.session(auth.login("superdan", "", source="test")),
        name="Synthetic assistant",
        authorization_profile_id="creator-full",
        authorization_profile_version=1,
        idempotency_key=secrets.token_hex(12),
        origin=origin,
        scope={"kind": "all"},
        permission=permission,
    )
    token = "sxp_" + secrets.token_urlsafe(32)
    service.exchange(
        code=grant["code"],
        client_challenge=digest(secrets.token_urlsafe(32)),
        token_hash=digest(token),
        key_prefix=token[:12],
        expected_authorization_fingerprint=grant["connection"][
            "authorization_fingerprint"
        ],
        origin=origin,
        source="synthetic",
    )
    return {"Authorization": "Bearer " + token}


def assert_paid_work_refused(case, organise, editor):
    """The refusals a generation job gives the same two callers."""
    case.assertEqual(organise.status_code, 409, organise.text)
    case.assertEqual(organise.json()["code"], "connection_permission_denied")
    case.assertEqual(set(organise.json()["messages"]), {"en", "zh"})
    case.assertEqual(editor.status_code, 404, editor.text)
    case.assertEqual(set(editor.json()), {"detail"})


class SpeechRouteTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login
    setup_project = fixtures.ApiTests.setup_project

    def prepare(self, **switches):
        self.setup_project()
        self.adapter = FakeAdapter()
        self.app.state.speech_adapters = {"openrouter": self.adapter}
        if switches:
            set_switches(self.app, **switches)

    def speak(self, request_id="line-0001", headers=None, **body):
        return self.client.post(
            "/v1/projects/story-one/speech",
            json={
                "request_id": request_id,
                "text": "末班车还有十分钟就开了。",
                "voice_id": "v-warm",
                **body,
            },
            headers=headers,
        )

    def test_a_line_needs_the_permission_a_generation_job_needs(self):
        self.prepare(voice_enabled=True)
        organise = connection_headers(self.app, "organise")
        editor = key_headers(self.client, EDIT_SCOPES)
        assert_paid_work_refused(
            self, self.speak(headers=organise), self.speak(headers=editor)
        )
        # The same key is refused the same way where a generation is planned.
        planned = self.client.post(
            "/v1/generation-plans", json=fixtures.generation_request(), headers=editor
        )
        self.assertEqual(
            (planned.status_code, planned.json()),
            (404, self.speak(headers=editor).json()),
        )
        self.assertEqual(self.adapter.calls, [])
        project = self.client.get("/v1/projects/story-one").json()["project"]
        self.assertNotIn("speechRequests", project.get("journey", {}))
        self.assertFalse([e for e in project["entities"] if e["type"] == "audio"])
        # Pricing a line spends nothing, so both may still ask.
        for headers in (organise, editor):
            quoted = self.client.post(
                "/v1/projects/story-one/speech/quote",
                json={"request_id": "line-0001", "text": "x", "voice_id": "v-warm"},
                headers=headers,
            )
            self.assertEqual(quoted.status_code, 200, quoted.text)
        # A refusal uses nothing up: the same request then works for a caller who may spend.
        self.assertEqual(self.speak().status_code, 200)
        for number, headers in enumerate(
            (
                connection_headers(self.app, "generate"),
                key_headers(self.client, SPEND_SCOPES),
            )
        ):
            reply = self.speak(f"line-100{number}", headers=headers)
            self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(len(self.adapter.calls), 3)

    def test_default_off_shows_nothing_and_calls_nothing(self):
        self.prepare()
        self.assertEqual(
            self.client.get("/v1/voices").json(), {"enabled": False, "voices": []}
        )
        reply = self.speak()
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(reply.json()["detail"]["code"], "voice_disabled")
        self.assertEqual(self.adapter.calls, [])

    def test_a_voice_revision_always_names_the_same_vendor_voice(self):
        # Clips store only (voice id, revision). If a vendor field changes here without
        # a new revision, every old clip would quietly claim the wrong maker.
        pinned = {
            ("v-steady", "r1"): "sarah",
            ("v-velvet", "r1"): "lily",
            ("v-bright", "r1"): "jessica",
            ("v-clear", "r1"): "alice",
            ("v-warm", "r1"): "george",
            ("v-deep", "r1"): "brian",
            ("v-anchor", "r1"): "daniel",
            ("v-young", "r1"): "liam",
            ("v-sassy", "r1"): "laura",
            ("v-easy", "r1"): "roger",
            ("v-chill", "r1"): "will",
            ("v-casual", "r1"): "chris",
        }
        for (ident, revision), vendor_voice in pinned.items():
            self.assertEqual(
                maker(ident, revision),
                {
                    "provider": "openrouter",
                    "model": "elevenlabs/eleven-v4",
                    "provider_voice": vendor_voice,
                },
            )
        for ident, voice in VOICES.items():
            if spoken(ident) is None:
                # A designed voice is a sample only: no vendor, no revision, never a maker.
                self.assertFalse(
                    {"provider", "model", "provider_voice", "revision"} & set(voice),
                    ident,
                )
                self.assertIsNone(maker(ident, "r1"))
                continue
            self.assertIn((ident, voice["revision"]), pinned, ident)
            self.assertNotIn((ident, voice["revision"]), RETIRED, ident)
        self.assertIsNone(maker("v-warm", "r0"))
        self.assertIsNone(maker("v-none", "r1"))

    def test_catalog_never_names_a_vendor(self):
        self.prepare(voice_enabled=True)
        reply = self.client.get("/v1/voices")
        self.assertTrue(reply.json()["enabled"])
        raw = reply.text.lower()
        for word in ("eleven", "openrouter", "provider", "model"):
            self.assertNotIn(word, raw)
        for voice in VOICES.values():
            if "provider_voice" in voice:
                self.assertNotIn('"' + voice["provider_voice"] + '"', raw)

    def test_a_line_becomes_an_asset_that_records_what_made_it(self):
        self.prepare(voice_enabled=True)
        reply = self.speak(direction="angry", language="zh")
        self.assertEqual(reply.status_code, 200, reply.text)
        value = reply.json()
        self.assertEqual(value["voice_id"], "v-warm")
        self.assertEqual(self.adapter.calls[0][1:], ("george", "angry", "zh"))
        project = self.client.get("/v1/projects/story-one").json()
        project = project.get("project", project)
        entity = next(e for e in project["entities"] if e["id"] == value["entity_id"])
        self.assertEqual(entity["type"], "audio")
        made = entity["data"]["speech"]
        self.assertEqual((made["voice_id"], made["voice_revision"]), ("v-warm", "r1"))
        self.assertEqual(maker("v-warm", "r1")["provider_voice"], "george")
        # A project read never names the vendor, its model or the vendor voice (#1097).
        raw = self.client.get("/v1/projects/story-one").text.lower()
        for word in ("openrouter", "eleven", '"george"', "provider_voice"):
            self.assertNotIn(word, raw)
        self.assertEqual(len(made["text_sha256"]), 64)
        self.assertEqual(self.client.get(value["content_path"]).status_code, 200)
        # Same request again: the same clip, with no second provider call.
        again = self.speak(direction="angry", language="zh")
        self.assertEqual(again.json(), value)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_failure_does_not_change_voice_or_attach(self):
        self.prepare(voice_enabled=True)
        self.adapter.fail = SpeechError("speech_provider_failed")
        reply = self.speak()
        self.assertEqual(reply.status_code, 502)
        self.assertEqual(reply.json()["detail"]["code"], "speech_provider_failed")
        self.assertTrue(reply.json()["detail"]["nothing_charged"])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_charging_without_an_account_never_calls_the_provider(self):
        self.prepare(voice_enabled=True)
        with patch("studio_platform.credits.credit_policy") as policy:
            policy.return_value = {"enabled": True}
            reply = self.speak()
        self.assertEqual(reply.status_code, 403)
        self.assertEqual(reply.json()["detail"]["code"], "credit_account_unavailable")
        self.assertEqual(self.adapter.calls, [])

    def test_free_while_testing_passes_charging(self):
        self.prepare(voice_enabled=True, free_while_testing=True)
        with patch("studio_platform.credits.credit_policy") as policy:
            policy.return_value = {"enabled": True}
            reply = self.speak()
        self.assertEqual(reply.status_code, 200, reply.text)

    def test_bad_requests(self):
        self.prepare(voice_enabled=True)
        self.assertEqual(self.speak(voice_id="sarah").status_code, 422)
        # A designed voice is a timbre sample; it cannot say a line.
        self.assertEqual(self.speak(voice_id="vf-gentle").status_code, 422)
        self.assertEqual(self.speak(direction="shouting").status_code, 422)
        self.assertEqual(self.speak(language="fr").status_code, 422)
        self.assertEqual(self.speak(request_id="bad id !!").status_code, 422)
        self.assertEqual(self.speak(text="x" * 501).status_code, 422)
        self.assertEqual(self.speak(text="[ ]").status_code, 422)
        self.assertEqual(self.adapter.calls, [])

    def test_another_owner_cannot_speak_into_a_story(self):
        self.prepare(voice_enabled=True)
        reply = self.client.post(
            "/v1/projects/no-such-story/speech",
            json={"request_id": "line-0001", "text": "x", "voice_id": "v-warm"},
        )
        self.assertEqual(reply.status_code, 404)

    def test_every_voice_has_its_samples(self):
        self.prepare()
        for entry in public():
            for path in entry["samples"].values():
                reply = self.client.get(path)
                self.assertEqual(reply.status_code, 200, path)
                self.assertEqual(reply.headers["content-type"], "audio/mpeg")
        for path in ("/v1/voices/sarah/sample/zh", "/v1/voices/v-warm/sample/fr"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_admin_settings_require_an_admin(self):
        self.prepare()
        with patch("studio_platform.identity.is_admin", return_value=False):
            reply = self.client.get("/v1/admin/speech/settings")
        self.assertEqual(reply.status_code, 403)


class SpeechSettingsTests(unittest.TestCase):
    def test_free_is_refused_outside_staging(self):
        with self.assertRaises(speech_settings.SpeechSettingsError) as caught:
            speech_settings.write(
                None,
                "tenant",
                None,
                {
                    "expected_version": 0,
                    "voice_enabled": True,
                    "free_while_testing": True,
                },
                0,
                deployment_target="production",
            )
        self.assertEqual(caught.exception.code, "speech_free_staging_only")

    def test_extra_fields_are_refused(self):
        with self.assertRaises(speech_settings.SpeechSettingsError):
            speech_settings.write(
                None,
                "tenant",
                None,
                {"expected_version": 0, "voice_enabled": True},
                0,
                deployment_target="staging",
            )


class AdapterTests(unittest.TestCase):
    def test_direction_is_written_as_tags_and_the_body_is_exact(self):
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content))
            seen["auth"] = request.headers["authorization"]
            seen["url"] = str(request.url)
            return httpx.Response(200, content=b"audio")

        adapter = OpenRouterSpeech(
            speech_provider.Credential("k" * 20), httpx.MockTransport(handler)
        )
        result = adapter.synthesize("你好", VOICES["v-warm"], "angry", "zh")
        self.assertEqual(result.data, b"audio")
        self.assertEqual(seen["url"], speech_provider.ORIGIN + "/audio/speech")
        self.assertEqual(seen["input"], "[furious, shouting] 你好")
        self.assertEqual(seen["voice"], "george")
        self.assertEqual(seen["model"], "elevenlabs/eleven-v4")

    def test_every_direction_has_a_tag(self):
        self.assertEqual(set(OpenRouterSpeech.STYLE), set(speech_provider.DIRECTIONS))

    def test_errors_carry_no_upstream_text(self):
        adapter = OpenRouterSpeech(
            speech_provider.Credential("k" * 20),
            httpx.MockTransport(lambda _: httpx.Response(500, text="secret detail")),
        )
        with self.assertRaises(SpeechError) as caught:
            adapter.synthesize("x", VOICES["v-warm"], "calm", "en")
        self.assertEqual(str(caught.exception), "speech_provider_failed")

    def test_config_is_closed(self):
        good = {
            "enabled": True,
            "openrouter": {
                "profile": speech_provider.PROFILE,
                "base_url": speech_provider.ORIGIN,
                "api_key": "k" * 20,
            },
        }
        self.assertEqual(validate(good).api_key, "k" * 20)
        self.assertNotIn("k" * 20, repr(validate(good)))
        for broken in (
            {"enabled": False},
            {**good, "extra": 1},
            {**good, "openrouter": {**good["openrouter"], "base_url": "https://x"}},
            {**good, "openrouter": {**good["openrouter"], "profile": "other"}},
            {**good, "openrouter": {**good["openrouter"], "api_key": "short"}},
        ):
            with self.assertRaises(SpeechConfigError):
                validate(broken)

    def test_catalog_names_each_voice_in_both_languages(self):
        for entry in public():
            self.assertEqual(set(entry["name"]), {"zh", "en"})
            self.assertEqual(set(entry["samples"]), set(LANGUAGES))
            self.assertIn(entry["gender"], GENDERS)
            self.assertIn(entry["age"], AGES)
            self.assertEqual(entry["speaks"], spoken(entry["id"]) is not None)
        shown = [(e["gender"], e["age"], e["name"]["zh"]) for e in public()]
        # Within one gender and age a name is the only thing that tells two voices apart.
        self.assertEqual(len(shown), len(set(shown)))

    def test_the_assistant_is_told_every_voice_under_its_gender_and_age(self):
        from studio_platform.story_chat import INSTRUCTION

        start = INSTRUCTION.index('("voice":')
        block = " ".join(
            INSTRUCTION[
                start : INSTRUCTION.index(")", INSTRUCTION.index("women ", start))
            ].split()
        )
        told = {}
        women, men = block[block.index("women ") + 6 :].split(" men ")
        for gender, part in (("female", women), ("male", men)):
            for group in part.split(";"):
                age, ids = group.split(":")
                for ident in ids.split(","):
                    told[ident.strip()] = (gender, age.strip())
        self.assertEqual(
            told,
            {ident: (voice["gender"], voice["age"]) for ident, voice in VOICES.items()},
        )

    def test_a_line_cannot_steer_its_own_delivery(self):
        self.assertEqual(clean("[shouting] 你好\n再见"), "shouting 你好 再见")


if __name__ == "__main__":
    unittest.main()
