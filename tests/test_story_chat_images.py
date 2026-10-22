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


def set_pictures(app, enabled):
    """Store the admin switch directly (the admin route is tested separately)."""
    from sqlalchemy import insert, select, update

    from studio_platform import story_chat_settings as switch
    from studio_platform.repository import documents

    repo = app.state.repository
    tenant = app.state.settings.tenant_id
    where = (
        documents.c.tenant_id == tenant,
        documents.c.owner_id == switch.OWNER,
        documents.c.project_id == switch.PROJECT,
        documents.c.kind == switch.KIND,
        documents.c.document_id == switch.ID,
    )
    with repo.transaction() as conn:
        value = {"pictures_enabled": enabled}
        if conn.execute(select(documents.c.version).where(*where)).first():
            conn.execute(update(documents).where(*where).values(payload=value))
        else:
            conn.execute(
                insert(documents).values(
                    tenant_id=tenant,
                    owner_id=switch.OWNER,
                    project_id=switch.PROJECT,
                    kind=switch.KIND,
                    document_id=switch.ID,
                    payload=value,
                    version=1,
                    updated_at=repo.clock(),
                )
            )


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
    """Records the credit calls; mimics the primitive's replay results."""

    def __init__(self):
        self.events, self.state = [], {}

    def hold(self, scope, reference, binding):
        assert binding["request_id"] == reference.split(":", 1)[1]
        assert len(binding["input_hash"]) == 64
        self.events.append(("hold", reference))
        prior = self.state.get(reference)
        if prior is None:
            self.state[reference] = "open"
            return {"created": True, "terminal": None, "attachment": None}
        terminal = {"kind": prior} if prior in ("capture", "release") else None
        return {"created": False, "terminal": terminal, "attachment": None}

    def capture(self, scope, reference, binding):
        self.events.append(("capture", reference))
        self.state[reference] = "capture"

    def release(self, scope, reference, binding, reason):
        self.events.append(("release", reference, reason))
        self.state[reference] = "release"


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
        set_pictures(self.app, True)
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

        def delete():
            version = self.client.get(
                "/v1/projects/" + self.story["project_id"]
            ).json()["version"]
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

    def test_charging_on_but_not_payable_refuses_before_drawing(self):
        # Charging on without a ready ledger or image price fails closed: no picture is
        # drawn for free and Google is never called.
        zib = self.story["cards"]["characters"][0]["id"]
        with patch(
            "studio_platform.credits.credit_policy", return_value={"enabled": True}
        ):
            response = self.draw(zib)
        self.assertEqual(response.status_code, 503, response.text)
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
        client.post(self.path, json={"request_id": "pic-bill-001", "card_id": zib})
        # In flight elsewhere (open hold, nothing attached): refuse, never draw twice.
        self.billing.state["story_chat_image:pic-bill-003"] = "open"
        busy = client.post(
            self.path, json={"request_id": "pic-bill-003", "card_id": zib}
        )
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json()["detail"]["code"], "story_chat_image_in_progress")
        self.assertEqual(len(self.images.calls), 2)
        self.assertEqual(
            self.billing.events,
            [
                ("hold", "story_chat_image:pic-bill-001"),
                ("capture", "story_chat_image:pic-bill-001"),
                ("hold", "story_chat_image:pic-bill-002"),
                ("release", "story_chat_image:pic-bill-002", "provider_failed"),
                # A replay of the delivered picture settles idempotently; no new drawing.
                ("hold", "story_chat_image:pic-bill-001"),
                ("capture", "story_chat_image:pic-bill-001"),
                ("hold", "story_chat_image:pic-bill-003"),
            ],
        )

    def test_same_request_during_drawing_never_calls_the_provider_twice(self):
        zib = self.story["cards"]["characters"][0]["id"]
        inner = {}

        def overlap():
            inner["response"] = self.draw(zib)

        self.images.during = overlap
        first = self.draw(zib)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(inner["response"].status_code, 409)
        self.assertEqual(
            inner["response"].json()["detail"]["code"], "story_chat_image_in_progress"
        )
        self.assertEqual(len(self.images.calls), 1)

    def test_unknown_outcome_is_never_resubmitted(self):
        zib = self.story["cards"]["characters"][0]["id"]
        self.images.fail = ImageError("image_upstream_timeout", 504)
        self.assertEqual(self.draw(zib).status_code, 504)
        self.images.fail = None
        again = self.draw(zib)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"]["code"], "story_chat_image_in_progress")
        self.assertEqual(len(self.images.calls), 1)

    def test_whole_project_save_does_not_erase_replay_proof(self):
        zib = self.story["cards"]["characters"][0]["id"]
        first = self.draw(zib).json()
        current = self.client.get("/v1/projects/" + self.story["project_id"]).json()
        project = current["project"]
        project["journey"]["storyChat"]["images"] = {}
        project["journey"]["storyChat"].pop("imageRequests", None)
        saved = self.client.put(
            "/v1/projects/" + self.story["project_id"],
            json={"project": project, "expected_version": current["version"]},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        again = self.draw(zib)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["image"], first["image"])
        self.assertEqual(len(self.images.calls), 1)

    def test_card_shows_the_selected_variant_it_was_drawn_into(self):
        zib = self.story["cards"]["characters"][0]["id"]
        current = self.client.get("/v1/projects/" + self.story["project_id"]).json()
        card = next(e for e in current["project"]["entities"] if e["id"] == zib)
        old = next(
            e
            for e in current["project"]["entities"]
            if e["type"] == "character" and e["id"] != zib
        )
        looks = [
            {
                "id": "main",
                "name": "Old",
                "description": "",
                "gallery": {},
                "imageIds": [],
                "version": 1,
            },
            {
                "id": "alt",
                "name": "Alt",
                "description": "",
                "gallery": {},
                "imageIds": [],
                "version": 1,
            },
        ]
        self.assertTrue(card and old)
        r = self.client.post(
            f"/v1/projects/{self.story['project_id']}/actions",
            json={
                "expected_version": current["version"],
                "actions": [
                    {
                        "op": "entity.update",
                        "entity_id": zib,
                        "patch": {"data": {"looks": looks, "defaultLookId": "alt"}},
                    }
                ],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.draw(zib, "pic-main-0001")  # goes into the selected "alt" look
        self.client.post(
            f"/v1/projects/{self.story['project_id']}/actions",
            json={
                "expected_version": self.client.get(
                    "/v1/projects/" + self.story["project_id"]
                ).json()["version"],
                "actions": [
                    {
                        "op": "entity.update",
                        "entity_id": zib,
                        "patch": {
                            "data": {
                                "looks": [
                                    {**looks[0], "imageIds": ["image-pic-main-0001"]},
                                    {**looks[1], "imageIds": ["image-pic-main-0001"]},
                                ]
                            }
                        },
                    }
                ],
            },
        )
        value = self.draw(zib, "pic-alt-00002").json()
        shown = next(c for c in value["cards"]["characters"] if c["id"] == zib)
        self.assertEqual(shown["image_entity_id"], "image-pic-alt-00002")

    def test_malformed_story_marker_is_left_out_of_the_list(self):
        r = self.client.post(
            "/v1/projects", json={"title": "Odd", "id": "project-odd-marker-01"}
        )
        self.assertEqual(r.status_code, 201, r.text)
        for marker in ("garbage", {"x": 1}, True):
            current = self.client.get("/v1/projects/project-odd-marker-01").json()
            r = self.client.post(
                "/v1/projects/project-odd-marker-01/actions",
                json={
                    "expected_version": current["version"],
                    "actions": [
                        {
                            "op": "journey.update",
                            "patch": {"storyChat": {"version": marker}},
                        }
                    ],
                },
            )
            self.assertEqual(r.status_code, 200, r.text)
            listed = self.client.get("/v1/story-chats")
            self.assertEqual(listed.status_code, 200, listed.text)
            ids = [c["project_id"] for c in listed.json()["story_chats"]]
            self.assertEqual(ids, [self.story["project_id"]])

    def test_switch_off_refuses_new_pictures_before_any_hold(self):
        zib = self.story["cards"]["characters"][0]["id"]
        set_pictures(self.app, False)
        self.assertFalse(
            self.client.get("/v1/story-chats/config").json()["pictures_enabled"]
        )
        refused = self.draw(zib)
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(
            refused.json()["detail"]["code"], "story_chat_pictures_disabled"
        )
        self.assertEqual(self.images.calls, [])
        set_pictures(self.app, True)
        self.assertTrue(
            self.client.get("/v1/story-chats/config").json()["pictures_enabled"]
        )

    def test_switch_off_still_returns_an_already_drawn_picture(self):
        zib = self.story["cards"]["characters"][0]["id"]
        first = self.draw(zib).json()
        set_pictures(self.app, False)
        again = self.draw(zib)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["image"], first["image"])
        self.assertEqual(len(self.images.calls), 1)

    def test_only_admins_read_or_change_the_switch(self):
        self.assertEqual(
            self.client.get("/v1/admin/story-chat/settings").status_code, 403
        )
        put = self.client.put(
            "/v1/admin/story-chat/settings",
            json={"expected_version": 1, "pictures_enabled": False, "reason": "x"},
        )
        self.assertEqual(put.status_code, 403)

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
