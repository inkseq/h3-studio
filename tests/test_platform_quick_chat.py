"""Durable chat isolation/recovery tests: local DB, CPU PNGs, fake model only."""

import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.capabilities import VERSION
from studio_platform.quick_chat import (
    DEFAULT_SETTINGS,
    EMPTY_INPUTS,
    QuickChatError,
    objects,
    events,
)
from studio_platform.repository import (
    jobs,
    attempts,
    artifacts,
    NotFound,
    Conflict,
    Scope,
)
from studio_platform.storage import make_object_key
from studio_platform.settings import Settings


class FakeAssistant:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or {"reply": "讨论回复。", "card": None}
        self.error = error

    def complete(self, model, messages, context, settings):
        self.calls.append((model, messages, context, settings))
        context["record_manifest"](
            [
                {
                    "asset_id": "receipt-marker",
                    "sent": False,
                    "reason": "prepared_for_request",
                }
            ]
        )
        if self.error:
            raise self.error
        return copy.deepcopy(self.response)


class QuickChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            Path(self.tmp.name),
            auth_mode="local-test",
            generation_enabled=True,
            execution_backend="mock",
        )
        self.app = create_app(self.settings)
        self.service = self.app.state.quick_chat
        self.repo = self.app.state.repository
        self.principal = Principal("superdan", "browser")
        self.agent = Principal(
            "superdan",
            "agent-key",
            machine=True,
            all_projects=True,
            scopes=(
                "projects:create",
                "projects:read",
                "projects:write",
                "assets:read",
                "assets:write",
                "jobs:read",
                "jobs:write",
                "assistant:run",
            ),
        )
        self.session = self.service.create_session(
            self.principal, {}, "session-create"
        )["session"]
        self.sid = self.session["id"]

    def tearDown(self):
        self.repo.engine.dispose()
        self.tmp.cleanup()

    def card(self, copies=1, controls=None, key="card-create"):
        return self.service.save_card(
            self.principal,
            self.sid,
            {
                **DEFAULT_SETTINGS,
                "controls": controls or DEFAULT_SETTINGS["controls"],
                "copies": copies,
                "prompt": "A blue cup rotates on a wooden desk, soft window light.",
                "inputs": copy.deepcopy(EMPTY_INPUTS),
            },
            key,
        )

    def preflight(self, revision, *, key="preflight", **extra):
        return self.service.preflight(
            self.principal,
            self.sid,
            revision["id"],
            {
                "capabilities_version": VERSION,
                "revision_hash": revision["input_hash"],
                **extra,
            },
            key,
        )

    def submit(self, revision, preflight=None, *, key="submit", principal=None):
        pf = preflight or self.preflight(revision)
        return self.service.submit(
            principal or self.principal,
            self.sid,
            revision["id"],
            {
                "preflight_id": pf["id"],
                "revision_hash": revision["input_hash"],
                "confirmed": True,
            },
            key,
        )

    def turn(self, text="只讨论", mode="assist", key="turn"):
        current = self.service.get_session(self.principal, self.sid)["session"]
        return self.service.create_turn(
            self.principal,
            self.sid,
            {
                "expected_version": current["version"],
                "text": text,
                "model_id": current["model_id"],
                "assistant_mode": mode,
            },
            key,
        )

    def test_session_replay_scope_and_conflict(self):
        same = self.service.create_session(self.agent, {}, "session-create")["session"]
        self.assertEqual(same["id"], self.sid)
        self.assertNotIn("project_id", same)
        with self.assertRaises(QuickChatError) as error:
            self.service.create_session(
                self.principal, {"title": "changed"}, "session-create"
            )
        self.assertEqual(error.exception.code, "idempotency_conflict")
        with self.assertRaises(NotFound):
            self.service.get_session(Principal("supervan", "browser"), self.sid)
        restricted = Principal(
            "superdan",
            "old-key",
            machine=True,
            scopes=("projects:read",),
            all_projects=True,
        )
        with self.assertRaises(QuickChatError):
            self.service.create_session(restricted, {}, "denied")

    def test_session_version_cas_and_replay_before_version(self):
        body = {"expected_version": 1, "title": "我的广告"}
        one = self.service.patch_session(self.principal, self.sid, body, "patch")
        two = self.service.patch_session(self.agent, self.sid, body, "patch")
        self.assertEqual(one, two)
        self.assertEqual(one["session"]["version"], 2)
        with self.assertRaises(QuickChatError) as error:
            self.service.patch_session(
                self.principal,
                self.sid,
                {"expected_version": 1, "title": "stale"},
                "stale",
            )
        self.assertEqual(error.exception.code, "version_conflict")

    def test_card_revision_is_immutable_and_fixed_seeds(self):
        one = self.card(
            copies=2, controls={"seed": "18446744073709551615", "duration": 5}
        )
        old = copy.deepcopy(one["revision"])
        two = self.service.save_card(
            self.principal,
            self.sid,
            {
                **DEFAULT_SETTINGS,
                "prompt": "A blue cup, close-up.",
                "inputs": EMPTY_INPUTS,
                "expected_card_version": 1,
                "source_revision_id": old["id"],
            },
            "revise",
            card_id=one["card"]["id"],
        )
        self.assertEqual(two["revision"]["version"], 2)
        self.assertEqual(
            self.service.get_revision(self.principal, self.sid, old["id"]), old
        )
        self.assertEqual(old["items"][0]["seed"], "18446744073709551615")
        self.assertNotEqual(old["items"][1]["seed"], old["items"][0]["seed"])
        self.assertTrue(0 <= int(old["items"][1]["seed"]) < 2**64)
        self.assertNotEqual(
            old["items"][0]["shot_id"], two["revision"]["items"][0]["shot_id"]
        )
        with self.assertRaises(QuickChatError):
            self.service.save_card(
                self.principal,
                self.sid,
                {
                    **DEFAULT_SETTINGS,
                    "prompt": "stale edit",
                    "inputs": EMPTY_INPUTS,
                    "expected_card_version": 1,
                },
                "stale-card",
                card_id=one["card"]["id"],
            )

    def test_upload_inventory_unbound_then_binding(self):
        source = io.BytesIO()
        Image.new("RGB", (512, 512), "navy").save(source, "PNG")
        source.seek(0)
        asset = self.service.upload(
            self.principal, self.sid, source, "reference.png", "stable-upload"
        )
        catalog = self.service.materials(self.principal, self.sid)
        binding = catalog["bindings"][0]
        self.assertEqual(binding["asset_id"], asset["asset_id"])
        self.assertFalse(binding["enabled"])
        self.assertEqual(binding["version"], 0)
        body_binding = {
            k: binding[k]
            for k in ("binding_id", "version", "asset_id", "kind", "slot", "enabled")
        }
        body_binding.update(slot="first_frame", enabled=True)
        result = self.service.put_materials(
            self.principal,
            self.sid,
            {"expected_version": 1, "bindings": [body_binding]},
            "bind",
        )
        self.assertEqual(result["bindings"][0]["version"], 1)
        self.assertEqual(
            self.service.get_session(self.principal, self.sid)["session"]["input_refs"][
                "first_frame"
            ],
            {"asset_id": asset["asset_id"]},
        )
        with self.assertRaises(NotFound):
            self.service.materials(Principal("supervan", "browser"), self.sid)

    def test_card_can_select_unbound_receipt_without_changing_next_turn(self):
        source = io.BytesIO()
        Image.new("RGB", (512, 512), "navy").save(source, "PNG")
        source.seek(0)
        asset = self.service.upload(
            self.principal, self.sid, source, "reference.png", "card-upload"
        )
        result = self.service.save_card(
            self.principal,
            self.sid,
            {
                **DEFAULT_SETTINGS,
                "prompt": "Animate this blue reference.",
                "inputs": {
                    **EMPTY_INPUTS,
                    "first_frame": {"asset_id": asset["asset_id"]},
                },
            },
            "independent-card",
        )
        self.assertEqual(
            result["revision"]["inputs"]["first_frame"]["asset_id"], asset["asset_id"]
        )
        self.assertIsNone(
            self.service.get_session(self.principal, self.sid)["session"]["input_refs"][
                "first_frame"
            ]
        )

    def test_recipe_effective_inputs_match_ui_and_frozen_assistant(self):
        bindings = []
        for index, slot in enumerate(("first_frame", "images")):
            source = io.BytesIO()
            Image.new("RGB", (512, 512), "navy").save(source, "PNG")
            source.seek(0)
            asset = self.service.upload(
                self.principal,
                self.sid,
                source,
                "reference.png",
                "participation-" + str(index),
            )
            bindings.append(
                {
                    "binding_id": "binding-" + str(index),
                    "version": 0,
                    "asset_id": asset["asset_id"],
                    "kind": "image",
                    "slot": slot,
                    "enabled": True,
                }
            )
        materials = self.service.put_materials(
            self.principal,
            self.sid,
            {"expected_version": 1, "bindings": bindings},
            "participation",
        )
        ordinary = next(b for b in materials["bindings"] if b["slot"] == "images")
        self.assertTrue(ordinary["enabled"])
        self.assertFalse(ordinary["effective_enabled"])
        self.assertEqual(ordinary["inactive_reason"], "mode_incompatible")
        fake = FakeAssistant()
        self.service.assistant, self.service.assistant_enabled = fake, True
        turn = self.turn(mode="discuss", key="fl-discuss")
        self.assertEqual(turn["input_refs"]["images"], [])
        self.assertTrue(turn["input_refs"]["first_frame"])
        self.assertEqual(
            [b["slot"] for b in fake.calls[0][2]["bindings"] if b["enabled"]],
            ["first_frame"],
        )
        self.assertEqual(
            turn["assistant_run"]["input_exclusions"][0]["reason"], "mode_incompatible"
        )
        current = self.service.get_session(self.principal, self.sid)["session"]
        self.service.patch_session(
            self.principal,
            self.sid,
            {
                "expected_version": current["version"],
                "next_settings": {**DEFAULT_SETTINGS, "recipe_id": "h3-base-ref2va-v1"},
            },
            "switch-ref",
        )
        refs = self.service.get_session(self.principal, self.sid)["session"][
            "input_refs"
        ]
        self.assertIsNone(refs["first_frame"])
        self.assertEqual(len(refs["images"]), 1)
        self.assertTrue(
            all(
                b["enabled"]
                for b in self.service.materials(self.principal, self.sid)["bindings"]
            )
        )

    def test_discussion_and_disabled_assistant_never_create_jobs(self):
        turn = self.turn()
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["error_code"], "assistant_disabled")
        second = self.turn(key="second")
        self.assertEqual(second["error_code"], "assistant_disabled")
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs)).all()), 0)

    def test_none_turn_creates_atomic_card_with_frozen_materials_and_settings(self):
        source = io.BytesIO()
        Image.new("RGB", (512, 512), "navy").save(source, "PNG")
        source.seek(0)
        asset = self.service.upload(
            self.principal, self.sid, source, "reference.png", "send-card-upload"
        )
        self.service.put_materials(
            self.principal,
            self.sid,
            {
                "expected_version": 1,
                "bindings": [
                    {
                        "binding_id": "send-card-reference",
                        "version": 0,
                        "asset_id": asset["asset_id"],
                        "kind": "image",
                        "slot": "first_frame",
                        "enabled": True,
                    }
                ],
            },
            "send-card-bind",
        )
        current = self.service.get_session(self.principal, self.sid)["session"]
        settings = {
            **DEFAULT_SETTINGS,
            "copies": 2,
            "controls": {"duration": 6, "resolution": "480P"},
        }
        self.service.patch_session(
            self.principal,
            self.sid,
            {"expected_version": current["version"], "next_settings": settings},
            "send-card-settings",
        )
        current = self.service.get_session(self.principal, self.sid)["session"]
        fake = FakeAssistant(error=AssertionError("no model call"))
        self.service.assistant, self.service.assistant_enabled = fake, True
        with mock.patch.object(
            self.service.hooks,
            "preflight",
            side_effect=AssertionError("no execution plan"),
        ):
            turn = self.service.create_turn(
                self.principal,
                self.sid,
                {
                    "expected_version": current["version"],
                    "text": "Animate this cup slowly.",
                    "model_id": current["model_id"],
                    "assistant_mode": "none",
                    "create_card": True,
                },
                "send-card",
            )
        card = self.service.get_card(self.principal, self.sid, turn["card_id"])
        revision = self.service.get_revision(
            self.principal, self.sid, card["current_revision_id"]
        )
        self.assertEqual(turn["status"], "recorded")
        self.assertIsNone(turn["reply"])
        self.assertEqual(revision["turn_id"], turn["id"])
        self.assertEqual(revision["prompt"], turn["text"])
        self.assertEqual(revision["inputs"], turn["input_refs"])
        self.assertEqual(
            {k: revision[k] for k in ("recipe_id", "controls", "copies")},
            turn["next_settings"],
        )
        self.assertEqual(len(revision["items"]), 2)
        self.assertEqual(fake.calls, [])
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs)).all()), 0)

    def test_none_turn_card_replay_across_actor_has_one_turn_one_card(self):
        body = {
            "expected_version": 1,
            "text": "A cup rotates.",
            "model_id": "gemini-3.8-flash",
            "assistant_mode": "none",
            "create_card": True,
        }
        one = self.service.create_turn(self.principal, self.sid, body, "send-card")
        two = self.service.create_turn(self.agent, self.sid, body, "send-card")
        self.assertEqual(one, two)
        with self.assertRaises(QuickChatError) as error:
            self.service.create_turn(self.principal, self.sid, body, "different-send")
        self.assertEqual(error.exception.code, "version_conflict")
        with self.repo.engine.connect() as conn:
            for kind in ("turn", "card", "revision"):
                self.assertEqual(
                    len(
                        conn.execute(
                            select(objects).where(objects.c.kind == kind)
                        ).all()
                    ),
                    1,
                )
            types = (
                conn.execute(select(events.c.type).order_by(events.c.seq))
                .scalars()
                .all()
            )
        self.assertEqual(types.count("turn.created"), 1)
        self.assertEqual(types.count("card.created"), 1)

    def test_none_turn_card_failure_rolls_back_turn_projection_and_operation(self):
        body = {
            "expected_version": 1,
            "text": "A cup rotates.",
            "model_id": "gemini-3.8-flash",
            "assistant_mode": "none",
            "create_card": True,
        }
        project = self.service._access(self.principal, self.sid)["payload"][
            "project_id"
        ]
        scope = Scope(self.settings.tenant_id, self.principal.owner, "__projects")
        before = self.repo.get_document(scope, "project", project)
        original = self.service._create_revision

        def fail_after_revision(*args, **kwargs):
            original(*args, **kwargs)
            raise QuickChatError("test_transaction_failure", "测试事务失败。")

        with mock.patch.object(
            self.service, "_create_revision", side_effect=fail_after_revision
        ):
            with self.assertRaises(QuickChatError):
                self.service.create_turn(self.principal, self.sid, body, "send-card")
        self.assertEqual(
            self.service.get_session(self.principal, self.sid)["session"]["version"], 1
        )
        self.assertEqual(self.repo.get_document(scope, "project", project), before)
        with self.repo.engine.connect() as conn:
            for kind in ("turn", "card", "revision"):
                self.assertEqual(
                    len(
                        conn.execute(
                            select(objects).where(objects.c.kind == kind)
                        ).all()
                    ),
                    0,
                )
            self.assertEqual(
                len(
                    conn.execute(
                        select(events).where(
                            events.c.type.in_(["turn.created", "card.created"])
                        )
                    ).all()
                ),
                0,
            )
        # The failed operation did not leave an idempotency receipt or revision
        # projection behind; retrying the same command succeeds once.
        turn = self.service.create_turn(self.principal, self.sid, body, "send-card")
        card = self.service.get_card(self.principal, self.sid, turn["card_id"])
        revision = self.service.get_revision(
            self.principal, self.sid, card["current_revision_id"]
        )
        document = self.repo.get_document(scope, "project", project)
        before_shots = len(
            [
                entity
                for entity in before["payload"]["entities"]
                if entity["type"] == "shot"
            ]
        )
        self.assertEqual(
            len(
                [
                    entity
                    for entity in document["payload"]["entities"]
                    if entity["type"] == "shot"
                ]
            ),
            before_shots + len(revision["items"]),
        )

    def test_none_turn_card_flag_does_not_change_other_modes_or_old_contract(self):
        fake = FakeAssistant(error=AssertionError("no model call"))
        self.service.assistant, self.service.assistant_enabled = fake, True
        base = {
            "expected_version": 1,
            "text": "A cup rotates.",
            "model_id": "gemini-3.8-flash",
            "create_card": True,
        }
        for mode in ("assist", "discuss"):
            with self.assertRaises(QuickChatError) as error:
                self.service.create_turn(
                    self.principal,
                    self.sid,
                    {**base, "assistant_mode": mode},
                    "invalid-" + mode,
                )
            self.assertEqual(error.exception.code, "invalid_card_creation")
        with self.assertRaises(QuickChatError):
            self.service.create_turn(
                self.principal,
                self.sid,
                {**base, "assistant_mode": "none", "create_card": "true"},
                "invalid-bool",
            )
        old_turn = self.turn(mode="none", key="old-contract")
        self.assertIsNone(old_turn["card_id"])
        self.assertEqual(old_turn["status"], "recorded")
        self.assertEqual(fake.calls, [])

    def test_none_turn_card_retains_owner_and_write_scope_checks(self):
        body = {
            "expected_version": 1,
            "text": "A cup rotates.",
            "model_id": "gemini-3.8-flash",
            "assistant_mode": "none",
            "create_card": True,
        }
        read_only = Principal(
            "superdan",
            "read-only",
            machine=True,
            all_projects=True,
            scopes=("projects:read",),
        )
        for principal in (Principal("supervan", "browser"), read_only):
            with self.assertRaises(NotFound):
                self.service.create_turn(principal, self.sid, body, "denied")
        # Direct draft creation needs no assistant:run or jobs:write grant.
        creator = Principal(
            "superdan",
            "draft-only",
            machine=True,
            all_projects=True,
            scopes=("projects:read", "projects:write"),
        )
        turn = self.service.create_turn(creator, self.sid, body, "draft-only")
        self.assertTrue(turn["card_id"])

    def test_discuss_suppresses_card_even_if_adapter_proposes(self):
        fake = FakeAssistant(
            {"reply": "可以这样做。", "card": {"prompt": "Should not create"}}
        )
        self.service.assistant, self.service.assistant_enabled = fake, True
        result = self.turn(mode="discuss")
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["card_id"])
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                len(
                    conn.execute(
                        select(objects).where(objects.c.kind == "revision")
                    ).all()
                ),
                0,
            )
            self.assertEqual(len(conn.execute(select(jobs)).all()), 0)

    def test_read_only_asset_agent_cannot_create_clip_derivation_for_assistant(self):
        fake = FakeAssistant()
        self.service.assistant, self.service.assistant_enabled = fake, True
        with self.repo.transaction() as conn:
            session = self.service._get(
                conn, self.principal, self.sid, self.sid, "session"
            )
            self.service._put(
                conn,
                session,
                {
                    **session["payload"],
                    "next_settings": {
                        **DEFAULT_SETTINGS,
                        "recipe_id": "h3-base-ref2va-v1",
                    },
                    "bindings": [
                        {
                            "binding_id": "video-fixture",
                            "version": 1,
                            "asset_id": "read-only-video",
                            "kind": "video",
                            "slot": "videos",
                            "enabled": True,
                            "source_range": {"start": 0, "end": 4},
                        }
                    ],
                },
            )
        read_only = Principal(
            "superdan",
            "read-only-agent",
            machine=True,
            all_projects=True,
            scopes=("projects:read", "projects:write", "assets:read", "assistant:run"),
        )
        with mock.patch.object(
            self.app.state.assets,
            "derive",
            side_effect=AssertionError("no unauthorized receipt write"),
        ):
            turn = self.service.create_turn(
                read_only,
                self.sid,
                {
                    "expected_version": 1,
                    "text": "讨论这段动作",
                    "model_id": "gemini-3.8-flash",
                },
                "readonly-clip",
            )
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["error_code"], "insufficient_scope")
        self.assertEqual(fake.calls, [])

    def test_assistant_inherits_real_card_and_changes_control(self):
        original = self.card(
            controls={"duration": 5, "resolution": "768P", "steps": 24}
        )["revision"]
        fake = FakeAssistant(
            {
                "reply": "镜头已改。",
                "card": {
                    "prompt": "A blue cup rotates on a wooden desk, soft window light, close-up.",
                    "controls": {"duration": 10},
                },
            }
        )
        self.service.assistant, self.service.assistant_enabled = fake, True
        turn = self.turn(text="只改为特写，并改成10秒")
        self.assertEqual(turn["status"], "completed")
        self.assertEqual(
            fake.calls[0][2]["related_card"]["input_hash"], original["input_hash"]
        )
        revision = self.service.get_revision(
            self.principal,
            self.sid,
            self.service.get_card(self.principal, self.sid, turn["card_id"])[
                "current_revision_id"
            ],
        )
        self.assertEqual(
            revision["controls"], {"duration": 10, "resolution": "768P", "steps": 24}
        )
        self.assertEqual(revision["source_revision_id"], original["id"])
        self.assertEqual(
            turn["assistant_run"]["context_revision_hash"], original["input_hash"]
        )

    def test_unknown_keeps_manifest_and_does_not_automatically_resend(self):
        from studio_platform.google_chat import ChatError

        fake = FakeAssistant(error=ChatError("upstream_timeout", "safe timeout"))
        self.service.assistant, self.service.assistant_enabled = fake, True
        result = self.turn()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(
            result["assistant_run"]["media_input_manifest"][0]["reason"],
            "prepared_for_request",
        )
        with self.assertRaises(QuickChatError) as error:
            self.turn(key="not-resend")
        self.assertEqual(error.exception.code, "assistant_run_active")
        self.assertEqual(len(fake.calls), 1)
        self.service.acknowledge_unknown(
            self.principal, self.sid, result["id"], {"acknowledged": True}, "ack"
        )
        self.service.assistant = FakeAssistant()
        self.assertEqual(self.turn(key="after-ack")["status"], "completed")

    def test_context_limit_is_explicit_without_upstream_call(self):
        fake = FakeAssistant()
        self.service.assistant, self.service.assistant_enabled = fake, True
        # A fixed clock checks stable seq ordering rather than timestamp luck.
        self.repo.clock = lambda: 1000.0
        for index in range(21):
            result = self.turn(key="round-" + str(index))
            if index < 20:
                self.assertEqual(result["status"], "completed")
            else:
                self.assertEqual(result["error_code"], "context_limit")
        self.assertEqual(len(fake.calls), 20)
        self.assertEqual(len(fake.calls[-1][1]), 39)

    def test_pending_recovery_releases_active_session(self):
        with mock.patch.object(self.service, "_run_assistant"):
            result = self.turn()
        self.repo.clock = lambda: 9999999999.0
        self.assertEqual(self.service.recover_assistant_runs(), 1)
        self.assertEqual(
            self.service.get_turn(self.principal, self.sid, result["id"])["status"],
            "failed",
        )
        self.assertEqual(
            self.turn(key="after-recovery")["error_code"], "assistant_disabled"
        )

    def test_submission_unique_across_actors_and_new_keys(self):
        revision = self.card(copies=3)["revision"]
        pf = self.preflight(revision)
        first = self.submit(revision, pf)
        second = self.submit(revision, pf, principal=self.agent, key="agent-other-key")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            [i["job_id"] for i in first["items"]],
            [i["job_id"] for i in second["items"]],
        )
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs)).all()), 3)

    def test_submit_requires_confirmation_and_current_preflight(self):
        revision = self.card()["revision"]
        pf = self.preflight(revision)
        with self.assertRaises(QuickChatError):
            self.service.submit(
                self.principal,
                self.sid,
                revision["id"],
                {
                    "preflight_id": pf["id"],
                    "revision_hash": revision["input_hash"],
                    "confirmed": False,
                },
                "unconfirmed",
            )
        self.repo.clock = lambda: pf["expires_at"] + 1
        self.assertTrue(
            self.service.get_preflight(self.principal, self.sid, pf["id"])["stale"]
        )
        with self.assertRaises(QuickChatError) as error:
            self.submit(revision, pf)
        self.assertEqual(error.exception.code, "preflight_stale")

    def test_preflight_known_video_length_error_has_safe_persistent_detail(self):
        revision = self.card(copies=2)["revision"]
        with mock.patch.object(
            self.service.hooks,
            "preflight",
            side_effect=ValueError(
                "Reference video exceeds the output length; increase duration or explicitly trim the reference"
            ),
        ):
            preflight = self.preflight(revision)
        self.assertEqual(preflight["status"], "blocked")
        self.assertFalse(preflight["estimate_available"])
        self.assertTrue(all(item["plan"] is None for item in preflight["items"]))
        for item in preflight["items"]:
            self.assertEqual(item["error_code"], "reference_video_exceeds_output")
            self.assertEqual(
                item["error_message"],
                "参考视频比输出时长长，请增加生成时长或选取更短片段。",
            )
        self.assertEqual(
            self.service.get_preflight(self.principal, self.sid, preflight["id"]),
            preflight,
        )

    def test_preflight_unknown_error_keeps_private_detail_out_of_public_record(self):
        revision = self.card()["revision"]
        marker = "private-input-and-provider-diagnostic-not-for-public-output"
        with mock.patch.object(
            self.service.hooks, "preflight", side_effect=ValueError(marker)
        ):
            preflight = self.preflight(revision)
        self.assertFalse(preflight["estimate_available"])
        self.assertEqual(preflight["items"][0]["error_code"], "preflight_rejected")
        self.assertNotIn(marker, json.dumps(preflight))
        persisted = self.service.get_preflight(
            self.principal, self.sid, preflight["id"]
        )
        self.assertNotIn(marker, json.dumps(persisted))

    def test_preflight_normal_plan_keeps_whole_batch_estimate(self):
        revision = self.card(copies=2)["revision"]
        preflight = self.preflight(revision)
        self.assertTrue(preflight["estimate_available"])
        self.assertEqual(preflight["status"], "ready")
        expected = sum(
            item["plan"]["estimate"]["cost_microusd"] for item in preflight["items"]
        )
        self.assertEqual(preflight["estimate"]["cost_microusd"], expected)
        self.assertEqual(preflight["estimate"]["kind"], "budget_reservation")
        self.assertFalse(preflight["estimate"]["final_bill"])

    def test_admission_lost_response_reconciles_same_business_job(self):
        revision = self.card()["revision"]
        pf = self.preflight(revision)
        original = self.service.hooks.create_planned

        def lose(principal, plan_id, identity):
            original(principal, plan_id, identity)
            raise RuntimeError("process interrupted after job creation")

        with mock.patch.object(self.service.hooks, "create_planned", side_effect=lose):
            with self.assertRaises(RuntimeError):
                self.submit(revision, pf)
        result = self.submit(revision, pf)
        self.assertEqual(result["items"][0]["status"], "queued")
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs)).all()), 1)

    def test_single_item_cancel_during_unlinked_creation_does_not_enqueue(self):
        revision = self.card(copies=2)["revision"]
        pf = self.preflight(revision)
        entered, release = threading.Event(), threading.Event()
        original = self.service.hooks.create_planned

        def paused(principal, plan_id, identity):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test release missing")
            return original(principal, plan_id, identity)

        with mock.patch.object(
            self.service.hooks, "create_planned", side_effect=paused
        ):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.submit, revision, pf)
                self.assertTrue(entered.wait(5))
                with self.repo.engine.connect() as conn:
                    submission = dict(
                        conn.execute(
                            select(objects).where(objects.c.kind == "submission")
                        )
                        .mappings()
                        .one()
                    )
                self.service.cancel(
                    self.principal,
                    self.sid,
                    submission["id"],
                    {"item_ids": [revision["items"][0]["id"]]},
                    "cancel-one",
                )
                release.set()
                result = future.result(5)
        self.assertEqual(
            [i["status"] for i in result["items"]], ["cancelled", "queued"]
        )
        self.assertFalse(result["cancel_requested"])

    def test_single_item_cancel_during_enqueue_is_not_overwritten(self):
        revision = self.card(copies=2)["revision"]
        pf = self.preflight(revision)
        entered, release = threading.Event(), threading.Event()
        original = self.service.hooks.enqueue

        def paused(principal, job, **spending):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test release missing")
            return original(principal, job, **spending)

        with mock.patch.object(self.service.hooks, "enqueue", side_effect=paused):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.submit, revision, pf)
                self.assertTrue(entered.wait(5))
                with self.repo.engine.connect() as conn:
                    submission = dict(
                        conn.execute(
                            select(objects).where(objects.c.kind == "submission")
                        )
                        .mappings()
                        .one()
                    )
                self.service.cancel(
                    self.principal,
                    self.sid,
                    submission["id"],
                    {"item_ids": [revision["items"][0]["id"]]},
                    "cancel-enqueue",
                )
                release.set()
                result = future.result(5)
        self.assertEqual(
            [i["status"] for i in result["items"]], ["cancelled", "queued"]
        )

    def test_concurrent_submit_different_actor_and_key_creates_one_batch(self):
        revision = self.card(copies=2)["revision"]
        pf = self.preflight(revision)
        barrier = threading.Barrier(2)

        def submit(actor, key):
            barrier.wait(5)
            return self.submit(revision, pf, principal=actor, key=key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(submit, self.principal, "browser-commit")
            b = pool.submit(submit, self.agent, "agent-commit")
            results = (a.result(5), b.result(5))
        self.assertEqual(results[0]["id"], results[1]["id"])
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                len(
                    conn.execute(
                        select(objects).where(objects.c.kind == "submission")
                    ).all()
                ),
                1,
            )
            self.assertEqual(len(conn.execute(select(jobs)).all()), 2)

    def test_partial_admission_explicit_resume_preserves_other_job(self):
        revision = self.card(copies=2)["revision"]
        original = self.service.hooks.enqueue
        call_count = [0]

        def fail_one(principal, job, **spending):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Conflict("test_admission_blocked")
            return original(principal, job, **spending)

        pf = self.preflight(revision)
        with mock.patch.object(self.service.hooks, "enqueue", side_effect=fail_one):
            result = self.submit(revision, pf)
        self.assertEqual(
            [i["status"] for i in result["items"]], ["admission_blocked", "queued"]
        )
        # Replaying confirmation is receipt recovery, not a fresh admission command.
        replay = self.submit(revision, pf)
        self.assertEqual(replay["items"][0]["status"], "admission_blocked")
        self.assertTrue(replay["items"][0]["resume_admission_available"])
        target = result["items"][0]
        fresh = self.preflight(revision, key="resume-pf", item_ids=[target["id"]])
        body = {
            "item_ids": [target["id"]],
            "fresh_preflight_id": fresh["id"],
            "confirmed": True,
        }
        resumed = self.service.resume_admission(
            self.principal, self.sid, result["id"], body, "resume"
        )
        again = self.service.resume_admission(
            self.agent, self.sid, result["id"], body, "resume"
        )
        self.assertEqual([i["status"] for i in resumed["items"]], ["queued", "queued"])
        self.assertEqual(resumed["items"][0]["job_id"], target["job_id"])
        self.assertEqual(resumed["items"][1]["job_id"], result["items"][1]["job_id"])
        self.assertEqual(again["id"], resumed["id"])
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs)).all()), 2)

    def test_admission_refresh_preserves_only_finite_diagnostic_and_original_job(self):
        revision = self.card()["revision"]
        initial = self.preflight(revision)
        with mock.patch.object(
            self.service.hooks,
            "enqueue",
            side_effect=Conflict("test_admission_blocked"),
        ):
            submission = self.submit(revision, initial)
        target = submission["items"][0]
        before = self.repo.get_job_for_owner("sixnine", "superdan", target["job_id"])
        for index, (error, expected) in enumerate(
            (
                (
                    Conflict("execution_policy_changed_or_unavailable"),
                    "execution_policy_changed_or_unavailable",
                ),
                (
                    QuickChatError("shot_version_conflict", "PRIVATE TEXT"),
                    "shot_version_conflict",
                ),
                (ValueError("PRIVATE PATH OR PROMPT"), "admission_refresh_blocked"),
            )
        ):
            fresh = self.preflight(
                revision, key="refresh-pf-" + str(index), item_ids=[target["id"]]
            )
            with mock.patch.object(
                self.service.hooks, "refresh_planned", side_effect=error
            ):
                result = self.service.resume_admission(
                    self.principal,
                    self.sid,
                    submission["id"],
                    {
                        "item_ids": [target["id"]],
                        "fresh_preflight_id": fresh["id"],
                        "confirmed": True,
                    },
                    "refresh-" + str(index),
                )
            item = result["items"][0]
            self.assertEqual(item["error_code"], expected)
            self.assertEqual(item["job_id"], target["job_id"])
            self.assertEqual(
                item["current_execution_id"], target["current_execution_id"]
            )
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertEqual(
                self.repo.get_job_for_owner("sixnine", "superdan", target["job_id"]),
                before,
            )

    def test_retry_is_single_item_unique_across_actor_and_seed_fixed(self):
        revision = self.card(copies=2)["revision"]
        original = self.submit(revision)
        target = original["items"][0]
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == target["job_id"])
                .values(status="failed", error_code="test_failure")
            )
        pf = self.preflight(
            revision,
            key="retry-pf",
            item_ids=[target["id"]],
            retry_of_execution_id=target["current_execution_id"],
        )
        body = {
            "retry_of_execution_id": target["current_execution_id"],
            "fresh_preflight_id": pf["id"],
            "confirmed": True,
        }
        one = self.service.retry(
            self.principal,
            self.sid,
            original["id"],
            target["id"],
            body,
            "retry-browser",
        )
        two = self.service.retry(
            self.agent, self.sid, original["id"], target["id"], body, "retry-agent"
        )
        self.assertEqual(
            one["items"][0]["current_execution_id"],
            two["items"][0]["current_execution_id"],
        )
        self.assertNotEqual(
            target["current_execution_id"], two["items"][0]["current_execution_id"]
        )
        self.assertEqual(target["seed"], two["items"][0]["seed"])
        self.assertEqual(original["items"][1]["job_id"], two["items"][1]["job_id"])
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(jobs)).all()), 3)

    def test_unknown_or_missing_attempt_stop_proof_cannot_retry(self):
        revision = self.card()["revision"]
        result = self.submit(revision)
        item = result["items"][0]
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == item["job_id"])
                .values(status="failed", attempt_no=1)
            )
        refreshed = self.service.get_submission(self.principal, self.sid, result["id"])
        self.assertFalse(refreshed["items"][0]["retryable"])
        with self.assertRaises(QuickChatError) as error:
            self.preflight(
                revision,
                key="unsafe-pf",
                item_ids=[item["id"]],
                retry_of_execution_id=item["current_execution_id"],
            )
        self.assertEqual(error.exception.code, "upstream_stop_unconfirmed")

    def test_restore_hold_blocks_missing_job_id(self):
        revision = self.card()["revision"]
        with mock.patch.object(self.service, "_admit"):
            submission = self.submit(revision)
        item = submission["items"][0]
        with self.repo.transaction() as conn:
            row = self.service._get(
                conn,
                self.principal,
                item["current_execution_id"],
                self.sid,
                "execution",
            )
            self.service._put(conn, row, {**row["payload"], "status": "recovery_hold"})
        state = self.service.get_submission(self.principal, self.sid, submission["id"])
        self.assertFalse(state["items"][0]["resume_admission_available"])
        with self.assertRaises(QuickChatError):
            self.preflight(revision, key="restore-pf", item_ids=[item["id"]])

    def test_orphan_current_attempt_needs_matching_stopped_history_before_retry(self):
        revision = self.card()["revision"]
        submitted = self.submit(revision)
        item = submitted["items"][0]
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == item["job_id"])
                .values(
                    status="failed",
                    attempt_no=0,
                    current_attempt_id="missing-current-attempt",
                )
            )

        def refused(key):
            current = self.service.get_submission(
                self.principal, self.sid, submitted["id"]
            )
            self.assertFalse(current["items"][0]["retryable"])
            with self.assertRaises(QuickChatError) as error:
                self.preflight(
                    revision,
                    key=key,
                    item_ids=[item["id"]],
                    retry_of_execution_id=item["current_execution_id"],
                )
            self.assertEqual(error.exception.code, "upstream_stop_unconfirmed")

        refused("orphan-empty-history")
        with self.repo.transaction() as conn:
            conn.execute(
                insert(attempts).values(
                    id="stopped-first-attempt",
                    job_id=item["job_id"],
                    number=1,
                    status="failed",
                    fence=1,
                    worker_id="synthetic-worker",
                    created_at=1,
                    updated_at=1,
                    upstream_stopped=1,
                )
            )
            conn.execute(
                update(jobs).where(jobs.c.id == item["job_id"]).values(attempt_no=1)
            )
        refused("orphan-other-stopped-history")
        with self.repo.transaction() as conn:
            conn.execute(
                insert(attempts).values(
                    id="stopped-second-attempt",
                    job_id=item["job_id"],
                    number=2,
                    status="failed",
                    fence=2,
                    worker_id="synthetic-worker",
                    created_at=2,
                    updated_at=2,
                    upstream_stopped=1,
                )
            )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == item["job_id"])
                .values(attempt_no=2, current_attempt_id="stopped-first-attempt")
            )
        refused("current-number-mismatch")
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == item["job_id"])
                .values(current_attempt_id="stopped-second-attempt")
            )
        current = self.service.get_submission(self.principal, self.sid, submitted["id"])
        self.assertTrue(current["items"][0]["retryable"])
        fresh = self.preflight(
            revision,
            key="matching-stopped-current",
            item_ids=[item["id"]],
            retry_of_execution_id=item["current_execution_id"],
        )
        self.assertEqual(fresh["status"], "ready")

    def test_timeline_cursor_stable_scoped_and_runtime_does_not_append(self):
        for index in range(5):
            self.turn(mode="none", key="record-" + str(index))
        page = self.service.timeline(self.principal, self.sid, limit=2)
        self.assertEqual([e["seq"] for e in page["events"]], [4, 5])
        old = self.service.timeline(
            self.principal, self.sid, limit=2, cursor=page["next_cursor"]
        )
        self.assertEqual([e["seq"] for e in old["events"]], [2, 3])
        self.turn(mode="none", key="later")
        new = self.service.timeline(
            self.principal, self.sid, cursor=page["after_cursor"], direction="newer"
        )
        self.assertEqual([e["seq"] for e in new["events"]], [6])
        other = self.service.create_session(self.principal, {}, "second-session")[
            "session"
        ]
        with self.assertRaises(QuickChatError):
            self.service.timeline(
                self.principal, other["id"], cursor=page["next_cursor"]
            )
        revision = self.card()["revision"]
        sub = self.submit(revision)
        latest = self.service.get_session(self.principal, self.sid)["session"][
            "latest_seq"
        ]
        self.service.get_submission(self.principal, self.sid, sub["id"])
        self.assertEqual(
            self.service.get_session(self.principal, self.sid)["session"]["latest_seq"],
            latest,
        )

    def test_http_inventory_and_managed_projection_protection(self):
        with TestClient(self.app) as client:
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            source = io.BytesIO()
            Image.new("RGB", (512, 512), "navy").save(source, "PNG")
            response = client.post(
                "/v1/quick-chat/sessions/" + self.sid + "/assets",
                files={"file": ("ref.png", source.getvalue(), "image/png")},
                data={"client_asset_id": "upload-http"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            inventory = client.get(
                "/v1/quick-chat/sessions/" + self.sid + "/assets",
                params={"client_asset_id": "upload-http"},
            )
            self.assertEqual(inventory.status_code, 200)
            self.assertEqual(len(inventory.json()["assets"]), 1)
            hidden_id = self.service._access(self.principal, self.sid)["payload"][
                "project_id"
            ]
            self.assertEqual(client.get("/v1/projects").json()["projects"], [])
            old = client.get("/v1/projects/" + hidden_id).json()
            denied = client.put(
                "/v1/projects/" + hidden_id,
                json={"project": old["project"], "expected_version": old["version"]},
            )
            self.assertIn(denied.status_code, (409, 422))

    def jpeg_artifact(self, image_format="JPEG"):
        """Validated fixture only; never a real model output or paid generation."""
        revision = self.card()["revision"]
        sub = self.submit(revision)
        job_id = sub["items"][0]["job_id"]
        image = io.BytesIO()
        Image.new("RGB", (512, 512), "gold").save(image, image_format)
        raw = image.getvalue()
        ident = uuid.uuid4().hex
        mime, extension = (
            ("image/jpeg", ".jpg")
            if image_format == "JPEG"
            else ("image/webp", ".webp")
        )
        key = make_object_key("superdan", ident, "output" + extension)
        info = self.app.state.storage.put(
            key, io.BytesIO(raw), content_type=mime, max_bytes=len(raw)
        )
        attempt_id = str(uuid.uuid4())
        artifact_id = str(uuid.uuid4())
        with self.repo.transaction() as conn:
            job = dict(
                conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
            )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="succeeded",
                    attempt_no=1,
                    execution_plan={**job["execution_plan"], "backend": "comfy-worker"},
                )
            )
            conn.execute(
                insert(attempts).values(
                    id=attempt_id,
                    job_id=job_id,
                    number=1,
                    status="succeeded",
                    fence=1,
                    worker_id="fixture-worker",
                    created_at=self.repo.clock(),
                    updated_at=self.repo.clock(),
                    upstream_stopped=1,
                )
            )
            conn.execute(
                insert(artifacts).values(
                    id=artifact_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    metadata={
                        "kind": "image",
                        "validated": True,
                        "object_key": key,
                        "size_bytes": info.size_bytes,
                        "sha256": info.sha256,
                        "content_type": mime,
                    },
                    created_at=self.repo.clock(),
                )
            )
        return artifact_id, key

    def test_result_import_jpeg_mime_replay_preserves_original_and_composer(self):
        artifact_id, key = self.jpeg_artifact()
        body = {"source_artifact_id": artifact_id, "purpose": "reference"}
        imported = self.service.result_import(self.principal, self.sid, body, "import")
        self.assertEqual(imported["status"], "ready")
        with mock.patch.object(
            self.app.state.assets,
            "upload",
            side_effect=AssertionError("no replay upload"),
        ):
            replay = self.service.result_import(self.agent, self.sid, body, "import")
        self.assertEqual(imported["id"], replay["id"])
        self.assertEqual(
            self.app.state.assets.get("superdan", imported["asset_id"])["mime"],
            "image/jpeg",
        )
        self.assertGreater(self.app.state.storage.stat(key).size_bytes, 0)
        self.assertEqual(
            self.service.get_session(self.principal, self.sid)["session"]["input_refs"],
            EMPTY_INPUTS,
        )
        with self.assertRaises(NotFound):
            self.service.result_import(
                Principal("supervan", "browser"), self.sid, body, "import-other"
            )

    def test_result_import_lost_upload_response_reconciles_existing_receipt(self):
        artifact_id, _ = self.jpeg_artifact()
        original = self.app.state.assets.upload

        def lose(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("lost local upload response")

        with mock.patch.object(self.app.state.assets, "upload", side_effect=lose):
            first = self.service.result_import(
                self.principal,
                self.sid,
                {"source_artifact_id": artifact_id},
                "import-lost",
            )
        self.assertEqual(first["status"], "unknown")
        with mock.patch.object(
            self.app.state.assets,
            "upload",
            side_effect=AssertionError("same client receipt must be reused"),
        ):
            final = self.service.result_import(
                self.principal,
                self.sid,
                {"source_artifact_id": artifact_id},
                "import-lost",
            )
        self.assertEqual(final["id"], first["id"])
        self.assertEqual(final["status"], "ready")

    def test_result_import_webp_uses_verified_mime(self):
        artifact_id, _ = self.jpeg_artifact("WEBP")
        imported = self.service.result_import(
            self.principal, self.sid, {"source_artifact_id": artifact_id}, "import-webp"
        )
        self.assertEqual(imported["status"], "ready")
        asset = self.app.state.assets.get("superdan", imported["asset_id"])
        self.assertEqual(asset["mime"], "image/webp")
        self.assertTrue(asset["file_name"].endswith(".webp"))


if __name__ == "__main__":
    unittest.main()
