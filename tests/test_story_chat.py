"""Chat-first story building (#422) with a scripted model; no network, no GPU, no credits."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from studio_platform.api import create_app
from studio_platform.google_chat import ChatError, payload
from studio_platform.repository import jobs
from studio_platform.settings import Settings


class ScriptedModel:
    """Returns the next scripted plan; an optional hook runs during the call."""

    def __init__(self):
        self.plans, self.calls, self.during = [], [], None

    def generate(self, model, messages, instruction, max_tokens=2048, json_mode=False):
        # Validate exactly as the real adapter does before any network call.
        body = payload(model, messages, instruction, max_tokens, json_mode)
        self.configs = getattr(self, "configs", []) + [body["generationConfig"]]
        self.calls.append(
            {"model": model, "messages": messages, "instruction": instruction}
        )
        if self.during:
            hook, self.during = self.during, None
            hook()
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return {"text": plan if isinstance(plan, str) else json.dumps(plan)}


CAST = {
    "reply": "I made the cast. How long should it be?",
    "chips": ["30 seconds", "Make it funny"],
    "actions": [
        {"tool": "set_title", "title": "First Contact"},
        {
            "tool": "upsert_character",
            "ref": "new:zib",
            "name": "Zib",
            "description": "Small green alien",
        },
        {
            "tool": "upsert_character",
            "ref": "new:mia",
            "name": "Mia",
            "description": "Girl, red raincoat",
        },
        {
            "tool": "upsert_place",
            "ref": "new:field",
            "name": "Cornfield",
            "description": "Night, full moon",
        },
        {
            "tool": "upsert_prop",
            "ref": "new:pod",
            "name": "Pod",
            "description": "Glowing teal ship",
        },
        {
            "tool": "upsert_shot",
            "ref": "new:s1",
            "text": "Light falls into the field.",
            "place": "new:field",
            "props": ["new:pod"],
        },
        {
            "tool": "upsert_shot",
            "ref": "new:s3",
            "text": "They touch hands.",
            "cast": ["new:mia", "new:zib"],
            "place": "new:field",
            "seconds": 6,
        },
        {
            "tool": "upsert_shot",
            "ref": "new:s2",
            "text": "Mia sees the glow.",
            "cast": ["new:mia"],
            "after": "new:s1",
        },
    ],
}


class StoryChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            Path(self.tmp.name), auth_mode="local-test", story_chat_enabled=True
        )
        self.model = ScriptedModel()
        self.app = create_app(self.settings, story_chat_client=self.model)
        self.addCleanup(self.app.state.repository.engine.dispose)
        self.client = TestClient(self.app)

    def login(self, who="superdan"):
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": who}).status_code, 200
        )

    def start(self):
        self.login()
        response = self.client.post("/v1/story-chats", json={"language": "en"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def turn(self, chat, text, turn_id="turn-0001", plan=CAST):
        if plan is not None:
            self.model.plans.append(plan)
        return self.client.post(
            f"/v1/story-chats/{chat['project_id']}/turns",
            json={"turn_id": turn_id, "text": text},
        )

    def jobs(self):
        with self.app.state.repository.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(jobs)).scalar()

    def test_disabled_by_default(self):
        app = create_app(
            replace(self.settings, story_chat_enabled=False),
            repository=self.app.state.repository,
            story_chat_client=self.model,
        )
        client = TestClient(app)
        client.post("/api/auth/login", json={"username": "superdan"})
        self.assertFalse(client.get("/v1/story-chats/config").json()["enabled"])
        response = client.post("/v1/story-chats", json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "story_chat_disabled")

    def test_a_story_chat_is_a_normal_project(self):
        chat = self.start()
        self.assertEqual(chat["messages"], [])
        listed = self.client.get("/v1/projects").json()
        ids = [
            p.get("id") or p.get("project_id") for p in listed.get("projects", listed)
        ]
        self.assertIn(chat["project_id"], json.dumps(listed))
        project = self.client.get(f"/v1/projects/{chat['project_id']}")
        self.assertEqual(project.status_code, 200, project.text)
        self.assertTrue(ids)

    def test_turn_builds_cards_in_order_with_references(self):
        chat = self.start()
        before = self.jobs()
        response = self.turn(chat, "An alien meets a girl in a cornfield")
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        cards = value["cards"]
        self.assertEqual(value["title"], "First Contact")
        self.assertEqual([c["name"] for c in cards["characters"]], ["Zib", "Mia"])
        self.assertEqual([c["name"] for c in cards["places"]], ["Cornfield"])
        self.assertEqual([c["name"] for c in cards["props"]], ["Pod"])
        shots = cards["shots"]
        self.assertEqual(
            [s["text"] for s in shots],
            ["Light falls into the field.", "Mia sees the glow.", "They touch hands."],
        )
        self.assertEqual(
            [(s["start"], s["end"]) for s in shots], [(0, 5), (5, 10), (10, 16)]
        )
        mia = cards["characters"][1]["id"]
        zib = cards["characters"][0]["id"]
        self.assertEqual(shots[2]["cast"], [mia, zib])
        self.assertEqual(shots[0]["place"], cards["places"][0]["id"])
        self.assertEqual(shots[0]["props"], [cards["props"][0]["id"]])
        self.assertEqual([m["role"] for m in value["messages"]], ["user", "assistant"])
        self.assertEqual(value["turn"]["chips"], ["30 seconds", "Make it funny"])
        self.assertEqual(self.jobs(), before)
        # The stored shot is the existing storyboard shape, so Works and preflight read it.
        project = self.client.get(f"/v1/projects/{chat['project_id']}").json()[
            "project"
        ]
        shot = next(e for e in project["entities"] if e["id"] == shots[2]["id"])
        self.assertEqual(shot["data"]["prompt"], "They touch hands.")
        self.assertEqual(shot["data"]["storyboard"]["mode"], "ref")
        self.assertEqual(shot["data"]["seconds"], 6)
        # Works reads character/look bindings, not bare ids.
        self.assertEqual(
            shot["data"]["cast"],
            [{"characterId": mia, "lookId": ""}, {"characterId": zib, "lookId": ""}],
        )

    def test_replayed_turn_does_not_call_the_model_again(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        again = self.turn(chat, "aliens", plan=None)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(again.json()["turn"], first["turn"])
        self.assertEqual(again.json()["version"], first["version"])

    def test_user_edit_during_the_call_is_kept(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        zib = first["cards"]["characters"][0]

        def edit():
            response = self.client.post(
                f"/v1/projects/{chat['project_id']}/actions",
                json={
                    "expected_version": first["version"],
                    "actions": [
                        {
                            "op": "entity.update",
                            "entity_id": zib["id"],
                            "patch": {"title": "Zibby"},
                        }
                    ],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)

        self.model.during = edit
        plan = {
            "reply": "Added a shot.",
            "actions": [
                {
                    "tool": "upsert_shot",
                    "ref": "new:s4",
                    "text": "Zib waves.",
                    "cast": [zib["id"]],
                }
            ],
        }
        response = self.turn(chat, "add a wave", turn_id="turn-0002", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["cards"]["characters"][0]["name"], "Zibby")
        self.assertEqual(value["cards"]["shots"][-1]["text"], "Zib waves.")
        self.assertEqual(value["version"], first["version"] + 2)

    def actions(self, chat, version, actions):
        response = self.client.post(
            f"/v1/projects/{chat['project_id']}/actions",
            json={"expected_version": version, "actions": actions},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_same_card_edited_during_the_call_is_not_overwritten(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        zib = first["cards"]["characters"][0]["id"]
        self.model.during = lambda: self.actions(
            chat,
            first["version"],
            [
                {
                    "op": "entity.update",
                    "entity_id": zib,
                    "patch": {"title": "Mine", "description": "user"},
                }
            ],
        )
        plan = {
            "reply": "Renamed.",
            "actions": [
                {
                    "tool": "upsert_character",
                    "ref": zib,
                    "name": "Model",
                    "description": "model",
                }
            ],
        }
        response = self.turn(chat, "rename", turn_id="turn-0010", plan=plan)
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "story_chat_card_changed")
        self.assertEqual(detail["cards"], [zib])
        after = self.client.get(f"/v1/story-chats/{chat['project_id']}").json()
        self.assertEqual(after["cards"]["characters"][0]["name"], "Mine")
        self.assertEqual(after["version"], first["version"] + 1)
        self.assertEqual(len(after["messages"]), 2)

    def test_full_project_save_during_the_call_is_not_overwritten(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        zib = first["cards"]["characters"][0]["id"]

        def save():
            current = self.client.get(f"/v1/projects/{chat['project_id']}").json()
            project = current["project"]
            card = next(e for e in project["entities"] if e["id"] == zib)
            # A browser save changes content without bumping the nested entity version.
            card["title"], card["description"] = "USER via PUT", "user blue coat"
            r = self.client.put(
                f"/v1/projects/{chat['project_id']}",
                json={"project": project, "expected_version": current["version"]},
            )
            self.assertEqual(r.status_code, 200, r.text)

        self.model.during = save
        plan = {
            "reply": "Renamed.",
            "actions": [
                {
                    "tool": "upsert_character",
                    "ref": zib,
                    "name": "Model",
                    "description": "model",
                }
            ],
        }
        response = self.turn(chat, "rename", turn_id="turn-0014", plan=plan)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "story_chat_card_changed")
        after = self.client.get(f"/v1/story-chats/{chat['project_id']}").json()
        self.assertEqual(after["cards"]["characters"][0]["name"], "USER via PUT")
        self.assertEqual(after["cards"]["characters"][0]["text"], "user blue coat")

    def test_text_only_shot_edit_keeps_length_cast_place_and_props(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        shot = first["cards"]["shots"][2]
        self.assertEqual((shot["seconds"], len(shot["cast"])), (6, 2))
        plan = {
            "reply": "Changed.",
            "actions": [
                {"tool": "upsert_shot", "ref": shot["id"], "text": "They hug."}
            ],
        }
        value = self.turn(chat, "hug", turn_id="turn-0011", plan=plan).json()
        after = next(s for s in value["cards"]["shots"] if s["id"] == shot["id"])
        self.assertEqual(after["text"], "They hug.")
        self.assertEqual(after["seconds"], 6)
        self.assertEqual(after["cast"], shot["cast"])
        self.assertEqual(after["place"], shot["place"])
        self.assertEqual(after["props"], shot["props"])
        # An explicit clear is still honoured.
        plan = {
            "reply": "Cleared.",
            "actions": [
                {"tool": "upsert_shot", "ref": shot["id"], "place": None, "cast": []}
            ],
        }
        value = self.turn(chat, "empty", turn_id="turn-0012", plan=plan).json()
        after = next(s for s in value["cards"]["shots"] if s["id"] == shot["id"])
        self.assertEqual(
            (after["cast"], after["place"], after["text"]), ([], None, "They hug.")
        )

    def test_rebinding_then_removing_a_character_in_one_turn_keeps_the_new_binding(
        self,
    ):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        zib, mia = (c["id"] for c in first["cards"]["characters"])
        shot = first["cards"]["shots"][1]
        self.assertEqual(shot["cast"], [mia])
        plan = {
            "reply": "Swapped.",
            "actions": [
                {"tool": "upsert_shot", "ref": shot["id"], "cast": [zib]},
                {"tool": "remove_card", "ref": mia},
            ],
        }
        response = self.turn(chat, "swap", turn_id="turn-0013", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        after = next(
            s for s in response.json()["cards"]["shots"] if s["id"] == shot["id"]
        )
        self.assertEqual(after["cast"], [zib])

    def test_managed_project_is_refused_before_the_model_is_called(self):
        chat = self.start()
        self.actions(
            chat,
            chat["version"],
            [{"op": "journey.update", "patch": {"integration_kind": "quick_chat"}}],
        )
        response = self.turn(chat, "aliens", plan=None)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.model.calls, [])

    def test_card_deleted_during_the_call_refuses_without_writing(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        zib = first["cards"]["characters"][0]["id"]
        # Remove Zib from the shot first, then delete the card, as the UI would.
        shot = first["cards"]["shots"][2]

        def delete():
            r = self.client.post(
                f"/v1/projects/{chat['project_id']}/actions",
                json={
                    "expected_version": first["version"],
                    "actions": [
                        {
                            "op": "entity.update",
                            "entity_id": shot["id"],
                            "patch": {"data": {"cast": [shot["cast"][0]]}},
                        },
                        {"op": "entity.delete", "entity_id": zib},
                    ],
                },
            )
            self.assertEqual(r.status_code, 200, r.text)

        self.model.during = delete
        plan = {
            "reply": "Renamed.",
            "actions": [
                {
                    "tool": "upsert_character",
                    "ref": zib,
                    "name": "Zab",
                    "description": "x",
                }
            ],
        }
        response = self.turn(chat, "rename zib", turn_id="turn-0003", plan=plan)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "story_chat_card_missing")
        after = self.client.get(f"/v1/story-chats/{chat['project_id']}").json()
        self.assertEqual(after["version"], first["version"] + 1)
        self.assertEqual(len(after["messages"]), 2)

    def test_removing_a_character_cleans_shots_and_proposal_is_advice_only(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        zib = first["cards"]["characters"][0]["id"]
        before = self.jobs()
        plan = {
            "reply": "Removed Zib. Ready to make the shots?",
            "actions": [
                {"tool": "remove_card", "ref": zib},
                {"tool": "propose_generate", "shots": "all"},
            ],
        }
        response = self.turn(chat, "no alien", turn_id="turn-0004", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual([c["name"] for c in value["cards"]["characters"]], ["Mia"])
        self.assertTrue(all(zib not in s["cast"] for s in value["cards"]["shots"]))
        self.assertEqual(
            value["turn"]["proposal"],
            {
                "kind": "generate",
                "shot_ids": [s["id"] for s in value["cards"]["shots"]],
            },
        )
        self.assertEqual(self.jobs(), before)

    def test_reply_format_is_enforced_and_picture_is_only_proposed(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        self.assertEqual(self.model.configs[-1]["responseMimeType"], "application/json")
        zib = first["cards"]["characters"][0]["id"]
        before = self.jobs()
        plan = {
            "reply": "Removed Zib. Ready to make the shots?",
            "actions": [
                {"tool": "remove_card", "ref": zib},
                {"tool": "propose_generate", "shots": "all"},
            ],
        }
        response = self.turn(chat, "no alien", turn_id="turn-0004", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual([c["name"] for c in value["cards"]["characters"]], ["Mia"])
        self.assertTrue(all(zib not in s["cast"] for s in value["cards"]["shots"]))
        self.assertEqual(
            value["turn"]["proposal"],
            {
                "kind": "generate",
                "shot_ids": [s["id"] for s in value["cards"]["shots"]],
            },
        )
        self.assertEqual(self.jobs(), before)

    def test_reply_format_is_enforced_and_picture_is_only_proposed(self):
        chat = self.start()
        first = self.turn(chat, "aliens").json()
        self.assertEqual(self.model.configs[-1]["responseMimeType"], "application/json")
        zib = first["cards"]["characters"][0]["id"]
        before = self.jobs()
        plan = {
            "reply": "I can redraw Zib; confirm to spend credits.",
            "actions": [
                {"tool": "propose_image", "ref": zib},
                {"tool": "propose_generate", "shots": ["all"]},
            ],
        }
        value = self.turn(
            chat, "redraw zib and make it", turn_id="turn-0020", plan=plan
        ).json()
        proposal = value["turn"]["proposal"]
        self.assertEqual(proposal["picture_card_ids"], [zib])
        self.assertEqual(len(proposal["shot_ids"]), 3)
        self.assertEqual(self.jobs(), before)
        project = self.client.get(f"/v1/projects/{chat['project_id']}").json()[
            "project"
        ]
        self.assertFalse(any(e["type"] == "image" for e in project["entities"]))

    def test_model_failure_writes_nothing(self):
        chat = self.start()
        self.model.plans.append(ChatError("upstream_timeout", "timeout", 502))
        response = self.turn(chat, "aliens", plan=None)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"]["code"], "assistant_upstream_timeout"
        )
        after = self.client.get(f"/v1/story-chats/{chat['project_id']}").json()
        self.assertEqual(after["version"], chat["version"])
        self.assertEqual(after["messages"], [])

    def test_prose_without_a_plan_is_a_reply_only(self):
        chat = self.start()
        response = self.turn(
            chat, "hello", plan="Hi! What story would you like to make?"
        )
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(
            value["messages"][-1]["text"], "Hi! What story would you like to make?"
        )
        self.assertEqual(value["cards"]["shots"], [])

    def test_unknown_tools_and_bad_refs_are_refused(self):
        chat = self.start()
        for plan in (
            {"reply": "x", "actions": [{"tool": "create_job"}]},
            {
                "reply": "x",
                "actions": [
                    {
                        "tool": "upsert_shot",
                        "ref": "new:a",
                        "text": "t",
                        "cast": ["new:ghost"],
                    }
                ],
            },
        ):
            response = self.turn(
                chat, "go", turn_id="turn-bad-" + str(len(self.model.calls)), plan=plan
            )
            self.assertIn(response.status_code, (409, 502), response.text)
        after = self.client.get(f"/v1/story-chats/{chat['project_id']}").json()
        self.assertEqual(after["version"], chat["version"])

    def test_another_account_cannot_read_or_write(self):
        chat = self.start()
        self.client.post("/api/auth/logout")
        self.login("supervan")
        self.assertEqual(
            self.client.get(f"/v1/story-chats/{chat['project_id']}").status_code, 404
        )
        self.assertEqual(self.turn(chat, "x", plan=None).status_code, 404)
        self.assertEqual(self.model.calls, [])

    def test_ordinary_project_is_not_a_story_chat(self):
        self.login()
        created = self.client.post("/v1/projects", json={"title": "Plain"}).json()
        response = self.client.get(f"/v1/story-chats/{created['id']}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "story_chat_not_found")

    def test_input_bounds(self):
        chat = self.start()
        for body in (
            {"turn_id": "short", "text": "x"},
            {"turn_id": "turn-0001", "text": " "},
            {"turn_id": "turn-0001", "text": "x" * 4001},
            {"turn_id": "turn-0001"},
        ):
            response = self.client.post(
                f"/v1/story-chats/{chat['project_id']}/turns", json=body
            )
            self.assertEqual(response.status_code, 422, body)
        self.assertEqual(self.model.calls, [])


if __name__ == "__main__":
    unittest.main()
