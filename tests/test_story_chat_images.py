"""Story chat card pictures (#422) with a fake image model and a recording billing hook."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.google_chat import ORIGIN
from studio_platform.story_chat_images import (
    GeminiImageClient,
    ImageError,
    attach_actions,
    input_hash,
    load_pictures,
    older_settings_in_use,
    prompt_for,
    reference_cards,
    shot_storyboard,
)
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

    def generate(self, prompt, aspect="1:1", pictures=()):
        self.calls.append((prompt, aspect))
        self.pictures = pictures
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

    def has_hold(self, scope, reference):
        return reference in self.state

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

    def shot(self):
        return next(e for e in self.project()["entities"] if e["type"] == "shot")

    def type_to_inky(self, text="Make her look up slowly", turn=None, shot=None):
        self._turn = getattr(self, "_turn", 0) + 1
        return self.client.post(
            f"/v1/story-chats/{self.story['project_id']}/turns",
            json={
                "turn_id": turn or f"inky-turn-{self._turn:04d}",
                "text": text,
                "inky": {"shot_id": shot},
            },
        )

    def test_typing_to_inky_changes_the_shot_and_undo_restores_it(self):
        shot = self.shot()
        before = shot["data"].get("prompt")
        self.model.plans.append(
            {
                "reply": "She now looks up slowly.",
                "actions": [
                    {
                        "tool": "upsert_shot",
                        "ref": shot["id"],
                        "text": "She looks up slowly.",
                    }
                ],
            }
        )
        reply = self.type_to_inky(shot=shot["id"])
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(self.shot()["data"]["prompt"], "She looks up slowly.")
        turn = reply.json()["turn"]
        self.assertEqual(turn["chips"], [])
        self.assertTrue(turn["changes"])
        sent = str(self.model.calls[-1])
        self.assertIn("typing to Inky", sent)
        self.assertIn("Never mention another", sent)
        self.assertIn(shot["id"], sent.split("looking at the shot")[1][:80])
        undone = self.client.post(
            f"/v1/story-chats/{self.story['project_id']}/undo",
            json={"turn_id": turn["turn_id"]},
        )
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(self.shot()["data"].get("prompt"), before)

    def test_typing_to_inky_cannot_remove_or_move_towards_spending(self):
        shot = self.shot()
        cards_before = len(self.project()["entities"])
        self.model.plans.append(
            {
                "reply": "Done.",
                "actions": [
                    {"tool": "remove_card", "ref": shot["id"]},
                    {"tool": "propose_generate", "shots": ["all"]},
                    {"tool": "propose_image", "ref": shot["id"]},
                    {"tool": "propose_new_story", "idea": "Another"},
                ],
            }
        )
        reply = self.type_to_inky(shot=shot["id"])
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(len(self.project()["entities"]), cards_before)
        self.assertIsNone(reply.json()["turn"]["proposal"])
        self.assertEqual(self.images.calls, [])

    def test_typing_to_inky_stops_at_the_daily_limit(self):
        shot = self.shot()
        with patch("studio_platform.story_chat_routes.INKY_DAILY_TURNS", 1):
            self.model.plans.append({"reply": "One.", "actions": []})
            self.assertEqual(self.type_to_inky(shot=shot["id"]).status_code, 200)
            calls = len(self.model.calls)
            limited = self.type_to_inky(shot=shot["id"])
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertEqual(limited.json()["detail"]["code"], "inky_daily_limit")
        self.assertEqual(len(self.model.calls), calls)

    def test_typing_to_inky_on_a_story_without_a_chat_stays_out_of_quick_create(self):
        created = self.client.post("/v1/projects", json={"title": "Plain story"}).json()
        project_id = created.get("id") or created["project"]["id"]
        self.model.plans.append({"reply": "Nothing to change yet.", "actions": []})
        reply = self.client.post(
            f"/v1/story-chats/{project_id}/turns",
            json={
                "turn_id": "inky-plain-0001",
                "text": "Hello",
                "inky": {"shot_id": None},
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        listed = self.client.get("/v1/story-chats").json()
        self.assertNotIn(project_id, str(listed))

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

    def shot(self):
        return next(e for e in self.project()["entities"] if e["type"] == "shot")

    def test_shot_picture_becomes_its_first_frame(self):
        shot = self.shot()
        first = self.draw(shot["id"], "pic-shot-001")
        self.assertEqual(first.status_code, 200, first.text)
        entity = self.shot()
        self.assertEqual(
            entity["data"]["storyboard"]["firstFrameId"], "image-pic-shot-001"
        )
        self.assertIs(entity["data"]["storyboard"]["continuePrevious"], False)
        prompt, aspect = self.images.calls[0]
        self.assertIn("first moment of this shot", prompt)
        self.assertEqual(aspect, "16:9")
        # Another picture takes over, and the earlier one stays in the story's files.
        self.assertEqual(self.draw(shot["id"], "pic-shot-002").status_code, 200)
        entity = self.shot()
        self.assertEqual(
            entity["data"]["storyboard"]["firstFrameId"], "image-pic-shot-002"
        )
        files = {e["id"] for e in self.project()["entities"] if e["type"] == "image"}
        self.assertLessEqual({"image-pic-shot-001", "image-pic-shot-002"}, files)

    def test_older_shot_without_settings_gets_a_valid_first_frame(self):
        shot = self.shot()
        project_id = self.path.split("/")[3]
        project = self.client.get("/v1/projects/" + project_id).json()
        created = self.client.post(
            f"/v1/projects/{project_id}/actions",
            json={
                "expected_version": project["version"],
                "actions": [
                    {
                        "op": "entity.create",
                        "entity": {
                            "id": "older-shot",
                            "type": "shot",
                            "parentId": shot["parentId"],
                            "title": "Older shot",
                            "data": {
                                "seconds": 5,
                                "prompt": "She turns to the window.",
                            },
                        },
                    }
                ],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        reply = self.draw("older-shot", "pic-older-001")
        self.assertEqual(reply.status_code, 200, reply.text)
        entity = next(e for e in self.project()["entities"] if e["id"] == "older-shot")
        self.assertEqual(
            entity["data"]["storyboard"],
            {
                "version": 1,
                "mode": "fl",
                "firstFrameId": "image-pic-older-001",
                "continuePrevious": False,
            },
        )
        self.assertEqual(len(self.images.calls), 1)

    def create_older_shot(self, ident, h3):
        shot = self.shot()
        project = self.client.get("/v1/projects/" + self.story["project_id"]).json()
        created = self.client.post(
            f"/v1/projects/{self.story['project_id']}/actions",
            json={
                "expected_version": project["version"],
                "actions": [
                    {
                        "op": "entity.create",
                        "entity": {
                            "id": ident,
                            "type": "shot",
                            "parentId": shot["parentId"],
                            "title": "Older shot",
                            "data": {"seconds": 5, "prompt": "She looks up.", "h3": h3},
                        },
                    }
                ],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)

    def test_shot_with_older_settings_keeps_them_and_spends_nothing(self):
        for ident, h3 in (
            ("older-recipe", {"recipeId": "h3-base-ref2va-v1"}),
            ("older-stale", {"recipeId": "h3-base-ref2va-v1", "inputMode": "fl"}),
            ("older-fl", {"recipeId": "h3-base-fl2va-v1"}),
        ):
            with self.subTest(ident=ident):
                self.create_older_shot(ident, h3)
                before = next(e for e in self.project()["entities"] if e["id"] == ident)
                reply = self.draw(ident, "pic-" + ident)
                self.assertEqual(reply.status_code, 409, reply.text)
                self.assertEqual(
                    reply.json()["detail"]["code"], "story_chat_shot_settings_invalid"
                )
                self.assertIs(reply.json()["detail"]["nothing_charged"], True)
                after = next(e for e in self.project()["entities"] if e["id"] == ident)
                self.assertEqual(before["data"], after["data"])
        self.assertEqual(self.images.calls, [])

    def test_shot_with_a_reference_link_keeps_it_and_spends_nothing(self):
        project = {
            "links": [{"source": "photo", "target": "s1", "role": "reference"}],
        }
        card = {"id": "s1", "type": "shot", "data": {"prompt": "x"}}
        self.assertTrue(older_settings_in_use(project, card))
        self.assertFalse(older_settings_in_use({"links": []}, card))
        with_block = {**card, "data": {"storyboard": {"version": 1, "mode": "ref"}}}
        self.assertFalse(older_settings_in_use(project, with_block))

    def test_existing_settings_stay_and_a_bare_shot_gets_the_default(self):
        kept = {"version": 1, "mode": "ref", "quality": "final"}
        self.assertEqual(shot_storyboard({"storyboard": kept}), kept)
        self.assertEqual(shot_storyboard({}), {"version": 1, "mode": "fl"})

    def test_opening_hidden_keeps_the_story_out_of_quick_create(self):
        story = self.client.post("/v1/projects", json={"title": "Plain story"}).json()
        project_id = story.get("id") or story["project"]["id"]
        path = f"/v1/story-chats/{project_id}/open"
        first = self.client.post(path, json={"language": "en", "hidden": True})
        self.assertEqual(first.status_code, 200, first.text)
        listed = self.client.get("/v1/story-chats").json()
        self.assertNotIn(project_id, str(listed))
        # Asking again keeps it hidden; a plain open still brings it back.
        self.assertEqual(self.client.post(path, json={"hidden": True}).status_code, 200)
        self.assertNotIn(project_id, str(self.client.get("/v1/story-chats").json()))
        self.assertEqual(self.client.post(path, json={}).status_code, 200)
        self.assertIn(project_id, str(self.client.get("/v1/story-chats").json()))
        self.assertEqual(
            self.client.post(path, json={"hidden": False}).status_code, 422
        )

    def test_a_chapter_or_scene_is_not_a_picture_card(self):
        scene = next(
            e for e in self.project()["entities"] if e["type"] in {"scene", "chapter"}
        )
        self.assertEqual(self.draw(scene["id"]).status_code, 404)
        self.assertEqual(self.images.calls, [])

    def test_starter_pictures_are_a_closed_list(self):
        names = ("alien", "coffee", "fox", "look-film", "look-anime", "look-watercolor")
        for name in (*names, "look-noir", "look-soft3d", "look-storybook"):
            reply = self.client.get("/v1/story-chats/starter-pictures/" + name)
            self.assertEqual(reply.status_code, 200, name)
            self.assertEqual(reply.headers["content-type"], "image/webp")
        missing = self.client.get("/v1/story-chats/starter-pictures/other")
        self.assertEqual(missing.status_code, 404)

    def test_a_script_may_plan_more_than_a_normal_turn(self):
        from studio_platform.story_chat import (
            MAX_ACTIONS,
            MAX_SCRIPT_ACTIONS,
            StoryChatError,
            _parse,
        )

        plan = json.dumps({"reply": "ok", "actions": [{"op": "x"}] * (MAX_ACTIONS + 1)})
        with self.assertRaises(StoryChatError):
            _parse(plan)
        self.assertEqual(
            len(_parse(plan, max_actions=MAX_SCRIPT_ACTIONS)["actions"]),
            MAX_ACTIONS + 1,
        )

    def test_a_script_turn_has_its_own_limit_and_note(self):
        project = self.story["project_id"]
        path = f"/v1/story-chats/{project}/turns"
        long_text = "x" * 5000
        refused = self.client.post(
            path, json={"turn_id": "turn-0002", "text": long_text}
        )
        self.assertEqual(refused.status_code, 422)
        too_long = self.client.post(
            path, json={"turn_id": "turn-0003", "text": "x" * 20001, "script": True}
        )
        self.assertEqual(too_long.status_code, 422)
        self.assertEqual(too_long.json()["detail"]["max"], 20000)
        bad = self.client.post(
            path, json={"turn_id": "turn-0004", "text": "x", "script": False}
        )
        self.assertEqual(bad.status_code, 422)
        self.model.plans.append({"reply": "Please check the shots.", "actions": []})
        reply = self.client.post(
            path, json={"turn_id": "turn-0005", "text": long_text, "script": True}
        )
        self.assertEqual(reply.status_code, 200, reply.text)

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

    def first_frame_project(self):
        def entity(ident, kind, title, description="", data=None):
            return {
                "id": ident,
                "type": kind,
                "title": title,
                "description": description,
                "parentId": None,
                "data": data or {},
                "version": 1,
            }

        return {
            "entities": [
                entity("pic-ma", "image", "ma", data={"cloudAssetId": "asset-ma"}),
                entity(
                    "ma",
                    "character",
                    "Lin",
                    "Silver braid, red glasses.",
                    {"imageIds": ["pic-ma"]},
                ),
                entity("bo", "character", "Dino", "Green hoodie."),
                entity("st", "location", "Stall", "Rainy noodle stall."),
                entity("ca", "prop", "Cage", "Brass birdcage."),
                entity(
                    "s1",
                    "shot",
                    "s1",
                    data={
                        "prompt": "Lin slams the cage down.",
                        "cast": ["ma", "bo"],
                        "storyboard": {
                            "place": {"locationId": "st"},
                            "propIds": ["ca"],
                        },
                    },
                ),
                entity("s2", "shot", "s2", data={"prompt": "Rain.", "cast": []}),
            ],
            "links": [],
            "journey": {},
        }

    def test_first_frame_is_told_what_each_card_looks_like(self):
        project = self.first_frame_project()
        shot = project["entities"][-2]
        prompt = prompt_for(project, shot)
        self.assertIn("Lin (Silver braid, red glasses.); Dino (Green hoodie.)", prompt)
        self.assertIn("Stall (Rainy noodle stall.)", prompt)
        self.assertIn("Cage (Brass birdcage.)", prompt)
        self.assertIn("No extra people", prompt)
        self.assertNotIn("Picture 1", prompt)

    def test_first_frame_numbers_only_the_pictures_it_attaches(self):
        project = self.first_frame_project()
        shot = project["entities"][-2]
        rows = reference_cards(project, shot)
        self.assertEqual([r["asset_id"] for r in rows], ["asset-ma", None, None, None])
        pictures = [{"row": rows[0], "jpeg": b"x"}]
        prompt = prompt_for(project, shot, "", pictures)
        self.assertIn("Picture 1 is Lin", prompt)
        self.assertNotIn("Picture 2", prompt)

    def test_first_frame_with_no_cast_names_no_one(self):
        project = self.first_frame_project()
        prompt = prompt_for(project, project["entities"][-1])
        self.assertNotIn("Characters:", prompt)
        self.assertNotIn("No extra people", prompt)

    def test_a_changed_card_changes_what_was_agreed(self):
        project = self.first_frame_project()
        shot = project["entities"][-2]
        before = input_hash(project, shot, "")
        project["entities"][1]["description"] = "Now in a blue coat."
        self.assertNotEqual(before, input_hash(project, shot, ""))

    def test_an_unreadable_picture_leaves_the_others(self):
        rows = [
            {"asset_id": None},
            {"asset_id": "gone"},
            {"asset_id": "fine"},
        ]
        with patch(
            "studio_platform.inky_uploads.preview",
            side_effect=lambda _a, _p, _i, up: (
                b"jpeg"
                if up["image"]["asset_id"] == "fine"
                else (_ for _ in ()).throw(ValueError("gone"))
            ),
        ):
            pictures = load_pictures(None, None, "p", rows)
        self.assertEqual(pictures, [{"row": rows[2], "jpeg": b"jpeg"}])

    def test_the_request_carries_the_pictures(self):
        import base64

        import httpx

        seen = {}

        def answer(request):
            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/jpeg",
                                            "data": base64.b64encode(b"out").decode(),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        config = type("C", (), {"base_url": ORIGIN, "api_key": "k"})
        client = GeminiImageClient(
            lambda: config, transport=httpx.MockTransport(answer)
        )
        client.generate("draw", "16:9", [{"jpeg": b"abc"}])
        parts = seen["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "draw"})
        self.assertEqual(
            parts[1]["inlineData"],
            {"mimeType": "image/jpeg", "data": base64.b64encode(b"abc").decode()},
        )

    def test_another_account_cannot_draw(self):
        zib = self.story["cards"]["characters"][0]["id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "supervan"})
        self.assertEqual(self.draw(zib).status_code, 404)
        self.assertEqual(self.images.calls, [])


if __name__ == "__main__":
    unittest.main()
