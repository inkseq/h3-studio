"""Public revision contract on isolated SQL/API data; no providers or inference.

The one confirmation fixture only queues the explicit CPU mock backend. No
worker runs, media generation, paid naming, or external API calls occur here.
"""
import copy
import contextlib
import json
from pathlib import Path
import re
import time
from unittest import mock

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select

from studio_platform.api import create_app
from studio_platform.auth import API_SCOPES
from studio_platform.capabilities import VERSION
from studio_platform.quick_chat import objects, operations, events
from studio_platform.repository import (
    documents, plans, jobs, attempts, artifacts, budget_accounts,
    budget_reservations, outbox,
)
from studio_platform.settings import Settings
from test_platform_api import png
from test_platform_repository import LedgerCase
from test_agent_skill_quick_chat import helper


PRUNED = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
REVISION_ROUTE = "/v1/quick-chat/sessions/{session_id}/cards/{card_id}/revisions"


class QuickChatRevisionContractTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.app = create_app(Settings(Path(self.temp.name)/"api", auth_mode="local-test",
            database_url=self.url, generation_enabled=True, execution_backend="mock"), repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
        self.session = self.write("/v1/quick-chat/sessions", {"title": "Revision contract fixture"}, "session")["session"]
        self.base = "/v1/quick-chat/sessions/"+self.session["id"]
        self.body = {"prompt": "A blue cup on a table.", "recipe_id": "h3-base-fl2va-v1",
            "controls": {"duration": 5, "resolution": "480P", "seed": "7"}, "inputs": {}, "copies": 1}
        self.created = self.write(self.base+"/cards", {**self.body, "title": "Blue cup"}, "create-card")
        self.path = self.base+"/cards/"+self.created["card"]["id"]+"/revisions"

    def write(self, path, body, key, *, headers=None, method="POST", status=201):
        response = self.client.request(method, path, json=body,
            headers={"Idempotency-Key": key, **(headers or {})})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def revision_body(self, *, version=1, **changes):
        return {**copy.deepcopy(self.body), "expected_card_version": version, **changes}

    def ledger_snapshot(self):
        """Compare entire relevant rows, not just a unchanged card counter."""
        result = {}
        with self.repo.engine.connect() as conn:
            for table in (objects, operations, events, documents, plans, jobs, attempts,
                          artifacts, budget_accounts, budget_reservations, outbox):
                rows = [dict(row) for row in conn.execute(select(table)).mappings()]
                result[table.name] = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
        return result

    def assert_field_error(self, response, path, reason, *, status=422, code="invalid_request"):
        self.assertEqual(response.status_code, status, response.text)
        envelope = response.json()
        self.assertEqual(envelope["code"], code)
        self.assertIsInstance(envelope["message"], str)
        self.assertIs(envelope["retryable"], False)
        self.assertNotIn("detail", envelope)
        errors = envelope["field_errors"]
        self.assertLessEqual(len(errors), 12)
        self.assertIn({"path": path, "reason": reason}, errors)
        for error in errors:
            self.assertEqual(set(error), {"path", "reason"})
            self.assertIsInstance(error["path"], list)
            self.assertTrue(all(isinstance(part, (str, int)) for part in error["path"]))
            self.assertIn(error["reason"], {"missing_field", "type_mismatch", "unknown_field", "invalid_value", "stale_version"})
        return envelope

    def rejected(self, body, key):
        before = self.ledger_snapshot()
        response = self.client.post(self.path, json=body, headers={"Idempotency-Key": key})
        self.assertEqual(self.ledger_snapshot(), before, response.text)
        self.assertEqual(self.client.get(self.base+"/cards/"+self.created["card"]["id"]).json(), self.created["card"])
        self.assertEqual(self.client.get(self.base+"/revisions/"+self.created["revision"]["id"]).json(), self.created["revision"])
        return response

    def test_missing_version_and_adjacent_session_alias_are_repairable_not_stale(self):
        response = self.rejected(copy.deepcopy(self.body), "missing-version")
        self.assert_field_error(response, ["body", "expected_card_version"], "missing_field")
        response = self.rejected({**self.body, "expected_version": 1}, "session-version-alias")
        envelope = self.assert_field_error(response, ["body", "expected_card_version"], "missing_field")
        self.assertIn({"path": ["body"], "reason": "unknown_field"}, envelope["field_errors"])

    def test_invalid_version_never_mutates_any_authoring_or_execution_ledger(self):
        for index, value in enumerate((None, "1", True, False, 1.0, [], {}, 0, -1)):
            with self.subTest(version=value):
                reason = "invalid_value" if type(value) is int else "type_mismatch"
                response = self.rejected(self.revision_body(version=value), "bad-version-"+str(index))
                self.assert_field_error(response, ["body", "expected_card_version"], reason)

    def test_required_fields_publish_safe_missing_paths(self):
        for field in ("prompt", "recipe_id", "controls", "inputs", "copies"):
            with self.subTest(field=field):
                body = self.revision_body()
                del body[field]
                response = self.rejected(body, "missing-"+field)
                self.assert_field_error(response, ["body", field], "missing_field")

    def test_invalid_required_types_and_values_do_not_leak_inputs(self):
        cases = (("prompt", None, "type_mismatch"), ("prompt", {"secret": "private-prompt-marker"}, "type_mismatch"),
            ("prompt", " ", "invalid_value"), ("recipe_id", ["private-recipe-marker"], "invalid_value"),
            ("recipe_id", "private-recipe-marker", "invalid_value"), ("controls", "private-controls-marker", "type_mismatch"),
            ("inputs", [], "type_mismatch"), ("copies", True, "type_mismatch"),
            ("copies", "2", "type_mismatch"), ("copies", 0, "invalid_value"), ("copies", 2147483648, "invalid_value"))
        for index, (field, value, reason) in enumerate(cases):
            with self.subTest(field=field, value=value):
                response = self.rejected(self.revision_body(**{field: value}), "bad-field-"+str(index))
                self.assert_field_error(response, ["body", field], reason)
                self.assertNotIn("private-", response.text)

    def test_unknown_unsafe_field_names_and_values_are_never_echoed(self):
        for index, body in enumerate((
            self.revision_body(**{"https://secret.example/token=private-key-marker": "private-value-marker"}),
            self.revision_body(controls={"duration": 5, "private-control-name": "private-value-marker"}),
            self.revision_body(inputs={"private-input-name": "private-value-marker"}),
            self.revision_body(**{f"private-field-{n}": "private-value-marker" for n in range(80)}),
        )):
            with self.subTest(index=index):
                response = self.rejected(body, "unknown-"+str(index))
                envelope = self.assert_field_error(response, ["body"] if index in (0, 3) else
                    ["body", "controls" if index == 1 else "inputs"], "unknown_field")
                self.assertLess(len(json.dumps(envelope)), 4000)
                self.assertNotIn("private-", response.text)
                self.assertNotIn("secret.example", response.text)

    def test_non_object_body_uses_the_public_error_envelope(self):
        for index, body in enumerate((None, [], "private-body-marker", 7)):
            with self.subTest(body=body):
                response = self.rejected(body, "non-object-"+str(index))
                self.assert_field_error(response, ["body"], "type_mismatch" if body is not None else "missing_field")
                self.assertNotIn("private-body-marker", response.text)

    def test_minimal_revision_needs_no_optional_lineage_or_title(self):
        body = self.revision_body()
        self.assertTrue({"source_revision_id", "turn_id", "title", "deployment_profile_id"}.isdisjoint(body))
        revised = self.write(self.path, body, "minimal-revision")
        self.assertEqual(revised["card"]["version"], 2)
        self.assertEqual(revised["revision"]["version"], 2)
        self.assertIsNone(revised["revision"]["source_revision_id"])
        self.assertIsNone(revised["revision"]["turn_id"])
        self.assertEqual(revised["revision"]["title"], "我的视频")
        self.assertEqual(self.client.get(self.base+"/revisions/"+self.created["revision"]["id"]).json(), self.created["revision"])
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])
            self.assertEqual(conn.execute(select(budget_reservations.c.id)).all(), [])

    def test_explicit_lineage_title_settings_and_reference_snapshot_are_preserved(self):
        upload = self.client.post(self.base+"/assets", data={"client_asset_id": "synthetic-blue-cup"},
            files={"file": ("cup.png", png(), "image/png")})
        self.assertEqual(upload.status_code, 201, upload.text)
        asset = upload.json()
        current = self.write(self.base+"/cards", {**self.body, "deployment_profile_id": PRUNED,
            "controls": {**self.body["controls"], "steps": 20}, "inputs": {"first_frame": {"asset_id": asset["asset_id"]}},
            "copies": 2, "title": "Blue cup selected settings"}, "profile-card")
        original = copy.deepcopy(current["revision"])
        body = {key: copy.deepcopy(original[key]) for key in
            ("deployment_profile_id", "recipe_id", "controls", "inputs", "copies", "title")}
        body.update(prompt="A blue cup on a table, seen from above.", expected_card_version=current["card"]["version"],
            source_revision_id=original["id"])
        path = self.base+"/cards/"+current["card"]["id"]+"/revisions"
        revised = self.write(path, body, "profile-revision")
        for field in ("deployment_profile_id", "recipe_id", "controls", "inputs", "copies", "title", "provenance"):
            self.assertEqual(revised["revision"][field], original[field], field)
        self.assertEqual(revised["revision"]["source_revision_id"], original["id"])
        self.assertEqual(revised["revision"]["items"][0]["seed"], "7")
        self.assertNotIn(revised["revision"]["items"][1]["seed"], {item["seed"] for item in original["items"]})
        self.assertEqual(self.client.get(self.base+"/revisions/"+original["id"]).json(), original)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])

    def test_genuine_stale_version_is_409_with_specific_field_reason(self):
        revised = self.write(self.path, self.revision_body(), "advance-card")
        before = self.ledger_snapshot()
        response = self.client.post(self.path, json=self.revision_body(prompt="An obsolete cup edit."),
            headers={"Idempotency-Key": "stale-version"})
        self.assert_field_error(response, ["body", "expected_card_version"], "stale_version",
            status=409, code="version_conflict")
        self.assertEqual(self.ledger_snapshot(), before)
        self.assertEqual(self.client.get(self.base+"/cards/"+self.created["card"]["id"]).json(), revised["card"])

    def test_same_key_replay_keeps_original_revision_after_later_card_edit(self):
        body = self.revision_body(prompt="A blue cup on a table, side view.")
        revised = self.write(self.path, body, "stable-revision")
        replay = self.write(self.path, body, "stable-revision")
        self.assertEqual(replay, revised)
        later = self.write(self.path, self.revision_body(version=2, prompt="A blue cup, top view."), "later-revision")
        before = self.ledger_snapshot()
        replay = self.write(self.path, body, "stable-revision")
        self.assertEqual(replay["revision"], revised["revision"])
        self.assertEqual(replay["card"], later["card"])
        self.assertEqual(self.ledger_snapshot(), before)
        response = self.client.post(self.path, json={**body, "prompt": "Different request under old key."},
            headers={"Idempotency-Key": "stable-revision"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "idempotency_conflict")
        with self.assertRaisesRegex(ValueError, "code=idempotency_conflict"):
            helper.check_response(response)
        self.assertEqual(self.ledger_snapshot(), before)

    def test_successful_revision_preserves_prior_submission_and_original_jobs(self):
        old = copy.deepcopy(self.created["revision"])
        preflight = self.write(self.base+"/revisions/"+old["id"]+"/preflights",
            {"capabilities_version": VERSION, "revision_hash": old["input_hash"]}, "cpu-preflight")
        self.assertEqual(preflight["status"], "ready", preflight)
        submitted = self.write(self.base+"/revisions/"+old["id"]+"/submissions",
            {"revision_hash": old["input_hash"], "preflight_id": preflight["id"], "confirmed": True},
            "cpu-queue-only", status=202)
        self.assertTrue(all(item["status"] == "queued" for item in submitted["items"]))
        with self.repo.engine.connect() as conn:
            original_jobs = [dict(row) for row in conn.execute(select(jobs)).mappings()]
            original_reservations = [dict(row) for row in conn.execute(select(budget_reservations)).mappings()]
        self.write(self.path, self.revision_body(title="Blue cup", source_revision_id=old["id"],
            prompt="A blue cup on a table, closer framing."), "after-confirm-revision")
        self.assertEqual(self.client.get(self.base+"/revisions/"+old["id"]).json(), old)
        self.assertEqual(self.client.get(self.base+"/submissions/"+submitted["id"]).json(), submitted)
        with self.repo.engine.connect() as conn:
            self.assertEqual([dict(row) for row in conn.execute(select(jobs)).mappings()], original_jobs)
            self.assertEqual([dict(row) for row in conn.execute(select(budget_reservations)).mappings()], original_reservations)
            self.assertEqual(conn.execute(select(attempts.c.id)).all(), [])
        self.assertTrue(all(job["execution_plan"]["backend"] == "mock" for job in original_jobs))

    def native_card(self):
        upload = self.client.post(self.base+"/assets", data={"client_asset_id": "guide-synthetic-reference"},
            files={"file": ("blue-cup.png", png(), "image/png")})
        self.assertEqual(upload.status_code, 201, upload.text)
        return self.write(self.base+"/cards", {**self.body, "deployment_profile_id": PRUNED,
            "controls": {**self.body["controls"], "steps": 20}, "copies": 2,
            "inputs": {"first_frame": {"asset_id": upload.json()["asset_id"]}},
            "title": "Blue cup guide example"}, "guide-card")

    def test_published_structured_and_downloaded_python_examples_work_for_agent_without_jobs(self):
        selected = self.native_card()
        key = self.client.post("/v1/api-keys", json={"name": "Isolated revision example",
            "scopes": sorted(API_SCOPES), "all_projects": True, "project_ids": [], "expires_in_days": 1})
        self.assertEqual(key.status_code, 201, key.text)
        headers = {"Authorization": "Bearer "+key.json()["api_key"]}
        self.client.post("/api/auth/logout").raise_for_status()
        guide = self.client.get("/for-agents/guide.json").json()["quick_chat"]
        schema_response = self.client.get("/v1/quick-chat/schema", headers=headers)
        self.assertEqual(schema_response.status_code, 200, schema_response.text)
        schema = schema_response.json()
        self.assertEqual(schema["agent_contract"], guide)
        examples = guide["examples"]
        card_path = examples["read_card"]["path"].format(session_id=self.session["id"], card_id=selected["card"]["id"])
        card = self.client.request(examples["read_card"]["method"], card_path, headers=headers).json()
        revision_path = examples["read_revision"]["path"].format(session_id=self.session["id"], current_revision_id=card["current_revision_id"])
        revision = self.client.request(examples["read_revision"]["method"], revision_path, headers=headers).json()
        self.assertEqual(revision["id"], card["current_revision_id"])
        substitutions = {"{current_card_version}": card["version"], "{new_complete_prompt}": "A blue cup on a table."}
        substitutions.update({"{current_revision."+field+"}": revision[field] for field in
            ("deployment_profile_id", "recipe_id", "controls", "inputs", "copies", "title", "id")})
        body = {field: copy.deepcopy(substitutions.get(value, value)) if isinstance(value, str) else copy.deepcopy(value)
            for field, value in examples["revision"]["body"].items()}
        self.assertIs(type(body["expected_card_version"]), int)
        self.assertIs(type(body["copies"]), int)
        self.assertIsInstance(body["controls"], dict)
        self.assertIsInstance(body["inputs"], dict)
        self.assertEqual(body["deployment_profile_id"], PRUNED)
        path = examples["revision"]["path"].format(session_id=self.session["id"], card_id=card["id"])
        service = self.app.state.quick_chat
        naming = mock.Mock(side_effect=AssertionError("authoring must not call paid naming"))
        service.title_generator = naming
        with contextlib.ExitStack() as stack:
            for name in ("preflight", "create_planned", "enqueue"):
                stack.enter_context(mock.patch.object(service.hooks, name,
                    side_effect=AssertionError("revision example must remain authoring-only")))
            structured = self.write(path, body, "structured-example", headers=headers, method=examples["revision"]["method"])
            current = self.client.get(card_path, headers=headers).json()
            current_revision = self.client.get(self.base+"/revisions/"+current["current_revision_id"], headers=headers).json()
            # Execute exactly the newly published Python body builder using
            # synthetic read receipts and files in this test's temp directory.
            downloaded = self.client.get("/for-agents/guide.md").text
            blocks = re.findall(r"```python\n(.*?)\n```", downloaded, re.DOTALL)
            builders = [block for block in blocks if '"expected_card_version": card["version"]' in block]
            self.assertEqual(len(builders), 1)
            fixture = Path(self.temp.name)/"published-example"
            fixture.mkdir()
            (fixture/"card-current.json").write_text(json.dumps(current), encoding="utf-8")
            (fixture/"revision-current.json").write_text(json.dumps(current_revision), encoding="utf-8")
            real_path = Path
            with mock.patch("pathlib.Path", side_effect=lambda filename: fixture/real_path(filename)):
                exec(compile(builders[0], "published-revision-example", "exec"), {})
            documented_body = json.loads((fixture/"revision-request.json").read_text(encoding="utf-8"))
            documented = self.write(path, documented_body, "downloaded-example", headers=headers)
        naming.assert_not_called()
        for saved in (structured, documented):
            for field in ("deployment_profile_id", "recipe_id", "controls", "inputs", "copies", "title"):
                self.assertEqual(saved["revision"][field], revision[field], field)
        self.assertEqual(self.client.get(revision_path, headers=headers).json(), revision)
        with self.repo.engine.connect() as conn:
            for table in (jobs, plans, attempts, artifacts, budget_reservations):
                self.assertEqual(conn.execute(select(table)).all(), [], table.name)

    def test_downloaded_helper_reports_only_safe_field_diagnostics(self):
        actual = self.rejected(copy.deepcopy(self.body), "helper-missing-version")
        with self.assertRaises(ValueError) as valid:
            helper.check_response(actual)
        self.assertIn("expected_card_version", str(valid.exception))
        self.assertIn("missing_field", str(valid.exception))
        hostile = httpx.Response(422, json={"code": "invalid_request", "message": "private-message-marker",
            "field_errors": [{"path": ["body", "expected_card_version"], "reason": "missing_field"},
                {"path": ["body", "private-key-marker"], "reason": "missing_field"},
                {"path": ["body", "prompt"], "reason": "private-reason-marker"},
                {"path": ["body", "title"], "reason": "invalid_value", "value": "private-value-marker"},
                {"path": ["body", "controls", "private-nested-marker"], "reason": "unknown_field"}]})
        with self.assertRaises(ValueError) as unsafe:
            helper.check_response(hostile)
        self.assertIn("expected_card_version", str(unsafe.exception))
        self.assertNotIn("private-", str(unsafe.exception))

    @staticmethod
    def resolve_schema(document, schema):
        while "$ref" in schema:
            parts = schema["$ref"].removeprefix("#/").split("/")
            schema = document
            for part in parts:
                schema = schema[part]
        return schema

    def test_openapi_describes_strict_revision_body_and_actual_response_envelopes(self):
        spec = self.client.get("/openapi.json").json()
        operation = spec["paths"][REVISION_ROUTE]["post"]
        body = self.resolve_schema(spec, operation["requestBody"]["content"]["application/json"]["schema"])
        self.assertIs(body["additionalProperties"], False)
        self.assertEqual(set(body["required"]), {"prompt", "recipe_id", "controls", "inputs", "copies", "expected_card_version"})
        self.assertTrue({"deployment_profile_id", "source_revision_id", "turn_id", "title"} <= set(body["properties"]))
        self.assertNotIn("expected_version", body["properties"])
        self.assertEqual(body["properties"]["expected_card_version"]["type"], "integer")
        self.assertEqual(body["properties"]["expected_card_version"]["minimum"], 1)
        for field in ("controls", "inputs"):
            nested = self.resolve_schema(spec, body["properties"][field])
            self.assertIs(nested["additionalProperties"], False)
        response = self.resolve_schema(spec, operation["responses"]["201"]["content"]["application/json"]["schema"])
        self.assertTrue({"card", "revision"} <= set(response["required"]))
        for status in ("409", "422"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            self.assertNotIn("HTTPValidationError", json.dumps(schema))
            envelope = self.resolve_schema(spec, schema)
            self.assertTrue({"code", "message", "retryable"} <= set(envelope["required"]))
            self.assertIn("field_errors", envelope["properties"])


if __name__ == "__main__":
    import unittest
    unittest.main()
