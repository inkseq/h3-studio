"""Real HTTP lifecycle/isolation, isolated storage/DB; never a provider call."""
import unittest

import test_platform_api as api_fixture


PREFIX = "/v1/quick-chat/sessions"


class CreationHistoryRoutesTests(unittest.TestCase):
    setUp = api_fixture.ApiTests.setUp
    login = api_fixture.ApiTests.login

    def create_session(self, key="history-route-session"):
        response = self.client.post(PREFIX, json={"title": "History retention fixture", "is_test": True},
                                    headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["session"]

    def preview(self, action, session):
        response = self.client.post(PREFIX+"/history/preview", json={"action": action,
            "sessions": [{"id": session["id"], "expected_version": session["version"]}]})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertTrue(all(item["eligible"] for item in value["items"]), value)
        return value

    def confirm(self, preview, key):
        response = self.client.post(PREFIX+"/history/actions",
            json={"preview_id": preview["preview_id"], "confirmed": True},
            headers={"Idempotency-Key": key})
        self.assertLess(response.status_code, 300, response.text)
        return response.json()

    def test_trash_restore_preserves_saved_card_and_persistent_settings(self):
        self.login()
        session = self.create_session()
        saved_settings = session["next_settings"]
        card_response = self.client.post(PREFIX+"/"+session["id"]+"/cards", json={
            "recipe_id": "h3-base-fl2va-v1", "prompt": "A synthetic blue cup on a desk.",
            "controls": {"duration": 5, "resolution": "480P"}, "copies": 1, "inputs": {}},
            headers={"Idempotency-Key": "history-route-card"})
        self.assertEqual(card_response.status_code, 201, card_response.text)
        card_id = card_response.json()["card"]["id"]
        card_path = PREFIX+"/"+session["id"]+"/cards/"+card_id
        saved_card = self.client.get(card_path).json()
        session = self.client.get(PREFIX+"/"+session["id"]).json()["session"]
        preview = self.preview("trash", session)
        receipt = self.confirm(preview, "history-route-trash")
        self.assertEqual(receipt["state"], "completed")
        replay = self.confirm(preview, "history-route-trash")
        self.assertEqual(replay["action_id"], receipt["action_id"])
        observed = self.client.get(PREFIX+"/history/actions/"+receipt["action_id"])
        self.assertEqual(observed.status_code, 200, observed.text)
        self.assertEqual(observed.json()["action_id"], receipt["action_id"])
        active = self.client.get(PREFIX).json()
        self.assertNotIn(session["id"], [item["id"] for item in active["sessions"]])
        trash = self.client.get(PREFIX, params={"view": "trash"}).json()
        trashed = next(item for item in trash["sessions"] if item["id"] == session["id"])
        self.assertEqual(trashed["trash_state"], "trashed")
        self.assertTrue(trashed["is_test"])
        restored = self.confirm(self.preview("restore", trashed), "history-route-restore")
        self.assertEqual(restored["state"], "completed")
        self.assertEqual(self.client.get(card_path).json(), saved_card)
        current = self.client.get(PREFIX+"/"+session["id"]).json()["session"]
        self.assertEqual(current["next_settings"], saved_settings)
        self.assertEqual(current["trash_state"], "active")
        hidden = self.client.get(PREFIX, params={"hide_tests": True}).json()
        self.assertNotIn(session["id"], [item["id"] for item in hidden["sessions"]])

    def test_preview_and_action_are_bound_to_original_owner(self):
        self.login()
        session = self.create_session()
        preview = self.preview("trash", session)
        action = self.confirm(preview, "history-owner-action")
        self.login("supervan")
        self.assertEqual(self.client.get(PREFIX+"/history/actions/"+action["action_id"]).status_code, 404)
        denied = self.client.post(PREFIX+"/history/actions",
            json={"preview_id": preview["preview_id"], "confirmed": True},
            headers={"Idempotency-Key": "history-other-owner-action"})
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assertEqual(self.client.get(PREFIX, params={"view": "trash"}).json()["sessions"], [])

    def test_public_contract_exposes_guarded_workflow_without_authentication(self):
        guide = self.client.get("/for-agents/guide.json")
        self.assertEqual(guide.status_code, 200)
        history = guide.json()["quick_chat"]["creation_history"]
        self.assertEqual(history["preview"]["path"], PREFIX+"/history/preview")
        self.assertEqual(history["confirm"]["path"], PREFIX+"/history/actions")
        self.assertIn("jobs:write", history["scopes"]["purge"])
        self.assertIn("pending", history["unknown"])
        self.assertIn("review_hold", history["retention"])


if __name__ == "__main__":
    unittest.main()
