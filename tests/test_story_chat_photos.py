"""Photos attached in story chat (#726): the model sees them, pictures can be drawn from them.

Fake Gemini text and image clients; no network, no GPU, no credits.
"""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from studio_platform.api import create_app
from studio_platform.google_chat import MAX_IMAGES, ChatError, payload
from studio_platform.settings import Settings
from studio_platform.story_chat_images import input_hash, prompt_for
from test_story_chat import CAST, ScriptedModel
from test_story_chat_images import FakeImages, set_pictures


def png(color=(220, 140, 60)):
    raw = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(raw, format="PNG")
    return raw.getvalue()


class PayloadTests(unittest.TestCase):
    def picture(self):
        return {"mime": "image/jpeg", "data": b"\xff\xd8jpeg"}

    def test_pictures_follow_the_latest_message_text(self):
        messages = [{"role": "user", "text": "x", "images": [self.picture()]}]
        body = payload("gemini-3.8-flash", messages, "")
        parts = body["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "x"})
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "image/jpeg")

    def test_refuses_pictures_in_history_too_many_or_unbounded(self):
        history = [
            {"role": "user", "text": "a", "images": [self.picture()]},
            {"role": "model", "text": "b"},
            {"role": "user", "text": "c"},
        ]
        bad = [
            history,
            [
                {
                    "role": "user",
                    "text": "x",
                    "images": [self.picture()] * (MAX_IMAGES + 1),
                }
            ],
            [{"role": "user", "text": "x", "images": []}],
            [
                {
                    "role": "user",
                    "text": "x",
                    "images": [{"mime": "image/gif", "data": b"x"}],
                }
            ],
            [
                {
                    "role": "user",
                    "text": "x",
                    "images": [{"mime": "image/jpeg", "data": "x"}],
                }
            ],
        ]
        for messages in bad:
            with self.subTest(messages=len(messages)):
                with self.assertRaises(ChatError) as caught:
                    payload("gemini-3.8-flash", messages, "")
                self.assertEqual(caught.exception.code, "invalid_images")
        with self.assertRaises(ChatError):
            payload(
                "gemma-4-31b-it",
                [{"role": "user", "text": "x", "images": [self.picture()]}],
                "",
            )


class PhotoTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = Settings(
            Path(self.tmp.name), auth_mode="local-test", story_chat_enabled=True
        )
        self.model, self.images = ScriptedModel(), FakeImages()
        self.app = create_app(
            settings, story_chat_client=self.model, story_chat_image_client=self.images
        )
        self.addCleanup(self.app.state.repository.engine.dispose)
        set_pictures(self.app, True)
        self.client = TestClient(self.app)
        self.assertEqual(
            self.client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).status_code,
            200,
        )
        chat = self.client.post("/v1/story-chats", json={"language": "en"}).json()
        self.pid = chat["project_id"]
        self.model.plans.append(CAST)
        self.story = self.turn("aliens", "turn-0001").json()

    def turn(self, text, turn_id, plan=None, **extra):
        if plan is not None:
            self.model.plans.append(plan)
        return self.client.post(
            f"/v1/story-chats/{self.pid}/turns",
            json={"turn_id": turn_id, "text": text, **extra},
        )

    def add_photo(self, name="scarf.png", color=(220, 140, 60)):
        asset = self.client.post(
            "/v1/assets",
            data={"client_project_id": self.pid},
            files={"file": (name, png(color), "image/png")},
        )
        self.assertEqual(asset.status_code, 201, asset.text)
        asset_id = asset.json()["asset_id"]
        current = self.client.get(f"/v1/projects/{self.pid}").json()
        attached = self.client.post(
            f"/v1/projects/{self.pid}/actions",
            json={
                "expected_version": current["version"],
                "actions": [
                    {
                        "op": "asset.attach",
                        "asset_id": asset_id,
                        "entity_id": "asset-" + asset_id,
                        "title": name,
                    }
                ],
            },
        )
        self.assertEqual(attached.status_code, 200, attached.text)
        return "asset-" + asset_id

    def project(self):
        return self.client.get(f"/v1/projects/{self.pid}").json()

    def test_the_model_sees_attached_photos_and_the_turn_keeps_their_ids(self):
        first, second = self.add_photo(), self.add_photo("cafe.png", (20, 90, 160))
        reply = {"reply": "A warm orange scarf and a blue cafe.", "actions": []}
        response = self.turn(
            "what do you see?", "turn-0002", reply, photos=[first, second]
        )
        self.assertEqual(response.status_code, 200, response.text)
        sent = self.model.calls[-1]["messages"][-1]
        self.assertEqual(len(sent["images"]), 2)
        self.assertTrue(all(item["mime"] == "image/jpeg" for item in sent["images"]))
        self.assertIn("Image 1 = photo id " + first + " (scarf.png)", sent["text"])
        self.assertIn("Image 2 = photo id " + second + " (cafe.png)", sent["text"])
        # Earlier turns never carry pictures.
        self.assertTrue(
            all("images" not in m for m in self.model.calls[-1]["messages"][:-1])
        )
        user = [m for m in response.json()["messages"] if m["role"] == "user"][-1]
        self.assertEqual(user["photos"], [first, second])
        self.assertEqual(user["text"], "what do you see?")
        # The bubble can show what was sent, also after a reload (inkseq-design#457).
        files = [
            {"id": ident, "name": name, "url": f"/v1/assets/{ident[6:]}/content"}
            for ident, name in ((first, "scarf.png"), (second, "cafe.png"))
        ]
        self.assertEqual(user["photo_files"], files)
        reloaded = self.client.get(f"/v1/story-chats/{self.pid}").json()["messages"]
        self.assertEqual(
            [m.get("photo_files") for m in reloaded if m["role"] == "user"][-1], files
        )
        self.assertTrue(all("photo_files" not in m for m in reloaded[:2]))

    def test_streamed_photo_turn_calls_model_once_and_replays_stored_result(self):
        from test_story_chat_stream import StreamingModel, events

        photo = self.add_photo()
        self.model.plans.append({"reply": "An orange scarf.", "actions": []})
        body = {
            "turn_id": "turn-photo-stream",
            "text": "Describe it",
            "photos": [photo],
        }
        before = len(self.model.calls)
        with patch.object(
            self.model,
            "generate_stream",
            StreamingModel.generate_stream.__get__(self.model),
            create=True,
        ):
            response = self.client.post(
                f"/v1/story-chats/{self.pid}/turns/stream", json=body
            )
        self.assertEqual(response.status_code, 200, response.text)
        emitted = events(response.text)
        self.assertIn(("reply", {"text": "An orange scarf."}), emitted)
        self.assertEqual(emitted[-1][0], "done")
        self.assertEqual(len(self.model.calls), before + 1)
        images = self.model.calls[-1]["messages"][-1]["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mime"], "image/jpeg")
        replay = self.client.post(f"/v1/story-chats/{self.pid}/turns", json=body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["turn"], emitted[-1][1]["turn"])
        self.assertEqual(len(self.model.calls), before + 1)

    def test_unknown_unreadable_or_inky_photos_are_refused_before_the_model(self):
        before = self.project()["version"]
        calls = len(self.model.calls)
        missing = self.turn("look", "turn-0003", photos=["asset-nope"])
        self.assertEqual(missing.status_code, 409, missing.text)
        self.assertEqual(missing.json()["detail"]["code"], "story_chat_photo_missing")
        zib = self.story["cards"]["characters"][0]["id"]
        not_image = self.turn("look", "turn-0004", photos=[zib])
        self.assertEqual(not_image.status_code, 409, not_image.text)
        too_many = self.turn("look", "turn-0005", photos=["a", "b", "c", "d", "e"])
        self.assertEqual(too_many.status_code, 422, too_many.text)
        photo = self.add_photo()
        before = self.project()["version"]
        with patch(
            "studio_platform.inky_uploads.preview",
            side_effect=ValueError("inky_image_unavailable"),
        ):
            unreadable = self.turn("look", "turn-0006", photos=[photo])
        self.assertEqual(unreadable.status_code, 422, unreadable.text)
        self.assertEqual(
            unreadable.json()["detail"]["code"], "story_chat_photo_unavailable"
        )
        inky = self.turn("look", "turn-0007", photos=[photo], inky={"shot_id": None})
        self.assertEqual(inky.status_code, 422, inky.text)
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(self.project()["version"], before)

    def test_a_picture_can_be_drawn_from_attached_photos(self):
        photo = self.add_photo()
        mia = next(c for c in self.story["cards"]["characters"] if c["name"] == "Mia")[
            "id"
        ]
        plan = {
            "reply": "I can draw Mia wearing your scarf; review and confirm it.",
            "actions": [{"tool": "propose_image", "ref": mia, "photos": [photo]}],
        }
        response = self.turn("put my scarf on Mia", "turn-0002", plan, photos=[photo])
        self.assertEqual(response.status_code, 200, response.text)
        proposal = response.json()["turn"]["proposal"]
        self.assertEqual(proposal["picture_card_ids"], [mia])
        self.assertEqual(proposal["picture_sources"], {mia: [photo]})

        drawn = self.client.post(
            f"/v1/story-chats/{self.pid}/images",
            json={"request_id": "pic-00000009", "card_id": mia, "photo_ids": [photo]},
        )
        self.assertEqual(drawn.status_code, 200, drawn.text)
        prompt, _aspect = self.images.calls[-1]
        self.assertIn("is a photo the person added", prompt)
        # Earlier card pictures (the style reference) come first, the photo last.
        self.assertEqual(len(self.images.pictures), 1)
        self.assertIn("Picture 1 is a photo", prompt)
        self.assertEqual(self.images.pictures[-1]["id"], photo)

    def test_a_proposed_photo_must_exist(self):
        mia = self.story["cards"]["characters"][1]["id"]
        plan = {
            "reply": "x",
            "actions": [{"tool": "propose_image", "ref": mia, "photos": ["asset-x"]}],
        }
        response = self.turn("draw her", "turn-0002", plan)
        self.assertEqual(response.status_code, 409, response.text)

    def test_a_drawing_with_a_missing_photo_charges_nothing(self):
        zib = self.story["cards"]["characters"][0]["id"]
        response = self.client.post(
            f"/v1/story-chats/{self.pid}/images",
            json={"request_id": "pic-00000010", "card_id": zib, "photo_ids": ["nope"]},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(response.json()["detail"]["nothing_charged"])
        self.assertEqual(self.images.calls, [])


class PhotoPromptTests(unittest.TestCase):
    def test_photos_change_the_hash_and_are_numbered_after_card_pictures(self):
        project = {
            "journey": {},
            "entities": [
                {"id": "p", "type": "image", "data": {"cloudAssetId": "asset-p"}},
            ],
        }
        card = {"id": "c", "type": "prop", "title": "Scarf", "description": ""}
        plain = input_hash(project, card, "")
        self.assertEqual(plain, input_hash(project, card, "", ()))
        self.assertNotEqual(plain, input_hash(project, card, "", ["p"]))
        prompt = prompt_for(
            project, card, "", [{"row": {}, "jpeg": b"x"}], [{"jpeg": b"y"}]
        )
        self.assertIn("Picture 2 is a photo the person added", prompt)
        self.assertTrue(prompt.endswith("No text, captions, watermarks or borders."))


if __name__ == "__main__":
    unittest.main()
