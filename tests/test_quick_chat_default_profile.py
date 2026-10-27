"""New API cards pin the configured default; old receipts remain immutable."""

import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_quality_authoring as fixture
from studio_platform.api import create_app
from studio_platform.auth import API_SCOPES
from studio_platform.capabilities import VERSION
from studio_platform.creator_controls import TAOMATE, default_chat_profile
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.quality_tiers import QualitySelectionError
from studio_platform.runtime_catalog import (
    engine_manifest,
    get_profile,
    profile_output_delivery,
)


class QuickChatDefaultProfileTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.QualityAuthoringTests(methodName="runTest")
        with patch.object(fixture, "PROFILE", TAOMATE):
            self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.f.machine = replace(
            self.f.machine, all_projects=True, scopes=tuple(API_SCOPES)
        )
        tenant = self.f.settings.tenant_id
        for owner in (None, "superdan", "supervan"):
            name = f"test-owner:{tenant}:{owner}" if owner else f"test-tenant:{tenant}"
            self.f.repo.configure_budget(
                name, tenant_id=tenant, owner_id=owner, limit_microusd=10_000_000
            )
        worker = WorkerControl(self.f.repo)
        worker.register(
            WorkerSpec(
                "synthetic-slot",
                "synthetic-pool",
                "test-only",
                "test-instance",
                ("test-gpu",),
                (fixture.RECIPE,),
                get_profile(TAOMATE)["model_id"],
                "synthetic-config",
                "wangp-worker",
                engine_manifest_digest=engine_manifest(TAOMATE, "fl").digest,
                output_delivery=profile_output_delivery(TAOMATE),
            )
        )
        worker.mark_ready("synthetic-slot", upstream_idle_confirmed=True)
        self.app = create_app(self.f.settings, repository=self.f.repo)
        self.service = self.app.state.quick_chat

    def body(self):
        return {
            "recipe_id": fixture.RECIPE,
            "prompt": "Offline default-profile regression.",
            "controls": {
                "steps": 50,
                "duration": 5,
                "resolution": "480P",
                "seed": "42",
            },
            "inputs": {},
            "copies": 1,
        }

    def test_omitted_profile_preflights_v7_five_steps_for_all_creator_clients(self):
        for actor in (self.f.admin, self.f.user, self.f.machine):
            with self.subTest(actor=actor.actor_id):
                session = self.service.create_session(
                    actor, {}, actor.actor_id.replace(":", "-")
                )["session"]
                body = self.body()
                before = copy.deepcopy(body)
                saved = self.service.save_card(actor, session["id"], body, "card")
                revision = saved["revision"]
                self.assertEqual(revision["deployment_profile_id"], TAOMATE)
                self.assertNotIn("steps", revision["controls"])
                result = self.service.preflight(
                    actor,
                    session["id"],
                    revision["id"],
                    {
                        "capabilities_version": VERSION,
                        "revision_hash": revision["input_hash"],
                    },
                    "plan",
                )
                self.assertEqual(result["status"], "ready", result)
                effective = result["items"][0]["plan"]["effective_request"]
                self.assertEqual(effective["steps"], 5)
                self.assertEqual(effective["seed"], "42")
                self.assertEqual(body, before)
                with patch(
                    "studio_platform.quick_chat.default_chat_profile",
                    side_effect=AssertionError("No replay resolution"),
                ):
                    replay = self.service.save_card(actor, session["id"], body, "card")
                self.assertEqual(replay, saved)

    def test_saved_revision_and_replay_survive_default_binding_removal(self):
        actor = self.f.admin
        session = self.service.create_session(actor, {}, "session")["session"]
        body = self.body()
        saved = self.service.save_card(actor, session["id"], body, "card")
        self.f.path.write_text("{}", encoding="utf-8")
        self.assertEqual(
            self.service.save_card(actor, session["id"], body, "card"), saved
        )
        self.assertEqual(
            self.service.get_revision(actor, session["id"], saved["revision"]["id"]),
            saved["revision"],
        )
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.service.save_card(actor, session["id"], body, "new-card")

    def test_explicit_profile_is_not_replaced_and_legacy_without_default_stays(self):
        body = self.body()
        for profile in (fixture.OLD_PROFILE, TAOMATE, None):
            explicit = {**body, "deployment_profile_id": profile}
            self.assertEqual(default_chat_profile(self.f.settings, explicit), explicit)
        legacy = replace(self.f.settings, default_deployment_profile_id=None)
        self.assertEqual(default_chat_profile(legacy, body), body)
