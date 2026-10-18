"""New public HTTP API through the actual asset/compiler/queue services.

Isolated temporary SQL + CPU decode; mock is only an explicit test backend.
"""

import tempfile
import hashlib
import shutil
import time
import threading
import copy
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from studio_platform.api import create_app
from studio_platform.auth import API_SCOPES, Principal
from studio_platform.capabilities import VERSION
from studio_platform.repository import jobs, Conflict
from studio_platform.settings import Settings
from studio_platform.quick_chat import objects as quick_chat_objects
from test_platform_api import png
from test_platform_repository import LedgerCase
from studio_platform.worker import MockBackend, WorkerRunner


class QuickChatIntegrationTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            Path(self.tmp.name),
            auth_mode="local-test",
            database_url=self.url,
            generation_enabled=True,
            execution_backend="mock",
        )
        self.app = create_app(self.settings, repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post(
            "/api/auth/login", json={"username": "superdan"}
        ).raise_for_status()

    def write(self, path, body, key, *, method="POST", headers=None):
        r = self.client.request(
            method, path, json=body, headers={"Idempotency-Key": key, **(headers or {})}
        )
        self.assertLess(r.status_code, 300, r.text)
        return r.json()

    def composer_fixture(self, suffix="reset"):
        session = self.write("/v1/quick-chat/sessions", {}, suffix + "-session")[
            "session"
        ]
        base = "/v1/quick-chat/sessions/" + session["id"]
        assets, bindings = [], []
        for index, slot in enumerate(("first_frame", "last_frame")):
            upload = self.client.post(
                base + "/assets",
                data={"client_asset_id": suffix + str(index)},
                files={"file": (slot + ".png", png(), "image/png")},
            )
            upload.raise_for_status()
            asset = upload.json()
            assets.append(asset)
            bindings.append(
                {
                    "binding_id": suffix + "-" + slot,
                    "version": 0,
                    "asset_id": asset["asset_id"],
                    "kind": "image",
                    "slot": slot,
                    "enabled": True,
                }
            )
        self.write(
            base + "/materials",
            {"expected_version": session["version"], "bindings": bindings},
            suffix + "-bind",
            method="PUT",
        )
        current = self.client.get(base).json()["session"]
        turn = self.write(
            base + "/turns",
            {
                "expected_version": current["version"],
                "text": "A detailed action scene.",
                "model_id": current["model_id"],
                "assistant_mode": "none",
                "create_card": True,
            },
            suffix + "-turn",
        )
        card = self.client.get(base + "/cards/" + turn["card_id"]).json()
        revision = self.client.get(
            base + "/revisions/" + card["current_revision_id"]
        ).json()
        path = base + "/revisions/" + revision["id"]
        preflight = self.write(
            path + "/preflights",
            {"capabilities_version": VERSION, "revision_hash": revision["input_hash"]},
            suffix + "-preflight",
        )
        self.assertEqual(preflight["status"], "ready", preflight)
        body = {
            "preflight_id": preflight["id"],
            "revision_hash": revision["input_hash"],
            "confirmed": True,
        }
        return base, revision, body, assets

    def test_generation_acceptance_resets_selection_for_browser_and_agent_without_touching_history(
        self,
    ):
        key = self.client.post(
            "/v1/api-keys",
            json={
                "name": "Reset test",
                "scopes": sorted(API_SCOPES),
                "all_projects": True,
                "project_ids": [],
                "expires_in_days": 1,
            },
        )
        key.raise_for_status()
        for index, headers in enumerate(
            (None, {"Authorization": "Bearer " + key.json()["api_key"]})
        ):
            with self.subTest(actor=index):
                base, revision, body, assets = self.composer_fixture(
                    "actor" + str(index)
                )
                original = copy.deepcopy(revision)
                before = self.client.get(base).json()["session"]
                self.assertTrue(before["input_refs"]["first_frame"])
                submitted = self.write(
                    base + "/revisions/" + revision["id"] + "/submissions",
                    body,
                    "submit" + str(index),
                    headers=headers,
                )
                receipt = submitted["composer_reset"]
                self.assertEqual(receipt["status"], "accepted")
                self.assertEqual(receipt["turn_id"], revision["turn_id"])
                self.assertEqual(len(receipt["cleared_binding_ids"]), 2)
                fresh = self.client.get(base).json()["session"]
                self.assertIsNone(fresh["input_refs"]["first_frame"])
                self.assertIsNone(fresh["input_refs"]["last_frame"])
                self.assertEqual(fresh["composer_reset"], receipt)
                self.assertEqual(fresh["next_settings"], before["next_settings"])
                self.assertEqual(fresh["model_id"], before["model_id"])
                self.assertEqual(
                    self.client.get(base + "/revisions/" + revision["id"]).json(),
                    original,
                )
                self.assertEqual(
                    len(self.client.get(base + "/materials").json()["bindings"]), 2
                )
                self.assertTrue(
                    all(
                        self.client.get("/v1/assets/" + a["asset_id"]).status_code
                        == 200
                        for a in assets
                    )
                )
                replay = self.write(
                    base + "/revisions/" + revision["id"] + "/submissions",
                    body,
                    "other" + str(index),
                    headers=headers,
                )
                self.assertEqual(replay["composer_reset"], receipt)
                self.assertEqual(
                    self.client.get(base).json()["session"]["version"], fresh["version"]
                )

    def test_definite_admission_rejection_keeps_composer(self):
        base, revision, body, _ = self.composer_fixture()
        before = self.client.get(base).json()["session"]
        with mock.patch.object(
            self.app.state.quick_chat.hooks,
            "enqueue",
            side_effect=Conflict("test_reject"),
        ):
            submitted = self.write(
                base + "/revisions/" + revision["id"] + "/submissions",
                body,
                "blocked-submit",
            )
        self.assertEqual(submitted["items"][0]["status"], "admission_blocked")
        self.assertNotIn("composer_reset", submitted)
        after = self.client.get(base).json()["session"]
        self.assertEqual(after["input_refs"], before["input_refs"])
        self.assertEqual(after["version"], before["version"])
        self.assertIsNone(after["composer_reset"])

    def test_unknown_enqueue_response_keeps_draft_until_original_receipt_reconciles(
        self,
    ):
        base, revision, body, _ = self.composer_fixture()
        service = self.app.state.quick_chat
        enqueue = service.hooks.enqueue

        def interrupted(principal, job, **spending):
            enqueue(principal, job, **spending)
            raise RuntimeError("lost queue reply")

        path = base + "/revisions/" + revision["id"] + "/submissions"
        with mock.patch.object(service.hooks, "enqueue", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    path, json=body, headers={"Idempotency-Key": "original-submit"}
                )
        self.assertTrue(
            self.client.get(base).json()["session"]["input_refs"]["first_frame"]
        )
        accepted = self.write(path, body, "original-submit")
        self.assertEqual(accepted["items"][0]["status"], "queued")
        self.assertEqual(accepted["composer_reset"]["status"], "accepted")
        self.assertIsNone(
            self.client.get(base).json()["session"]["input_refs"]["first_frame"]
        )
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs.c.id)).all()), 1)

    def test_acceptance_cas_preserves_newer_material_edit_and_replay_never_clears_it(
        self,
    ):
        base, revision, body, _ = self.composer_fixture()
        service = self.app.state.quick_chat
        enqueue = service.hooks.enqueue
        original = copy.deepcopy(revision)

        def edit_then_enqueue(principal, job, **spending):
            catalog = self.client.get(base + "/materials").json()
            current = self.client.get(base).json()["session"]
            bindings = [
                {
                    k: b[k]
                    for k in (
                        "binding_id",
                        "version",
                        "asset_id",
                        "kind",
                        "slot",
                        "enabled",
                    )
                }
                for b in catalog["bindings"]
            ]
            # A real ABA edit has the same value but a newer binding version.
            bindings[0]["enabled"] = False
            changed = self.write(
                base + "/materials",
                {"expected_version": current["version"], "bindings": bindings},
                "off",
                method="PUT",
            )
            bindings[0].update(
                enabled=True,
                version=next(
                    b["version"]
                    for b in changed["bindings"]
                    if b["binding_id"] == bindings[0]["binding_id"]
                ),
            )
            self.write(
                base + "/materials",
                {"expected_version": changed["session_version"], "bindings": bindings},
                "on",
                method="PUT",
            )
            return enqueue(principal, job, **spending)

        path = base + "/revisions/" + revision["id"] + "/submissions"
        with mock.patch.object(service.hooks, "enqueue", side_effect=edit_then_enqueue):
            accepted = self.write(path, body, "racing-submit")
        receipt = accepted["composer_reset"]
        self.assertEqual(len(receipt["cleared_binding_ids"]), 1)
        self.assertEqual(len(receipt["preserved_binding_ids"]), 1)
        self.assertEqual(
            self.client.get(base + "/revisions/" + revision["id"]).json(), original
        )
        selected = self.client.get(base).json()["session"]["input_refs"]
        self.assertEqual(
            sum(selected[s] is not None for s in ("first_frame", "last_frame")), 1
        )
        replay = self.write(path, body, "racing-submit")
        self.assertEqual(replay["composer_reset"], receipt)
        self.assertEqual(
            self.client.get(base).json()["session"]["input_refs"], selected
        )

    def legacy_composer_fixture(self, suffix="legacy"):
        base, revision, body, assets = self.composer_fixture(suffix)
        # Reproduce a persisted pre-release immutable revision. Its already
        # prepared preflight likewise has no authoring origin to infer later.
        with self.repo.transaction() as conn:
            row = (
                conn.execute(
                    select(quick_chat_objects).where(
                        quick_chat_objects.c.id == revision["id"]
                    )
                )
                .mappings()
                .one()
            )
            payload = {
                k: v for k, v in row["payload"].items() if k != "composer_snapshot"
            }
            conn.execute(
                update(quick_chat_objects)
                .where(quick_chat_objects.c.id == revision["id"])
                .values(payload=payload)
            )
        return (
            base,
            self.client.get(base + "/revisions/" + revision["id"]).json(),
            body,
            assets,
        )

    def material_payloads(self, base):
        allowed = {
            "binding_id",
            "version",
            "asset_id",
            "kind",
            "slot",
            "purpose",
            "enabled",
            "source_range",
            "include_audio",
            "time_seconds",
            "use_audio",
        }
        return [
            {k: v for k, v in b.items() if k in allowed}
            for b in self.client.get(base + "/materials").json()["bindings"]
        ]

    def add_unrelated_material(self, base, key="unrelated"):
        upload = self.client.post(
            base + "/assets",
            data={"client_asset_id": key},
            files={"file": (key + ".png", png(), "image/png")},
        )
        upload.raise_for_status()
        current = self.client.get(base).json()["session"]
        bindings = self.material_payloads(base)
        asset_id = upload.json()["asset_id"]
        bindings = [b for b in bindings if b["asset_id"] != asset_id]
        bindings.append(
            {
                "binding_id": key,
                "version": 0,
                "asset_id": asset_id,
                "kind": "image",
                "slot": "images",
                "purpose": "identity",
                "enabled": True,
            }
        )
        self.write(
            base + "/materials",
            {"expected_version": current["version"], "bindings": bindings},
            key + "-select",
            method="PUT",
        )
        return key

    def material_aba(self, base, binding_id, suffix="legacy-aba"):
        current = self.client.get(base).json()["session"]
        bindings = self.material_payloads(base)
        next(b for b in bindings if b["binding_id"] == binding_id)["enabled"] = False
        self.write(
            base + "/materials",
            {"expected_version": current["version"], "bindings": bindings},
            suffix + "-off",
            method="PUT",
        )
        current = self.client.get(base).json()["session"]
        bindings = self.material_payloads(base)
        next(b for b in bindings if b["binding_id"] == binding_id)["enabled"] = True
        self.write(
            base + "/materials",
            {"expected_version": current["version"], "bindings": bindings},
            suffix + "-on",
            method="PUT",
        )

    def fresh_legacy_preflight(self, base, revision, key="legacy-new-preflight"):
        checked = self.write(
            base + "/revisions/" + revision["id"] + "/preflights",
            {"capabilities_version": VERSION, "revision_hash": revision["input_hash"]},
            key,
        )
        self.assertEqual(checked["status"], "ready", checked)
        return checked, {
            "preflight_id": checked["id"],
            "revision_hash": revision["input_hash"],
            "confirmed": True,
        }

    def test_new_legacy_preflight_freezes_matching_origin_before_confirmation_for_agent(
        self,
    ):
        base, revision, _, _ = self.legacy_composer_fixture()
        unrelated = self.add_unrelated_material(base)
        checked, body = self.fresh_legacy_preflight(base, revision)
        self.assertEqual(
            {b["binding_id"] for b in checked["composer_snapshot"]["bindings"]},
            {"legacy-first_frame", "legacy-last_frame"},
        )
        self.material_aba(base, "legacy-first_frame")
        later = self.add_unrelated_material(base, "after-preflight")
        replay, _ = self.fresh_legacy_preflight(base, revision)
        self.assertEqual(replay["composer_snapshot"], checked["composer_snapshot"])
        key = self.client.post(
            "/v1/api-keys",
            json={
                "name": "Legacy reset test",
                "scopes": sorted(API_SCOPES),
                "all_projects": True,
                "project_ids": [],
                "expires_in_days": 1,
            },
        )
        key.raise_for_status()
        result = self.write(
            base + "/revisions/" + revision["id"] + "/submissions",
            body,
            "legacy-agent-submit",
            headers={"Authorization": "Bearer " + key.json()["api_key"]},
        )
        self.assertEqual(result["composer_snapshot"], checked["composer_snapshot"])
        self.assertEqual(
            result["composer_reset"]["cleared_binding_ids"], ["legacy-last_frame"]
        )
        self.assertEqual(
            set(result["composer_reset"]["preserved_binding_ids"]),
            {"legacy-first_frame", unrelated, later},
        )
        self.assertEqual(
            self.client.get(base + "/revisions/" + revision["id"]).json(), revision
        )
        after = self.client.get(base).json()["session"]
        again = self.write(
            base + "/revisions/" + revision["id"] + "/submissions",
            body,
            "legacy-agent-submit",
        )
        self.assertEqual(again["composer_reset"], result["composer_reset"])
        self.assertEqual(
            self.client.get(base).json()["session"]["version"], after["version"]
        )

    def test_explicit_legacy_card_control_edit_creates_origin_without_mutating_original(
        self,
    ):
        base, original, _, _ = self.legacy_composer_fixture()
        unrelated = self.add_unrelated_material(base)
        card = self.client.get(base + "/cards/" + original["card_id"]).json()
        body = {
            k: original[k]
            for k in ("prompt", "recipe_id", "controls", "inputs", "copies")
        }
        body.update(
            controls={**body["controls"], "seed": "123"},
            expected_card_version=card["version"],
            source_revision_id=original["id"],
        )
        edited = self.write(
            base + "/cards/" + card["id"] + "/revisions", body, "legacy-edit"
        )["revision"]
        self.assertEqual(edited["version"], original["version"] + 1)
        self.assertEqual(
            {b["binding_id"] for b in edited["composer_snapshot"]["bindings"]},
            {"legacy-first_frame", "legacy-last_frame"},
        )
        self.assertEqual(
            self.client.get(base + "/revisions/" + original["id"]).json(), original
        )
        self.material_aba(base, "legacy-first_frame")
        _, confirmation = self.fresh_legacy_preflight(base, edited)
        submitted = self.write(
            base + "/revisions/" + edited["id"] + "/submissions",
            confirmation,
            "legacy-edited-submit",
        )
        self.assertEqual(
            submitted["composer_reset"]["cleared_binding_ids"], ["legacy-last_frame"]
        )
        self.assertEqual(
            set(submitted["composer_reset"]["preserved_binding_ids"]),
            {"legacy-first_frame", unrelated},
        )
        self.assertEqual(
            self.client.get(base + "/revisions/" + original["id"]).json(), original
        )
        self.assertEqual(
            self.client.get(base + "/revisions/" + edited["id"]).json(), edited
        )

    def test_legacy_new_preflight_rejection_and_unknown_reply_preserve_original_origin(
        self,
    ):
        base, revision, _, _ = self.legacy_composer_fixture()
        checked, body = self.fresh_legacy_preflight(base, revision)
        before = self.client.get(base).json()["session"]
        service = self.app.state.quick_chat
        path = base + "/revisions/" + revision["id"] + "/submissions"
        with mock.patch.object(
            service.hooks, "enqueue", side_effect=Conflict("test_reject")
        ):
            rejected = self.write(path, body, "legacy-submit")
        self.assertNotIn("composer_reset", rejected)
        self.assertEqual(rejected["composer_snapshot"], checked["composer_snapshot"])
        self.assertEqual(
            self.client.get(base).json()["session"]["version"], before["version"]
        )
        # Explicit resume uses the same original item; response loss never
        # recaptures materials that have been changed since its first preflight.
        self.material_aba(base, "legacy-first_frame")
        item = rejected["items"][0]
        fresh = self.write(
            base + "/revisions/" + revision["id"] + "/preflights",
            {
                "capabilities_version": VERSION,
                "revision_hash": revision["input_hash"],
                "item_ids": [item["id"]],
            },
            "legacy-resume-preflight",
        )
        resume_body = {
            "item_ids": [item["id"]],
            "fresh_preflight_id": fresh["id"],
            "confirmed": True,
        }
        enqueue = service.hooks.enqueue

        def interrupted(principal, job, **spending):
            enqueue(principal, job, **spending)
            raise RuntimeError("lost legacy queue reply")

        with mock.patch.object(service.hooks, "enqueue", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    base + "/submissions/" + rejected["id"] + "/resume-admission",
                    json=resume_body,
                    headers={"Idempotency-Key": "legacy-resume"},
                )
        self.assertTrue(
            self.client.get(base).json()["session"]["input_refs"]["first_frame"]
        )
        accepted = self.write(
            base + "/submissions/" + rejected["id"] + "/resume-admission",
            resume_body,
            "legacy-resume",
        )
        self.assertEqual(
            accepted["composer_reset"]["cleared_binding_ids"], ["legacy-last_frame"]
        )
        self.assertEqual(
            accepted["composer_reset"]["preserved_binding_ids"], ["legacy-first_frame"]
        )
        self.assertEqual(accepted["composer_snapshot"], checked["composer_snapshot"])
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs.c.id)).all()), 1)

    def test_preexisting_originless_legacy_preflight_never_captures_later_selection(
        self,
    ):
        base, revision, original_body, _ = self.legacy_composer_fixture()
        self.add_unrelated_material(base)
        before = self.client.get(base).json()["session"]
        result = self.write(
            base + "/revisions/" + revision["id"] + "/submissions",
            original_body,
            "preexisting-legacy",
        )
        self.assertEqual(result["items"][0]["status"], "queued")
        self.assertNotIn("composer_snapshot", result)
        self.assertNotIn("composer_reset", result)
        self.assertEqual(
            self.client.get(base).json()["session"]["version"], before["version"]
        )
        self.assertEqual(
            self.client.get(base + "/revisions/" + revision["id"]).json(), revision
        )

    def test_legacy_association_requires_same_reference_role_clip_and_audio_flags(self):
        bindings = [
            {
                "binding_id": "same",
                "version": 17,
                "asset_id": "image",
                "kind": "image",
                "slot": "images",
                "purpose": "identity",
                "enabled": True,
            },
            {
                "binding_id": "wrong-role",
                "version": 18,
                "asset_id": "image",
                "kind": "image",
                "slot": "images",
                "purpose": "reference",
                "enabled": True,
            },
            {
                "binding_id": "wrong-video-audio",
                "version": 19,
                "asset_id": "video",
                "kind": "video",
                "slot": "videos",
                "purpose": "motion",
                "source_range": {"start": 0, "end": 3},
                "include_audio": False,
                "enabled": True,
            },
            {
                "binding_id": "wrong-clip",
                "version": 20,
                "asset_id": "audio",
                "kind": "audio",
                "slot": "audios",
                "purpose": "audio",
                "source_range": {"start": 1, "end": 4},
                "enabled": True,
            },
            {
                "binding_id": "wrong-guide-audio",
                "version": 21,
                "asset_id": "video",
                "kind": "video",
                "slot": "guides",
                "time_seconds": 2,
                "use_audio": True,
                "enabled": True,
            },
        ]
        wanted = {
            "images": [{"asset_id": "image", "purpose": "identity"}],
            "videos": [
                {
                    "asset_id": "video",
                    "purpose": "motion",
                    "source_range": {"start": 0, "end": 3},
                    "include_audio": True,
                }
            ],
            "audios": [{"asset_id": "audio", "source_range": {"start": 0, "end": 3}}],
            "guides": [{"media_id": "video", "time_seconds": 2, "use_audio": False}],
        }
        snapshot = self.app.state.quick_chat._composer_snapshot(
            bindings, inputs=wanted, recipe_id="h3-base-ref2va-v1"
        )
        self.assertEqual(snapshot["bindings"], [bindings[0]])
        # Omitted default purpose identifies the same input, but the captured
        # mutable binding version is retained.
        wanted["audios"][0]["source_range"] = {"start": 1, "end": 4}
        snapshot = self.app.state.quick_chat._composer_snapshot(
            bindings, inputs=wanted, recipe_id="h3-base-ref2va-v1"
        )
        self.assertEqual(snapshot["bindings"], [bindings[0], bindings[3]])

    def test_web_and_two_agent_keys_share_one_card_and_two_jobs(self):
        created = self.write(
            "/v1/quick-chat/sessions", {"title": "HTTP contract fixture"}, "new-session"
        )
        session = created["session"]
        base = "/v1/quick-chat/sessions/" + session["id"]
        upload = self.client.post(
            base + "/assets",
            data={"client_asset_id": "original-first-frame"},
            files={"file": ("reference.png", png(), "image/png")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        asset = upload.json()
        self.assertEqual(asset["status"], "ready")
        self.write(
            base + "/materials",
            {
                "expected_version": session["version"],
                "bindings": [
                    {
                        "binding_id": "first-image",
                        "version": 0,
                        "asset_id": asset["asset_id"],
                        "kind": "image",
                        "slot": "first_frame",
                        "enabled": True,
                    }
                ],
            },
            "bind-image",
            method="PUT",
        )
        with mock.patch(
            "studio_platform.quick_chat.secrets.randbits", return_value=23
        ) as random:
            saved = self.write(
                base + "/cards",
                {
                    "title": "Image motion",
                    "prompt": "A person slowly turns toward the camera.",
                    "recipe_id": "h3-base-fl2va-v1",
                    "controls": {
                        "duration": 5,
                        "resolution": "480P",
                        "seed": "18446744073709551615",
                    },
                    "inputs": {"first_frame": {"asset_id": asset["asset_id"]}},
                    "copies": 2,
                },
                "new-card",
            )
        random.assert_called_once_with(64)
        revision = saved["revision"]
        seeds = [x["seed"] for x in revision["items"]]
        self.assertEqual(seeds, ["18446744073709551615", "23"])
        path = base + "/revisions/" + revision["id"]
        preflight = self.write(
            path + "/preflights",
            {"capabilities_version": VERSION, "revision_hash": revision["input_hash"]},
            "preflight",
        )
        self.assertEqual(preflight["status"], "ready", preflight)
        body = {
            "revision_hash": revision["input_hash"],
            "preflight_id": preflight["id"],
            "confirmed": True,
        }
        submitted = self.write(path + "/submissions", body, "web-submit")
        ids = [x["job_id"] for x in submitted["items"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(
            all(x["status"] == "queued" for x in submitted["items"]), submitted
        )
        for index in range(2):
            key = self.client.post(
                "/v1/api-keys",
                json={
                    "name": "Agent test",
                    "scopes": sorted(API_SCOPES),
                    "all_projects": True,
                    "project_ids": [],
                    "expires_in_days": 1,
                },
            )
            key.raise_for_status()
            headers = {"Authorization": "Bearer " + key.json()["api_key"]}
            replay = self.write(
                path + "/submissions",
                body,
                "agent-submit-" + str(index),
                headers=headers,
            )
            self.assertEqual(replay["id"], submitted["id"])
            self.assertEqual([x["job_id"] for x in replay["items"]], ids)
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs.c.id)).all()), 2)
        self.client.post("/api/auth/logout").raise_for_status()
        self.client.post(
            "/api/auth/login", json={"username": "supervan"}
        ).raise_for_status()
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(
            self.client.get("/v1/quick-chat/sessions").json()["sessions"], []
        )

    def test_disabled_assistant_saves_real_turn_and_does_not_create_video_job(self):
        session = self.write("/v1/quick-chat/sessions", {}, "new-session")["session"]
        base = "/v1/quick-chat/sessions/" + session["id"]
        turn = self.write(
            base + "/turns",
            {
                "expected_version": session["version"],
                "text": "Help me discuss a camera movement.",
                "model_id": "gemini-3.8-flash",
            },
            "discuss",
        )
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["error_code"], "assistant_disabled")
        self.assertIsNone(turn["reply"])
        restored = self.client.get(base + "/turns/" + turn["id"]).json()
        self.assertEqual(restored["id"], turn["id"])
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "CPU media tools required"
    )
    def test_agent_turn_card_confirm_same_job_then_owner_downloads_mock_outputs(self):
        guide = self.client.get("/for-agents/guide.json").json()["quick_chat"]
        examples = guide["examples"]
        self.assertEqual(
            self.client.get(guide["schema_url"]).json()["agent_contract"], guide
        )
        key = self.client.post(
            "/v1/api-keys",
            json={
                "name": "Offline Agent",
                "scopes": [
                    "projects:create",
                    "projects:read",
                    "projects:write",
                    "assets:read",
                    "assets:write",
                    "jobs:read",
                    "jobs:write",
                ],
                "all_projects": True,
                "project_ids": [],
                "expires_in_days": 1,
            },
        )
        key.raise_for_status()
        auth = {"Authorization": "Bearer " + key.json()["api_key"]}
        self.client.post("/api/auth/logout").raise_for_status()
        session = self.write(
            examples["session"]["path"],
            examples["session"]["body"],
            "agent-session",
            headers=auth,
        )["session"]
        base = "/v1/quick-chat/sessions/" + session["id"]
        turn = self.write(
            base + "/turns",
            {
                **examples["turn_to_card"]["body"],
                "expected_version": session["version"],
                "assistant_mode": "none",
                "create_card": True,
                "model_id": session["model_id"],
                "text": "Synthetic CPU demo with audio.",
            },
            "agent-turn",
            headers=auth,
        )
        card = self.client.get(base + "/cards/" + turn["card_id"], headers=auth).json()
        revision = self.client.get(
            base + "/revisions/" + card["current_revision_id"], headers=auth
        ).json()
        path = base + "/revisions/" + revision["id"]
        preflight = self.write(
            path + "/preflights",
            {"capabilities_version": VERSION, "revision_hash": revision["input_hash"]},
            "agent-preflight",
            headers=auth,
        )
        self.assertEqual(preflight["status"], "ready", preflight)
        body = {
            "revision_hash": revision["input_hash"],
            "preflight_id": preflight["id"],
            "confirmed": False,
        }
        rejected = self.client.post(
            path + "/submissions",
            json=body,
            headers={**auth, "Idempotency-Key": "unconfirmed"},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])
        submitted = self.write(
            path + "/submissions",
            {**body, "confirmed": True},
            "confirmed",
            headers=auth,
        )
        ident = submitted["items"][0]["job_id"]
        self.assertIsNotNone(ident)
        backend = MockBackend(Path(self.tmp.name) / "mock-runtime", enabled=True)
        runner = WorkerRunner(
            self.repo,
            self.app.state.storage,
            Path(self.tmp.name) / "worker",
            backend=backend,
            retry_after_s=0,
        )
        for _ in range(5):
            runner.run_once("cpu-test-only", "mock")
            observed = self.client.get(
                base + "/submissions/" + submitted["id"], headers=auth
            ).json()
            if observed["items"][0]["status"] == "succeeded":
                break
        job = observed["items"][0]["job"]
        self.assertEqual(job["id"], ident)
        self.assertEqual(job["status"], "succeeded", observed)
        self.assertTrue(job["simulation"])
        self.assertTrue(
            {"video", "audio"}.issubset({a["kind"] for a in job["artifacts"]})
        )
        for artifact in job["artifacts"]:
            response = self.client.get(artifact["download_url"], headers=auth)
            self.assertEqual(
                response.status_code,
                200,
                response.text[:100] if response.status_code != 200 else "",
            )
            self.assertEqual(len(response.content), artifact["size_bytes"])
            self.assertEqual(
                hashlib.sha256(response.content).hexdigest(), artifact["sha256"]
            )
        replay = self.write(
            path + "/submissions",
            {**body, "confirmed": True},
            "confirmed",
            headers=auth,
        )
        self.assertEqual(replay["items"][0]["job_id"], ident)
        self.assertEqual(
            self.repo.get_job_for_owner("sixnine", "superdan", ident)["attempt_no"], 1
        )
        self.client.post(
            "/api/auth/login", json={"username": "supervan"}
        ).raise_for_status()
        self.assertEqual(
            self.client.get(base + "/submissions/" + submitted["id"]).status_code, 404
        )
        self.assertEqual(
            self.client.get(job["artifacts"][0]["download_url"]).status_code, 404
        )

    def test_concurrent_confirmations_across_actors_keep_one_submission_and_job_per_item(
        self,
    ):
        service = self.app.state.quick_chat
        browser = Principal("superdan", "browser")
        agent = Principal("superdan", "agent-two", True, (), tuple(API_SCOPES), True)
        session = service.create_session(browser, {}, "race-session")["session"]
        sid = session["id"]
        revision = service.save_card(
            browser,
            sid,
            {
                "recipe_id": "h3-base-fl2va-v1",
                "prompt": "Synthetic concurrent confirmation",
                "inputs": {},
                "copies": 2,
                "controls": {"duration": 5, "resolution": "480P"},
            },
            "race-card",
        )["revision"]
        preflight = service.preflight(
            browser,
            sid,
            revision["id"],
            {"capabilities_version": VERSION, "revision_hash": revision["input_hash"]},
            "race-preflight",
        )
        body = {
            "confirmed": True,
            "preflight_id": preflight["id"],
            "revision_hash": revision["input_hash"],
        }
        barrier = threading.Barrier(2)

        def confirm(principal, key):
            barrier.wait(10)
            return service.submit(principal, sid, revision["id"], body, key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(confirm, browser, "race-browser")
            b = pool.submit(confirm, agent, "race-agent")
            results = (a.result(15), b.result(15))
        self.assertEqual(results[0]["id"], results[1]["id"])
        observed = service.get_submission(agent, sid, results[0]["id"])
        ids = [i["job_id"] for i in observed["items"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertNotIn(None, ids)
        with self.repo.engine.connect() as conn:
            self.assertEqual(set(conn.execute(select(jobs.c.id)).scalars()), set(ids))


if __name__ == "__main__":
    unittest.main()
