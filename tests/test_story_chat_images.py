"""Story chat card pictures (#422) with a fake image model and a recording billing hook."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.story_chat_images import ImageError, attach_actions, prompt_for
from test_story_chat import CAST, ScriptedModel


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (384, 512), "teal").save(out, format="JPEG")
    return out.getvalue()


class FakeImages:
    def __init__(self):
        self.calls, self.fail, self.during = [], None, None

    def generate(self, prompt, aspect="1:1"):
        self.calls.append((prompt, aspect))
        if self.during:
            hook, self.during = self.during, None
            hook()
        if self.fail:
            raise self.fail
        return "image/jpeg", jpeg()


class Billing:
    def __init__(self):
        self.events = []

    def hold(self, principal, reference):
        self.events.append(("hold", reference))

    def capture(self, principal, reference):
        self.events.append(("capture", reference))

    def release(self, principal, reference, reason):
        self.events.append(("release", reference))


class StoryChatImageTests(unittest.TestCase):
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
        self.billing = Billing()
        self.client = TestClient(self.app)
        self.assertEqual(
            self.client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).status_code,
            200,
        )
        chat = self.client.post("/v1/story-chats", json={"language": "en"}).json()
        self.model.plans.append(CAST)
        self.story = self.client.post(
            f"/v1/story-chats/{chat['project_id']}/turns",
            json={"turn_id": "turn-0001", "text": "aliens"},
        ).json()
        self.path = f"/v1/story-chats/{chat['project_id']}/images"

    def draw(self, card_id, request_id="pic-00000001", **extra):
        return self.client.post(
            self.path, json={"request_id": request_id, "card_id": card_id, **extra}
        )

    def project(self):
        return self.client.get("/v1/projects/" + self.story["project_id"]).json()[
            "project"
        ]

    def test_character_picture_goes_on_the_default_look(self):
        zib = self.story["cards"]["characters"][0]
        response = self.draw(zib["id"])
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        card = value["cards"]["characters"][0]
        self.assertEqual(card["image_entity_id"], "image-pic-00000001")
        self.assertTrue(card["image_url"].startswith("/v1/assets/"))
        entity = next(e for e in self.project()["entities"] if e["id"] == zib["id"])
        self.assertEqual(entity["data"]["defaultLookId"], "main")
        self.assertEqual(entity["data"]["looks"][0]["imageIds"], ["image-pic-00000001"])
        prompt, aspect = self.images.calls[0]
        self.assertIn("Zib", prompt)
        self.assertEqual(aspect, "3:4")
        self.assertEqual(self.client.get(card["image_url"]).status_code, 200)

    def test_place_and_prop_use_their_editor_shapes(self):
        place = self.story["cards"]["places"][0]["id"]
        prop = self.story["cards"]["props"][0]["id"]
        self.assertEqual(self.draw(place, "pic-place-01").status_code, 200)
        self.assertEqual(self.draw(prop, "pic-prop-001").status_code, 200)
        entities = {e["id"]: e for e in self.project()["entities"]}
        self.assertEqual(
            entities[place]["data"]["versions"][0]["imageIds"], ["image-pic-place-01"]
        )
        self.assertEqual(entities[prop]["data"]["imageIds"], ["image-pic-prop-001"])
        self.assertEqual([a for _, a in self.images.calls], ["16:9", "1:1"])

    def test_replay_does_not_draw_again(self):
        zib = self.story["cards"]["characters"][0]["id"]
        first = self.draw(zib).json()
        again = self.draw(zib)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.images.calls), 1)
        self.assertEqual(again.json()["image"], first["image"])

    def test_redraw_puts_the_new_picture_first(self):
        zib = self.story["cards"]["characters"][0]["id"]
        self.draw(zib, "pic-first-01")
        self.draw(zib, "pic-second-1", instruction="shorter antennae")
        entity = next(e for e in self.project()["entities"] if e["id"] == zib)
        self.assertEqual(
            entity["data"]["looks"][0]["imageIds"],
            ["image-pic-second-1", "image-pic-first-01"],
        )
        self.assertIn("shorter antennae", self.images.calls[1][0])

    def test_model_failure_attaches_nothing(self):
        zib = self.story["cards"]["characters"][0]["id"]
        self.images.fail = ImageError("image_not_returned", 422)
        response = self.draw(zib)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "image_not_returned")
        self.assertTrue(response.json()["detail"]["nothing_charged"])
        entity = next(e for e in self.project()["entities"] if e["id"] == zib)
        self.assertEqual(entity["data"]["looks"][0]["imageIds"], [])

    def test_card_deleted_during_drawing_is_refused(self):
        zib = self.story["cards"]["characters"][0]["id"]
        version = self.story["version"]

        def delete():
            shots = [s for s in self.story["cards"]["shots"] if zib in s["cast"]]
            actions = [
                {
                    "op": "entity.update",
                    "entity_id": s["id"],
                    "patch": {"data": {"cast": []}},
                }
                for s in shots
            ] + [{"op": "entity.delete", "entity_id": zib}]
            r = self.client.post(
                f"/v1/projects/{self.story['project_id']}/actions",
                json={"expected_version": version, "actions": actions},
            )
            self.assertEqual(r.status_code, 200, r.text)

        self.images.during = delete
        response = self.draw(zib)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "story_chat_card_missing")

    def test_charging_on_without_the_service_charge_refuses_before_drawing(self):
        zib = self.story["cards"]["characters"][0]["id"]
        with patch(
            "studio_platform.credits.credit_policy", return_value={"enabled": True}
        ):
            response = self.draw(zib)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "image_billing_unavailable")
        self.assertEqual(self.images.calls, [])

    def test_billing_hook_holds_then_captures_or_releases(self):
        app = create_app(
            self.app.state.settings,
            repository=self.app.state.repository,
            storage=self.app.state.storage,
            story_chat_client=self.model,
            story_chat_image_client=self.images,
            story_chat_billing=self.billing,
        )
        client = TestClient(app)
        client.post("/api/auth/login", json={"username": "superdan"})
        zib = self.story["cards"]["characters"][0]["id"]
        ok = client.post(self.path, json={"request_id": "pic-bill-001", "card_id": zib})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.images.fail = ImageError("image_not_returned", 422)
        failed = client.post(
            self.path, json={"request_id": "pic-bill-002", "card_id": zib}
        )
        self.assertEqual(failed.status_code, 422)
        # A replay of the delivered picture moves no credits at all.
        client.post(self.path, json={"request_id": "pic-bill-001", "card_id": zib})
        self.assertEqual(
            self.billing.events,
            [
                ("hold", "story_chat_image:pic-bill-001"),
                ("capture", "story_chat_image:pic-bill-001"),
                ("hold", "story_chat_image:pic-bill-002"),
                ("release", "story_chat_image:pic-bill-002"),
            ],
        )

    def test_prompt_and_attach_shapes(self):
        project = self.project()
        zib = next(e for e in project["entities"] if e["type"] == "character")
        actions = attach_actions(project, zib, "asset-1", "image-x")
        self.assertEqual(actions[0]["op"], "asset.attach")
        self.assertEqual(
            actions[1]["patch"]["data"]["looks"][0]["imageIds"], ["image-x"]
        )
        self.assertIn(zib["title"], prompt_for(project, zib))

    def test_another_account_cannot_draw(self):
        zib = self.story["cards"]["characters"][0]["id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "supervan"})
        self.assertEqual(self.draw(zib).status_code, 404)
        self.assertEqual(self.images.calls, [])


if __name__ == "__main__":
    unittest.main()
