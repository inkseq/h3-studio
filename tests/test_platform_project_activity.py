"""Committed editor activity, privacy, isolation and paging; no provider calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

from studio_platform.auth import Principal, digest
from studio_platform.guided import Guided
from studio_platform.project_activity import activity
from studio_platform.repository import Conflict, Scope
import test_platform_api as api_fixtures
import test_platform_guided as guided_fixtures
from test_platform_api import project


class ProjectActivityTests(unittest.TestCase):
    setUp = api_fixtures.ApiTests.setUp
    login = api_fixtures.ApiTests.login
    setup_project = api_fixtures.ApiTests.setup_project
    key = guided_fixtures.GuidedTests.key
    edit = guided_fixtures.GuidedTests.edit

    def history(self, ident="story-one", **params):
        response = self.client.get(f"/v1/projects/{ident}/activity", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.json()

    def test_create_and_save_visible_without_document_content(self):
        created = self.setup_project()
        first = self.history()["items"][0]
        self.assertEqual(first["event_type"], "project.created")
        self.assertEqual(first["actor_kind"], "browser")
        self.assertEqual(first["actor_label"], "superdan")
        self.assertEqual(first["project_version"], 1)
        self.assertTrue(first["occurred_at"].endswith("Z"))
        doc = copy.deepcopy(created["project"])
        secret_prompt = "PRIVATE STORY PROMPT NOT AN ACTIVITY SUMMARY"
        doc["entities"][-1]["data"]["prompt"] = secret_prompt
        response = self.client.put("/v1/projects/story-one", json={"project": doc, "expected_version": 1})
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.history()["items"]
        self.assertEqual([row["project_version"] for row in rows], [2, 1])
        self.assertEqual(rows[0]["event_type"], "project.saved")
        self.assertEqual(rows[0]["target_entity_ids"], ["shot-one"])
        self.assertNotIn(secret_prompt, json.dumps(rows))
        self.assertNotIn(doc["title"], json.dumps(rows, ensure_ascii=False))
        stale = self.client.put("/v1/projects/story-one", json={"project": doc, "expected_version": 1})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(len(self.history()["items"]), 2)

    def test_named_key_actor_survives_revocation_without_key_material(self):
        self.setup_project()
        meta, headers = self.key()
        token = headers["Authorization"][7:]
        response = self.edit([{"op": "entity.update", "entity_id": "shot-one", "patch": {
            "data": {"prompt": "PRIVATE PROMPT"}, "description": "PRIVATE DESCRIPTION"}}], headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        row = self.history()["items"][0]
        self.assertEqual((row["actor_kind"], row["actor_label"]), ("api_key", "Test agent"))
        self.assertEqual(row["operations"], ["entity.update"])
        self.assertEqual(row["target_entity_ids"], ["shot-one"])
        self.client.delete("/v1/api-keys/" + meta["id"]).raise_for_status()
        self.assertEqual(self.history()["items"][0], row)
        with self.app.state.repository.engine.connect() as conn:
            stored = json.dumps([dict(r) for r in conn.execute(select(activity)).mappings()])
        for private in (token, digest(token), meta["id"], "PRIVATE PROMPT", "PRIVATE DESCRIPTION"):
            self.assertNotIn(private, stored)

    def test_static_machine_does_not_leak_internal_client_identity(self):
        self.setup_project()
        principal = Principal("superdan", "client:private-internal-identifier", True,
            ("story-one",), ("projects:read", "projects:write"))
        self.app.state.guided.mutate(principal, "story-one", {"expected_version": 1,
            "actions": [{"op": "project.update", "patch": {"title": "Changed"}}]})
        row = self.history()["items"][0]
        self.assertEqual((row["actor_kind"], row["actor_label"]), ("api_key", "superdan"))
        self.assertNotIn("private-internal-identifier", json.dumps(row))

    def test_create_and_edit_idempotency_replay_does_not_duplicate(self):
        self.login()
        _, headers = self.key()
        headers = {**headers, "Idempotency-Key": "create-story"}
        body = {"title": "Story", "id": "new-story"}
        first = self.client.post("/v1/projects", json=body, headers=headers)
        self.assertEqual(first.status_code, 201)
        replay = self.client.post("/v1/projects", json=body, headers=headers)
        self.assertEqual(replay.json(), first.json())
        actions = [{"op": "project.update", "patch": {"title": "Revised"}}]
        self.assertEqual(self.edit(actions, ident="new-story", headers=headers, key="edit-story").status_code, 200)
        self.assertEqual(self.edit(actions, ident="new-story", headers=headers, key="edit-story").status_code, 200)
        self.assertEqual(len(self.history("new-story")["items"]), 2)
        bad = self.edit([{ "op": "project.update", "patch": {"title": "Mismatch"}}],
            ident="new-story", headers=headers, key="edit-story")
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(len(self.history("new-story")["items"]), 2)

    def test_invalid_action_and_activity_failure_roll_back_document(self):
        self.setup_project()
        failed = self.edit([{ "op": "entity.create", "entity": {"id": "orphan", "type": "shot", "parentId": "absent"}}])
        self.assertEqual(failed.status_code, 422)
        self.assertEqual(len(self.history()["items"]), 1)
        # Fail after both document and activity INSERT but before transaction commit.
        with patch.object(self.app.state.guided, "envelope", side_effect=RuntimeError("injected rollback")):
            with self.assertRaises(RuntimeError):
                self.edit([{ "op": "project.update", "patch": {"title": "Not committed"}}], key="retry-safe")
        self.assertEqual(len(self.history()["items"]), 1)
        self.assertEqual(self.client.get("/v1/projects/story-one").json()["version"], 1)
        self.assertEqual(self.edit([{ "op": "project.update", "patch": {"title": "Not committed"}}], key="retry-safe").status_code, 200)
        self.assertEqual(len(self.history()["items"]), 2)

    def test_full_save_rolls_back_if_activity_write_fails(self):
        created = self.setup_project()
        doc = copy.deepcopy(created["project"])
        doc["title"] = "Failed save"
        with patch("studio_platform.guided.append_activity", side_effect=RuntimeError("injected rollback")):
            with self.assertRaises(RuntimeError):
                self.client.put("/v1/projects/story-one", json={"project": doc, "expected_version": 1})
        self.assertEqual(len(self.history()["items"]), 1)
        self.assertEqual(self.client.get("/v1/projects/story-one").json()["project"]["title"], created["project"]["title"])

    def test_account_project_scope_and_anonymous_isolation(self):
        self.assertEqual(self.client.get("/v1/projects/story-one/activity").status_code, 401)
        self.setup_project()
        self.client.post("/v1/projects", json={"title": "Other", "id": "other"}).raise_for_status()
        _, headers = self.key(scopes=["projects:read"], all_projects=False, project_ids=["story-one"])
        self.assertEqual(self.client.get("/v1/projects/story-one/activity", headers=headers).status_code, 200)
        for ident in ("other", "absent"):
            self.assertEqual(self.client.get(f"/v1/projects/{ident}/activity", headers=headers).status_code, 404)
        _, no_read = self.key(scopes=["jobs:read"], all_projects=False, project_ids=["story-one"])
        self.assertEqual(self.client.get("/v1/projects/story-one/activity", headers=no_read).status_code, 404)
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/projects/story-one/activity").status_code, 404)
        self.client.post("/v1/projects", json={"title": "Same ID other owner", "id": "story-one"}).raise_for_status()
        rows = self.history()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_label"], "supervan")

    def test_tenant_isolation_even_with_identical_owner_project(self):
        self.setup_project()
        with self.app.state.repository.engine.begin() as conn:
            row = dict(conn.execute(select(activity)).mappings().one())
            row.update(id="other-tenant-event", tenant_id="other-tenant", summary="OTHER TENANT")
            conn.execute(activity.insert().values(**row))
        rows = self.history()["items"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("OTHER TENANT", json.dumps(rows))

    def test_stable_keyset_pagination_while_new_events_arrive(self):
        self.setup_project()
        for version in (1, 2, 3):
            self.assertEqual(self.edit([{ "op": "project.update", "patch": {"title": str(version)}}], version=version).status_code, 200)
        page = self.history(limit=2)
        self.assertEqual([i["project_version"] for i in page["items"]], [4, 3])
        self.assertEqual(page["next_before_version"], 3)
        self.edit([{ "op": "project.update", "patch": {"title": "New"}}], version=4).raise_for_status()
        tail = self.history(limit=2, before_version=page["next_before_version"])
        self.assertEqual([i["project_version"] for i in tail["items"]], [2, 1])
        self.assertIsNone(tail["next_before_version"])
        for params in ({"limit": 0}, {"limit": 101}, {"before_version": 0}, {"before_version": "bad"}):
            self.assertEqual(self.client.get("/v1/projects/story-one/activity", params=params).status_code, 422)

    def test_old_projects_are_not_backfilled_on_schema_creation(self):
        self.login()
        repo = self.app.state.repository
        # This fixture database is disposable. Simulate an existing deployment
        # from before the additive activity table, retaining all other tables.
        activity.drop(repo.engine)
        repo.put_document(Scope(self.settings.tenant_id, "superdan", "__projects", "browser:superdan"),
            "project", "legacy", project("legacy"))
        Guided(self.app)
        self.assertEqual(repo.get_document(Scope(self.settings.tenant_id, "superdan", "__projects"),
            "project", "legacy")["payload"], project("legacy"))
        self.assertEqual(self.history("legacy")["items"], [])
        self.edit([{ "op": "project.update", "patch": {"title": "First observed edit"}}], ident="legacy").raise_for_status()
        self.assertEqual([i["project_version"] for i in self.history("legacy")["items"]], [2])

    def test_changed_targets_include_generated_ids_and_linked_entities(self):
        self.setup_project()
        response = self.edit([
            {"op": "entity.create", "entity": {"type": "note", "title": "Do not include title"}},
            {"op": "entity.create", "entity": {"id": "reference", "type": "note", "title": "Reference"}},
            {"op": "link.create", "link": {"source": "reference", "target": "shot-one", "role": "reference"}},
        ])
        self.assertEqual(response.status_code, 200, response.text)
        new_id = response.json()["project"]["entities"][-2]["id"]
        row = self.history()["items"][0]
        self.assertTrue({new_id, "shot-one", "reference"} <= set(row["target_entity_ids"]))
        self.assertEqual(row["operations"], ["entity.create", "link.create"])
        self.assertNotIn("Do not include title", json.dumps(row))

    def test_large_target_list_is_bounded_and_deletion_is_explicit(self):
        self.login()
        doc = project()
        template = doc["entities"][0]
        for index in range(130):
            doc["entities"].append({**template, "id": f"ch-{index}", "title": str(index)})
        self.client.post("/v1/projects", json={"project": doc}).raise_for_status()
        row = self.history()["items"][0]
        self.assertEqual(len(row["target_entity_ids"]), 100)
        self.assertEqual(row["target_count"], 133)
        self.edit([{ "op": "entity.delete", "entity_id": "chapter-one", "cascade": True}]).raise_for_status()
        row = self.history()["items"][0]
        self.assertEqual(row["operations"], ["entity.delete"])
        self.assertTrue({"chapter-one", "scene-one", "shot-one"} <= set(row["target_entity_ids"]))

    def test_concurrent_same_version_produces_one_activity(self):
        self.setup_project()
        service = self.app.state.guided
        principal = Principal("superdan", "browser:superdan")
        def save(title):
            try:
                service.mutate(principal, "story-one", {"expected_version": 1,
                    "actions": [{"op": "project.update", "patch": {"title": title}}]}, key="concurrent")
                return "ok"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, ("One", "Two")))
        self.assertCountEqual(results, ["ok", "conflict"])
        self.assertEqual([i["project_version"] for i in self.history()["items"]], [2, 1])


if __name__ == "__main__":
    unittest.main()
